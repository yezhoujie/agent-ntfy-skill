"""常驻 daemon：唯一的 ntfy 订阅者、唯一的状态持有者；其余子命令经 unix socket 向它说话。

    agent → ask ──unix socket──▶ daemon ──SSE──▶ ntfy.sh ──▶ 手机
                ◀──────────────         ◀─────────────────── 回复 / 指令

只做通路：把提问送到手机、把人的话原样送回 ask；不解释内容、不代答。

文件都在 AGENT_NTFY_HOME（默认 ~/.agent-ntfy/，0700）：leases.json · daemon.sock · daemon.pid · daemon.log（0600）。
日志只写槽位名 / 消息 id / 事件类型 / 错误类别——topic 是密码，正文与回复是用户的项目信息，都不落日志。

socket 协议是 JSON Lines：客户端连上后发一行 {"cmd": ...}，daemon 回若干行事件（每行一个 JSON 对象）。
ask 的连接保持到终态（reply / timeout / error）：daemon 若死了，连接当场断开，ask 立刻失败——这就是它的响亮信号；
confirm-sub 同样保持到终态，且中途客户端会再发一行 {"ready": true}（用户订阅好了，可以发测试通知了）；
其余命令一问一答即关。error 事件必带 sent：调用方要据此知道消息发出去了没有。

线程模型：主线程用 selectors 跑一切状态变化（socket、pending、active、租约、回执）；订阅线程只把 ntfy 事件放进队列，
再往唤醒管道写一个字节。游标（最后事件的 time + 边界秒内已见 id）由订阅线程独占，主线程只读它做 status。
注入线程只跑 herdr 子进程（inject.deliver()）：主线程把「槽位 + 租约 + 正文」排进注入队列，结果作为 _delivered 事件回到
主线程队列，回执发布 / 回执 id 记忆 / 租约释放仍只在主线程。一条注入线程串行处理全部投递：一个槽位同一时刻只处理一条、按到达顺序。
选 selectors 不选 asyncio：ntfy 客户端是同步阻塞的，asyncio 也得把它塞进线程；一条线程 + 一把队列已经够，
且不引入第二套并发模型。

订阅游标用 time 而不是消息 id：since=<已过期或不存在的 id> 服务端会回放整个缓存（实测），
把过期指令灌进 agent；since=<秒级 time> 含该秒，所以边界秒内的每个 id 都要记住去重。
冷启动不带 since：pending 不跨 daemon 生命周期，回放旧消息只会把过期指令注进 agent。
"""

import json
import logging
import os
import queue
import selectors
import signal
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import inject
import render
import texts
import validate
from ntfyclient import NtfyClient, NtfyClosed, NtfyError
from state import LEASES_PATH, Lease, NeedsUserDecision, SecretStore, SlotState, State, StateError

HOME_DIR = LEASES_PATH.parent
DEFAULT_TIMEOUT = 12 * 3600  # ntfy 缓存时长：手机离线超过这个时长就收不到了，再等没有意义
BACKOFF_BASE = 1.0
BACKOFF_MAX = 30.0
WARN_AFTER_FAILURES = 3  # 连续重连失败这么多次，或
WARN_AFTER_SECONDS = 60.0  # 断开这么久 ⇒ 向每个 pending 的 ask 发 warning，不许静默重试
# 卡片收尾的 Title 前缀在 texts（prefix.timeout / prefix.cancelled …），按那张卡片自己的语言取。
# ask 那端先断开（Ctrl-C / 被 kill）也收卡片：没人等了，带按钮悬在手机上就是误触的温床
CONFIRM_TIMEOUT = 600.0  # 可达性确认：订阅 + 找通知栏 + 点按钮，够用。CLI 侧同名默认值要同步（它刻意不 import 本模块）
# slots 视图里的 state 由 CLI 按语言显示；daemon 只给稳定的 state_key（unassigned / idle / active / confirming），叠在状态层三态之上
STATE_KEYS = {SlotState.UNASSIGNED.value: "unassigned", SlotState.IDLE.value: "idle", SlotState.ACTIVE.value: "active"}
MAX_REQUEST_BYTES = 1024 * 1024  # 一行请求的上限：本地 0600 socket 威胁不大，但不能让一个不发换行的客户端把内存吃光
SEND_TIMEOUT = 5.0  # 往客户端写事件的阻塞上限
STOP_INJECT_GRACE = 2.0  # 关停时给在途的 herdr 调用这么久收尾（正常几十毫秒就回）；过了就按「无法确认」发回执
STOP_RECEIPT_TIMEOUT = 5.0  # 关停路径上每张回执的发布上限：尽力而为，不用平时的 30 秒把 --stop 拖住
LOG = logging.getLogger("agent-ntfy.daemon")
LOG_ROOT = logging.getLogger("agent-ntfy")  # handler 挂这一级：注入层（agent-ntfy.inject）的日志才会一起进 daemon.log


class DaemonError(Exception):
    """启动前就能判定的失败：已有实例在跑、目录不可用、语言码不对。文案在 texts（daemon_error.<key>）：str(e) 中文，text(lang) 按语言。"""

    def __init__(self, key: str, **fmt):
        self.key, self.fmt = key, fmt
        super().__init__(self.text("zh"))

    def text(self, lang: str) -> str:
        return texts.t(f"daemon_error.{self.key}", lang, **self.fmt)


def paths(home: Path) -> dict[str, Path]:
    return {"sock": home / "daemon.sock", "pid": home / "daemon.pid", "log": home / "daemon.log", "leases": home / "leases.json"}


def pid_alive(pid_file: Path) -> int | None:
    """pid 文件指向的进程还活着就返回 pid，否则 None（文件不存在 / 内容坏 / 进程没了）。"""
    try:
        pid = int(pid_file.read_text().strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        return pid
    return pid


@dataclass
class Pending:
    """一个正挂着等回复的提问。存整个 Rendered：回复到达后要拼「已回复」态，发完就丢就拼不出来了。"""

    slot: str
    topic: str
    leased_by: str
    conn: socket.socket
    rendered: render.Rendered  # 自带 lang：之后同 seq 的更新沿用它
    msg_id: str
    deadline: float
    warned: bool = False

    @property
    def lang(self) -> str:
        return self.rendered.lang


@dataclass
class Confirm:
    """一次进行中的可达性确认（confirm-sub）。与 Pending 分开存：确认可以在未分配的槽位上做，
    而 Pending 的键集合会原样交给状态层当「活跃」，状态层要求活跃 ⊆ 已租用。"""

    slot: str
    topic: str
    conn: socket.socket
    deadline: float
    lang: str  # 请求方的语言：CLI 提示与测试消息都用它
    rendered: render.Rendered | None = None  # 测试消息发出之后才有
    msg_id: str | None = None


@dataclass
class Receipt:
    """某槽位最近一条投递失败回执。存整个 Rendered：按钮被点后要拼「已释放 / 已忽略」态的更新，发完就丢就拼不出来了。只在内存里。"""

    slot: str
    msg_id: str
    rendered: render.Rendered  # 自带 lang：按钮被点后的更新沿用它

    @property
    def lang(self) -> str:
        return self.rendered.lang


@dataclass
class Client:
    """一条 socket 连接的读缓冲。"""

    sock: socket.socket
    buf: bytes = b""
    pending: Pending | None = None
    confirm: Confirm | None = None


@dataclass
class Cursor:
    """订阅游标：最后事件的秒级 time + 该秒起已见过的消息 id。订阅线程独占写。"""

    time: int | None = None
    seen: dict[str, int] = field(default_factory=dict)  # id → time

    def advance(self, event: dict) -> bool:
        """记录一个事件；返回它是否是没见过的。

        open 事件带的是「现在」的 time，而服务端先发 open、后回放 since 起的旧消息（实测）：
        让 open 推进游标会把边界秒内已见的 id 清掉，回放的那条就会被当成新消息再投一次。
        所以 open 只在首连时用来建立游标（限定之后重连的回放起点），其余一概不动。
        """
        t = event.get("time")
        if not isinstance(t, int):
            return True
        if event.get("event") == "open":
            if self.time is None:
                self.time = t
            return True
        mid = event.get("id")
        if isinstance(mid, str) and mid in self.seen:
            return False
        if self.time is None or t > self.time:
            self.time = t
            self.seen = {k: v for k, v in self.seen.items() if v >= t}  # 只保留边界秒内的
        if isinstance(mid, str):
            self.seen[mid] = t
        return True

    def since(self) -> str | None:
        return None if self.time is None else str(self.time)


class Daemon:
    def __init__(self, home: Path = HOME_DIR, *, client: NtfyClient | None = None, store: SecretStore | None = None,
                 pool_size: int | None = None, log_to_stderr: bool = False, herdr: inject.Runner | None = None,
                 lang: str = texts.DEFAULT_LANG,
                 backoff_base: float = BACKOFF_BASE, backoff_max: float = BACKOFF_MAX,
                 warn_after_failures: int = WARN_AFTER_FAILURES, warn_after_seconds: float = WARN_AFTER_SECONDS):
        self.home = Path(home)
        self.paths = paths(self.home)
        self.client = client or NtfyClient()
        self._herdr: inject.Runner = herdr or inject.run_herdr
        # 没有 ask 上下文的一切（投递失败回执 / 关停回执 / 解析不出请求时的报错）用这个语言；由进程入口解析环境变量后传入，这里不读环境
        if not texts.is_lang(lang):
            raise DaemonError("bad_lang", value=repr(lang))  # 不静默回退：语言错了整个 daemon 的文案都会错
        self.lang = lang
        self._store = store
        self._pool_size = pool_size
        self.log_to_stderr = log_to_stderr
        self.backoff_base, self.backoff_max = backoff_base, backoff_max
        self.warn_after_failures, self.warn_after_seconds = warn_after_failures, warn_after_seconds
        self.state: State | None = None
        self._topics: list[str] = []
        self._slot_of: dict[str, str] = {}
        self._pending: dict[str, Pending] = {}  # slot → Pending；键集合就是「活跃」
        self._confirming: dict[str, Confirm] = {}  # slot → 进行中的可达性确认；对 release / ask 按活跃对待，但不交给状态层
        self._own_ids: dict[str, float] = {}  # 我们自己发布的消息 id → 发布时刻；订阅流会回显它们，不是回复
        self._receipts: dict[str, Receipt] = {}  # slot → 最近一条投递失败回执；按钮被点后用它做同 seq 更新 + clear
        self._inject_q: queue.Queue = queue.Queue()  # 主线程 → 注入线程：(slot, leased_by, 消息 id, 正文)；None 是收工
        self._inject_thread: threading.Thread | None = None
        self._inflight: dict[str, str] = {}  # 消息 id → slot：排进注入队列、尚未回到主线程的投递（只在主线程改；关停时靠它发回执）
        self._clients: dict[int, Client] = {}
        self._events: queue.Queue = queue.Queue()
        self._cursor = Cursor()
        self._sub = None
        self._sub_lock = threading.Lock()
        self._stopping = False
        self._topics_gen = 0  # restart_subscription() 每次 +1；订阅线程建连后比对，变了就立刻重来
        self._connected = False
        self._disconnected_since: float | None = None
        self._failures = 0
        self._warned_disconnect = False
        self._wake_r = self._wake_w = -1
        self._sel: selectors.DefaultSelector | None = None
        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._log_handlers: list[logging.Handler] = []
        self._wrote_pid = False

    # ---------------------------------------------------------------- 生命周期

    def run(self) -> None:
        """前台运行直到 stop() / SIGTERM / SIGINT / SIGHUP。另一个实例还活着就拒绝启动。

        单例靠 socket 的 bind 而不是 pid 文件：bind 是原子的，pid 文件既有窗口（写它之前别人已经起来）
        又有 pid 复用的误判。pid 文件紧跟 bind 之后写，钥匙串卡住时 --status / --stop 也有把手。
        """
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.home, 0o700)  # 目录早就存在时 mkdir 的 mode 不生效
        self._setup_logging()
        self._wake_r, self._wake_w = os.pipe()
        self._sel = selectors.DefaultSelector()
        try:
            self._listen()  # 先占 socket：已有实例 / 路径太长 / 目录不可写在这里就能判定，别等状态都建好了再死
            self._write(self.paths["pid"], f"{os.getpid()}\n")
            self._wrote_pid = True
            self._init_state()
        except DaemonError as e:
            LOG.error("启动失败：%s", e)
            self._abort()
            raise
        except OSError as e:
            LOG.error("启动失败：%s: %s", type(e).__name__, e)
            self._abort()
            raise DaemonError("socket", path=self.paths["sock"], error=e) from e
        except StateError as e:
            LOG.error("启动失败：%s: %s", type(e).__name__, e)  # state 层的报错文案不含 topic
            self._abort()
            raise DaemonError("state_init", error=e) from e
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                signal.signal(sig, lambda *_: self.stop())
        self._thread = threading.Thread(target=self._subscribe_loop, name="ntfy-subscriber", daemon=True)
        self._thread.start()
        self._inject_thread = threading.Thread(target=self._inject_loop, name="herdr-inject", daemon=True)
        self._inject_thread.start()
        LOG.info("daemon 启动 pid=%s 槽位=%s", os.getpid(), len(self._topics))
        try:
            self._loop()
        except BaseException as e:
            # --detach 下 stderr 接的是 /dev/null，崩溃原因不能只剩一个消失的进程
            LOG.error("主循环异常退出：%s", type(e).__name__, exc_info=True)
            raise
        finally:
            self._shutdown()

    def stop(self) -> None:
        """线程安全、信号处理器里也能调：只置标记 + 唤醒主循环，收尾在主循环里做。"""
        self._stopping = True
        self._wake()

    def _setup_logging(self) -> None:
        LOG_ROOT.setLevel(logging.INFO)
        fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        os.close(os.open(self.paths["log"], os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600))  # 先以 0600 建好再交给 FileHandler
        handlers: list[logging.Handler] = [logging.FileHandler(self.paths["log"], encoding="utf-8")]
        if self.log_to_stderr:
            handlers.append(logging.StreamHandler(sys.stderr))
        for h in handlers:
            h.setFormatter(fmt)
            LOG_ROOT.addHandler(h)
        self._log_handlers = handlers

    def _init_state(self) -> None:
        store = self._store
        if store is None:
            from state import KeychainStore  # 只有 daemon 碰钥匙串
            store = KeychainStore()
        if self._pool_size:
            self.state = State(store, self.paths["leases"], pool_size=self._pool_size)
        else:
            self.state = State(store, self.paths["leases"])
        self._topics = self.state.topics()  # 惰性建池在这里触发
        self._slot_of = {t: s for s, t in zip(self.state.slot_names(), self._topics)}

    def _write(self, path: Path, text: str) -> None:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)

    def _listen(self) -> None:
        sock_path = self.paths["sock"]
        if sock_path.exists():
            # 文件在：要么另一个实例活着（能连上 ⇒ 拒绝），要么是上次没清干净的残骸（连不上 ⇒ 清掉重来）
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.settimeout(1.0)
                probe.connect(str(sock_path))
            except OSError:
                sock_path.unlink()
            else:
                raise DaemonError("already_running", path=sock_path)
            finally:
                probe.close()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(sock_path))
            os.chmod(sock_path, 0o600)
            listener.listen(16)
        except OSError:
            listener.close()
            raise
        listener.setblocking(False)
        self._listener = listener
        assert self._sel is not None
        self._sel.register(listener, selectors.EVENT_READ, "listener")
        self._sel.register(self._wake_r, selectors.EVENT_READ, "wake")

    def _abort(self) -> None:
        """启动半途失败：把已占的东西放掉，别留下让下次启动误判的残骸。"""
        if self._listener is not None:
            self._listener.close()
            self._listener = None
            try:
                self.paths["sock"].unlink()
            except FileNotFoundError:
                pass
        if self._wrote_pid:
            try:
                self.paths["pid"].unlink()
            except FileNotFoundError:
                pass
        self._close_fds()
        self._teardown_logging()

    def _shutdown(self) -> None:
        LOG.info("daemon 收尾：pending=%s confirming=%s", len(self._pending), len(self._confirming))
        for pend in list(self._pending.values()):
            # 不改手机上的卡片：daemon 停了不代表用户不会回，重启后那条回复走无 pending 分支
            self._finish(pend, {"event": "error", "kind": "daemon_stopping", "sent": True,
                                "message": texts.t("daemon.stopping.ask", pend.lang)}, touch_card=False)
        self.client.timeout = STOP_RECEIPT_TIMEOUT
        for conf in list(self._confirming.values()):
            # 确认的点击跨不了 daemon 生命周期（重启后那个标记当无效丢弃），卡片留着按钮只会误导：按取消收掉
            self._finish_confirm(conf, {"event": "error", "kind": "daemon_stopping", "sent": conf.msg_id is not None,
                                        "message": texts.t("daemon.stopping.confirm", conf.lang)}, prefix_key="prefix.cancelled")
        with self._sub_lock:
            sub = self._sub
        if sub is not None:
            sub.close()
        self._stop_injections()
        if self._listener is not None and self._sel is not None:
            self._sel.unregister(self._listener)
            self._listener.close()
        for c in list(self._clients.values()):
            self._drop_client(c)
        for key in ("sock", "pid"):
            try:
                self.paths[key].unlink()
            except FileNotFoundError:
                pass
        LOG.info("daemon 已退出")
        self._teardown_logging()
        self._close_fds()

    def _stop_injections(self) -> None:
        """关停时还没投出去的消息不能静默消失（消息已从订阅流消费、冷启动不回放）：留痕 + 尽力发回执。

        排队中的：从没尝试过，回执说「未送达」。在途的：给 herdr 一个宽限期正常收尾；等不到就按「无法确认」发回执。
        回执每张最多等 STOP_RECEIPT_TIMEOUT，失败只记日志——关停路径上不能被网络拖死。
        """
        self.client.timeout = STOP_RECEIPT_TIMEOUT  # 从这里起发的每张回执 / 更新都是尽力而为
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(1.0)  # 流已关：让订阅线程把手上最后几条推完，别在下面排空的时候还往里塞
        self._drain_events()  # 主循环退出后、流关闭前订阅线程推进来的消息：已从流里消费、冷启动不回放，按原路过一遍（进注入队列 / 控制标记）
        queued: list[tuple[str, str]] = []
        while True:
            try:
                slot, _, mid, _ = self._inject_q.get_nowait()
            except queue.Empty:
                break
            queued.append((slot, mid))
            LOG.warning("关停，丢弃未投递的消息 slot=%s id=%s", slot, mid)
        self._inject_q.put(None)  # 注入线程收工
        if self._inject_thread is not None and self._inject_thread.is_alive():
            self._inject_thread.join(STOP_INJECT_GRACE)
        self._drain_events()  # 宽限期内回来的结果照常落地（失败照常发回执）
        LOG.info("关停时注入中 injecting=%s（排队 %s，在途未归 %s）", len(self._inflight), len(queued), len(self._inflight) - len(queued))
        queued_ids = {mid for _, mid in queued}
        for mid, slot in list(self._inflight.items()):
            uncertain = mid not in queued_ids
            if uncertain:
                LOG.warning("关停，在途投递等不到结果 slot=%s id=%s", slot, mid)
            try:
                topic = self._state().topic_of(slot)
                r = inject.render_stopping_receipt(slot, reply_url=self.client.topic_url(topic), lang=self.lang, uncertain=uncertain)
                self._publish_own(topic, r.message, title=r.title, actions=r.actions)
            except (NtfyError, StateError) as e:
                LOG.warning("关停回执发布失败 slot=%s id=%s：%s", slot, mid, type(e).__name__)
        self._inflight.clear()

    def _close_fds(self) -> None:
        if self._sel is not None:
            self._sel.close()
            self._sel = None
        for fd in (self._wake_r, self._wake_w):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self._wake_r = self._wake_w = -1  # 关掉后再 _wake() 不能往被复用的 fd 里写

    def _teardown_logging(self) -> None:
        for h in self._log_handlers:
            LOG_ROOT.removeHandler(h)
            h.close()
        self._log_handlers = []

    # ---------------------------------------------------------------- 主循环

    def _wake(self) -> None:
        if self._wake_w < 0:
            return
        try:
            os.write(self._wake_w, b"x")
        except OSError:
            pass

    def _loop(self) -> None:
        assert self._sel is not None
        while not self._stopping:
            timeout = self._next_timeout()
            for key, _ in self._sel.select(timeout):
                if key.data == "listener":
                    self._accept()
                elif key.data == "wake":
                    os.read(self._wake_r, 4096)
                    self._drain_events()
                else:
                    self._read_client(key.data)
            self._expire()
            self._check_disconnect_warning()

    def _next_timeout(self) -> float:
        now = time.monotonic()
        deadlines = [p.deadline for p in self._pending.values()] + [c.deadline for c in self._confirming.values()]
        soonest = min(deadlines, default=now + 5.0)
        return max(0.0, min(soonest - now, 5.0))

    def _accept(self) -> None:
        assert self._listener is not None
        try:
            sock, _ = self._listener.accept()
        except OSError:
            return
        sock.settimeout(SEND_TIMEOUT)  # 读只在 select 说可读时做；写用阻塞 + 上限，免得非阻塞下 sendall 写半行
        c = Client(sock)
        self._clients[sock.fileno()] = c
        assert self._sel is not None
        self._sel.register(sock, selectors.EVENT_READ, c)

    def _read_client(self, c: Client) -> None:
        try:
            data = c.sock.recv(65536)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            data = b""
        if not data:
            # 对端关了。若它是正挂着等的 ask（agent 那边被打断了）：移出活跃，卡片按超时同款收掉——
            # 没人等的卡片带着按钮悬在手机上就是误触的温床；用户之后的回复走无 pending 分支
            if c.pending is not None and self._pending.get(c.pending.slot) is c.pending:
                LOG.info("ask 连接断开 slot=%s id=%s，取消", c.pending.slot, c.pending.msg_id)
                self._finish(c.pending, {"event": "cancelled"}, touch_card=True)
            if c.confirm is not None and self._confirming.get(c.confirm.slot) is c.confirm:
                LOG.info("confirm-sub 连接断开 slot=%s id=%s，取消", c.confirm.slot, c.confirm.msg_id)
                self._finish_confirm(c.confirm, {"event": "cancelled"}, prefix_key="prefix.cancelled")
            self._drop_client(c)
            return
        c.buf += data
        if c.pending is not None:
            c.buf = b""
            return  # ask 期间客户端不该再发东西，收到也忽略
        if c.confirm is not None:
            self._consume_ready(c)
            return
        if b"\n" not in c.buf:
            if len(c.buf) > MAX_REQUEST_BYTES:
                self._send(c.sock, {"event": "error", "kind": "bad_request", "sent": False,
                                    "message": texts.t("daemon.bad_request.too_long", self.lang, limit=MAX_REQUEST_BYTES)})
                self._drop_client(c)
            return
        line, _, c.buf = c.buf.partition(b"\n")
        try:
            req = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            self._send(c.sock, {"event": "error", "kind": "bad_request", "sent": False, "message": texts.t("daemon.bad_request.not_json", self.lang, error=e)})
            self._drop_client(c)
            return
        if not isinstance(req, dict):
            self._send(c.sock, {"event": "error", "kind": "bad_request", "sent": False, "message": texts.t("daemon.bad_request.not_object", self.lang)})
            self._drop_client(c)
            return
        self._dispatch(c, req)
        if c.confirm is not None and c.buf:
            self._consume_ready(c)  # 请求行与 ready 行同一个包到达：余量里可能已经有它

    def _consume_ready(self, c: Client) -> None:
        """确认流程里客户端只会再发一行 {"ready": true}（用户已订阅、可以发测试消息了）。

        按行取，半行留着等下一次 recv（unix socket 上分段罕见，但协议不能靠这个）；发过测试消息之后再来的行一律忽略。
        """
        while b"\n" in c.buf:
            line, _, c.buf = c.buf.partition(b"\n")
            if c.confirm is None or c.confirm.msg_id is not None or self._confirming.get(c.confirm.slot) is not c.confirm:
                continue  # 发过了、或已经终态（比如第一次发布失败）：同包里再多的 ready 都不能再发一张没人等的卡片
            try:
                req = json.loads(line.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(req, dict) and req.get("ready") is True:
                self._confirm_publish(c.confirm)
        if len(c.buf) > MAX_REQUEST_BYTES:
            c.buf = b""  # 不发换行的客户端：别让它把内存吃光

    def _drop_client(self, c: Client) -> None:
        try:
            if self._sel is not None:
                self._sel.unregister(c.sock)
        except (KeyError, ValueError):
            pass
        for k, v in list(self._clients.items()):
            if v is c:
                del self._clients[k]
        try:
            c.sock.close()
        except OSError:
            pass

    def _send(self, sock: socket.socket, event: dict) -> bool:
        try:
            sock.sendall((json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8"))
            return True
        except OSError:
            return False

    # ---------------------------------------------------------------- 命令分派

    def _dispatch(self, c: Client, req: dict) -> None:
        cmd = req.get("cmd")
        if cmd == "ask":
            self._cmd_ask(c, req)
            return
        if cmd == "slots":
            self._reply_once(c, {"event": "slots", "slots": self._slots_view()})
        elif cmd == "release":
            self._reply_once(c, self._cmd_release(req))
        elif cmd == "status":
            self._reply_once(c, self._status())
        elif cmd == "confirm-sub":
            self._cmd_confirm(c, req)
        elif cmd == "add-slot":
            self._reply_once(c, self._cmd_add_slot(self._req_lang(req)))
        else:
            self._reply_once(c, {"event": "error", "kind": "bad_request", "sent": False, "message": texts.t("daemon.bad_request.unknown_cmd", self._req_lang(req), cmd=repr(cmd))})

    def _req_lang(self, req: dict) -> str:
        """这条请求的文案语言：CLI 在进程入口解析好随请求带来；没带就用 daemon 自己的。"""
        lang = req.get("lang")
        return str(lang) if texts.is_lang(lang) else self.lang

    def _reply_once(self, c: Client, event: dict) -> None:
        self._send(c.sock, event)
        self._drop_client(c)

    def _state(self) -> State:
        assert self.state is not None
        return self.state

    def _active(self) -> set[str]:
        """交给状态层的「活跃」：只有 pending。确认中不算——它可以落在未分配的槽位上，状态层要求活跃 ⊆ 已租用。"""
        return set(self._pending)

    def _slots_view(self) -> dict:
        """状态层的视图加一个稳定的 state_key（显示文案由 CLI 按语言取），确认中的槽位叠在三态之上。"""
        view = self._state().slots(self._active())
        for slot, rec in view.items():
            rec["state_key"] = "confirming" if slot in self._confirming else STATE_KEYS.get(rec["state"], rec["state"])
        return view

    def _status(self) -> dict:
        return {"event": "status", "pid": os.getpid(), "subscribed": self._connected,
                "disconnected_for": None if self._connected or self._disconnected_since is None
                else round(time.monotonic() - self._disconnected_since, 1),
                "pending": len(self._pending), "confirming": len(self._confirming), "pool": len(self._topics),
                "cursor": self._cursor.since(), "injecting": len(self._inflight)}

    def _unknown_slot(self, slot, lang: str) -> dict:
        """客户端给的槽位名不在池子里 / 形态不对：是输入错（kind=unknown_slot），不是状态层坏了（kind=state）。"""
        return {"event": "error", "kind": "unknown_slot", "sent": False, "message": texts.t("daemon.unknown_slot", lang, slot=repr(slot))}

    def _cmd_release(self, req: dict) -> dict:
        slot = req.get("slot")
        leased_by = req.get("leased_by")
        lang = self._req_lang(req)
        try:
            known = self._state().slots()
        except StateError as e:
            return {"event": "error", "kind": "state", "sent": False, "message": e.text(lang)}
        if not slot and leased_by:
            slot = next((s for s, rec in known.items() if rec["leased_by"] == leased_by), None)
            if slot is None:
                return {"event": "error", "kind": "no_lease", "sent": False, "message": texts.t("daemon.no_lease", lang)}
        if not slot and not leased_by:
            return {"event": "error", "kind": "bad_request", "sent": False, "message": texts.t("daemon.bad_request.release_args", lang)}
        if slot not in known:
            return self._unknown_slot(slot, lang)
        assert isinstance(slot, str)
        if slot in self._pending:
            return {"event": "error", "kind": "active", "sent": False, "message": texts.t("daemon.release.active", lang, slot=slot)}
        if slot in self._confirming:
            return {"event": "error", "kind": "active", "sent": False, "message": texts.t("daemon.release.confirming", lang, slot=slot)}
        try:
            self._state().release(slot, self._active())
        except StateError as e:
            return {"event": "error", "kind": "state", "sent": False, "message": e.text(lang)}
        LOG.info("释放 slot=%s", slot)
        return {"event": "released", "slot": slot}

    # ---------------------------------------------------------------- ask

    def _cmd_ask(self, c: Client, req: dict) -> None:
        payload = req.get("payload")
        leased_by = req.get("leased_by")
        tag = req.get("tag")
        timeout = req.get("timeout", DEFAULT_TIMEOUT)
        # CLI 已按 JSON lang → 环境 → en 解析好随请求带来；没带（别的客户端直接说 socket 协议）就自己按同一优先级看 JSON，再退到 daemon 的
        lang = str(req["lang"]) if texts.is_lang(req.get("lang")) else validate.lang_of(payload, self.lang)
        if not isinstance(leased_by, str) or not leased_by or not isinstance(timeout, (int, float)) or timeout <= 0:
            self._reply_once(c, {"event": "error", "kind": "bad_request", "sent": False, "message": texts.t("daemon.bad_request.ask_args", lang)})
            return
        problems = validate.check(payload, lang)  # ask 已在本地校验过；这里再核一次（长度）作防御，不重复报文案
        if problems:
            joined = texts.t("daemon.invalid_input.sep", lang).join(f"{validate.field_label(p.field, lang)}: {p.message}" for p in problems)
            self._reply_once(c, {"event": "error", "kind": "invalid_input", "sent": False, "message": texts.t("daemon.invalid_input", lang, problems=joined)})
            return
        assert isinstance(payload, dict)
        state = self._state()
        slots = state.slots()  # 这次 ask 里租约文件只读这一次；acquire() 内部会再读一次，那是状态层的事
        existing = next((s for s, rec in slots.items() if rec["leased_by"] == leased_by), None)
        if existing in self._pending:
            self._reply_once(c, {"event": "error", "kind": "busy", "sent": False, "message": texts.t("daemon.busy.pending", lang, slot=existing)})
            return
        if existing in self._confirming:
            self._reply_once(c, {"event": "error", "kind": "busy", "sent": False, "message": texts.t("daemon.busy.confirming", lang, slot=existing)})
            return
        try:
            if existing:
                # 同一个目标复用自己的租约（一个目标一个 topic），不刷新 leased_at——那是租约起点，不是上次提问时间
                lease = Lease(slot=existing, topic=state.topic_of(existing), subscribed=slots[existing]["subscribed"])
            else:
                lease = state.acquire(leased_by, self._active())
        except NeedsUserDecision as e:
            cands = [{"slot": s, "subscribed": slots[s]["subscribed"]} for s in e.candidates]
            listed = (", ".join(texts.t("daemon.candidate.confirmed" if x["subscribed"] else "daemon.candidate.unconfirmed", lang, slot=x["slot"]) for x in cands)
                      if cands else texts.t("daemon.no_free_slot.none", lang))
            self._reply_once(c, {"event": "error", "kind": "no_free_slot", "sent": False, "candidates": cands,
                                 "message": texts.t("daemon.no_free_slot", lang, candidates=listed)})
            return
        except StateError as e:
            self._reply_once(c, {"event": "error", "kind": "state", "sent": False, "message": e.text(lang)})
            return
        if lease.slot in self._confirming:
            if not existing:
                try:
                    state.release(lease.slot, self._active())  # busy 就是什么都没动：刚租到的退回去，重试时再租
                except StateError as e:
                    LOG.warning("退回租约失败 slot=%s：%s", lease.slot, e)
            self._reply_once(c, {"event": "error", "kind": "busy", "sent": False, "message": texts.t("daemon.busy.confirming", lang, slot=lease.slot)})
            return
        if not lease.subscribed:
            self._reply_once(c, {"event": "error", "kind": "unconfirmed", "sent": False, "slot": lease.slot, "message": texts.t("daemon.unconfirmed", lang, slot=lease.slot)})
            return
        if not isinstance(tag, str) or not tag:
            tag = lease.slot
        tag = tag.encode("utf-8")[:render.TAG_MAX_BYTES].decode("utf-8", errors="ignore")  # tag 是通路自己的东西，超了截不报错
        rendered = render.render_question(payload, tag=tag, reply_url=self.client.topic_url(lease.topic), lang=lang)
        try:
            msg_id = self._publish_own(lease.topic, rendered.message, title=rendered.title, actions=rendered.actions)
        except NtfyError as e:
            LOG.warning("发布失败 slot=%s：%s", lease.slot, type(e).__name__)
            self._reply_once(c, {"event": "error", "kind": "publish_failed", "sent": False, "message": texts.t("daemon.publish_failed", lang, error=e)})
            return
        pend = Pending(slot=lease.slot, topic=lease.topic, leased_by=leased_by, conn=c.sock, rendered=rendered,
                       msg_id=msg_id, deadline=time.monotonic() + float(timeout))
        self._pending[lease.slot] = pend
        c.pending = pend
        LOG.info("提问已发 slot=%s id=%s timeout=%ss", lease.slot, msg_id, timeout)
        self._send(c.sock, {"event": "sent", "slot": lease.slot, "id": msg_id})
        if self._warned_disconnect:  # 订阅正断着：发布走 HTTP 照样通，但回复此刻收不到，得让它知道
            self._send(c.sock, {"event": "warning", "message": texts.t("daemon.warning.disconnected_at_send", lang)})

    # ---------------------------------------------------------------- confirm-sub / add-slot

    def _cmd_confirm(self, c: Client, req: dict) -> None:
        """可达性确认闸：验「通知弹出 → 点按钮 → 回传」整条链路。连接保持到终态，同 ask。

        默认两段：先把 topic 交给 CLI（唯一会把 topic 名交给客户端的地方），等它回一行 {"ready": true}
        （用户在手机上订阅好了）再发测试消息——先发再订阅的话通知弹不弹没有把握，而闸验的正是弹出。
        show_topic：只回 topic 就关，不发、不进确认态。subscribed：跳过 topic 段直接发（用户已订阅、agent 代跑）。
        """
        slot = req.get("slot")
        timeout = req.get("timeout", CONFIRM_TIMEOUT)
        lang = self._req_lang(req)
        if not isinstance(timeout, (int, float)) or timeout <= 0:
            self._reply_once(c, {"event": "error", "kind": "bad_request", "sent": False, "message": texts.t("daemon.bad_request.confirm_timeout", lang)})
            return
        try:
            known = self._state().slots()
        except StateError as e:
            self._reply_once(c, {"event": "error", "kind": "state", "sent": False, "message": e.text(lang)})
            return
        if not isinstance(slot, str) or slot not in known:
            self._reply_once(c, self._unknown_slot(slot, lang))
            return
        rec = known[slot]
        topic = self._state().topic_of(slot)
        url = self.client.topic_url(topic)
        if req.get("show_topic"):
            self._reply_once(c, {"event": "topic", "topic": topic, "url": url})
            return
        if rec["subscribed"] and not req.get("again"):
            self._reply_once(c, {"event": "already_confirmed", "slot": slot})
            return
        if slot in self._pending or slot in self._confirming:
            key = "daemon.confirm.busy_pending" if slot in self._pending else "daemon.confirm.busy_confirming"
            self._reply_once(c, {"event": "error", "kind": "busy", "sent": False, "message": texts.t(key, lang, slot=slot)})
            return
        conf = Confirm(slot=slot, topic=topic, conn=c.sock, deadline=time.monotonic() + float(timeout), lang=lang)
        self._confirming[slot] = conf
        c.confirm = conf
        LOG.info("确认开始 slot=%s timeout=%ss", slot, timeout)
        if req.get("subscribed"):
            self._confirm_publish(conf)
            return
        self._send(c.sock, {"event": "topic", "topic": topic, "url": url})

    def _confirm_publish(self, conf: Confirm) -> None:
        """发测试消息（进入等点击）；发不出去就以 error 终态收掉。"""
        rendered = inject.render_confirm_request(conf.slot, reply_url=self.client.topic_url(conf.topic), lang=conf.lang)
        try:
            msg_id = self._publish_own(conf.topic, rendered.message, title=rendered.title, actions=rendered.actions)
        except NtfyError as e:
            LOG.warning("确认消息发布失败 slot=%s：%s", conf.slot, type(e).__name__)
            self._finish_confirm(conf, {"event": "error", "kind": "publish_failed", "sent": False, "message": texts.t("daemon.publish_failed.confirm", conf.lang, error=e)}, prefix_key=None)
            return
        conf.rendered, conf.msg_id = rendered, msg_id
        LOG.info("确认消息已发 slot=%s id=%s", conf.slot, msg_id)
        self._send(conf.conn, {"event": "sent", "slot": conf.slot, "id": msg_id})
        if self._warned_disconnect:
            self._send(conf.conn, {"event": "warning", "message": texts.t("daemon.warning.disconnected_at_confirm", conf.lang)})

    def _on_confirmed(self, slot: str, mid: str) -> None:
        conf = self._confirming.get(slot)
        if conf is None:
            LOG.info("无效的确认标记 slot=%s id=%s（该槽位不在确认中），丢弃", slot, mid)  # 我们自己的固定标记，不注入
            return
        try:
            self._state().mark_subscribed(slot)
        except StateError as e:
            LOG.error("确认落盘失败 slot=%s：%s", slot, e)
            self._finish_confirm(conf, {"event": "error", "kind": "state", "sent": True, "message": texts.t("daemon.confirm.mark_failed", conf.lang)},
                                 prefix_key="prefix.cancelled")  # 卡片一样要收：没人等的按钮悬在手机上是误触的温床
            return
        LOG.info("确认完成 slot=%s id=%s", slot, mid)
        self._finish_confirm(conf, {"event": "confirmed", "slot": slot}, prefix_key="prefix.confirmed")

    def _finish_confirm(self, conf: Confirm, event: dict, *, prefix_key: str | None) -> None:
        """确认的终态：事件交给 CLI、关连接、移出确认中；发过测试消息且给了前缀（texts 的 key）就同 seq 更新（无按钮）+ clear。"""
        if self._confirming.get(conf.slot) is conf:
            del self._confirming[conf.slot]
        self._send(conf.conn, event)
        for c in list(self._clients.values()):
            if c.confirm is conf:
                c.confirm = None
                self._drop_client(c)
        if prefix_key is None or conf.msg_id is None or conf.rendered is None:
            return
        try:
            resp = self.client.update(conf.topic, conf.msg_id, conf.rendered.body, title=texts.t(prefix_key, conf.rendered.lang) + conf.rendered.title)
            self._own_ids[str(resp.get("id"))] = time.monotonic()
        except (NtfyError, ValueError) as e:
            LOG.warning("更新确认卡片失败 slot=%s id=%s：%s", conf.slot, conf.msg_id, type(e).__name__)
        try:
            resp = self.client.clear(conf.topic, conf.msg_id)
            self._own_ids[str(resp.get("id"))] = time.monotonic()
        except NtfyError as e:
            LOG.warning("clear 确认卡片失败 slot=%s id=%s：%s", conf.slot, conf.msg_id, type(e).__name__)

    def _cmd_add_slot(self, lang: str) -> dict:
        """新建一个槽位：池子 +1、新 topic 进钥匙串、订阅按新列表重连。新槽位默认未确认（谁都没订阅过）。"""
        try:
            slot = self._state().add_slot()
        except StateError as e:
            return {"event": "error", "kind": "state", "sent": False, "message": e.text(lang)}
        self.restart_subscription()
        LOG.info("新建 slot=%s 池子=%s", slot, len(self._topics))
        return {"event": "added", "slot": slot}

    def _publish_own(self, topic: str, message: str, *, title: str | None = None, actions=None) -> str:
        """所有由 daemon 自己发布的消息都走这里：返回的 id 登记进 _own_ids，订阅流回显它时才认得出不是回复。"""
        resp = self.client.publish(topic, message, title=title, actions=actions)
        msg_id = str(resp.get("id"))
        self._own_ids[msg_id] = time.monotonic()
        return msg_id

    def _finish(self, pend: Pending, event: dict, *, touch_card: bool) -> None:
        """终态：先把事件交给 ask、关连接、立刻移出活跃；然后（按需）改手机上的卡片。"""
        if self._pending.get(pend.slot) is pend:
            del self._pending[pend.slot]
        self._send(pend.conn, event)
        for c in list(self._clients.values()):
            if c.pending is pend:
                self._drop_client(c)
        if not touch_card:
            return
        try:
            if event["event"] == "reply":
                answered = render.render_answered(pend.rendered, event["text"])
                resp = self.client.update(pend.topic, pend.msg_id, answered.message, title=answered.title)
            else:
                prefix = texts.t("prefix.cancelled" if event["event"] == "cancelled" else "prefix.timeout", pend.lang)
                resp = self.client.update(pend.topic, pend.msg_id, pend.rendered.body, title=prefix + pend.rendered.title)
            self._own_ids[str(resp.get("id"))] = time.monotonic()
        except (NtfyError, ValueError) as e:
            LOG.warning("更新卡片失败 slot=%s id=%s：%s", pend.slot, pend.msg_id, type(e).__name__)
        try:
            resp = self.client.clear(pend.topic, pend.msg_id)  # 不论 update 成败都清通知栏
            self._own_ids[str(resp.get("id"))] = time.monotonic()
        except NtfyError as e:
            LOG.warning("clear 失败 slot=%s id=%s：%s", pend.slot, pend.msg_id, type(e).__name__)

    def _expire(self) -> None:
        now = time.monotonic()
        for pend in [p for p in self._pending.values() if p.deadline <= now]:
            LOG.info("提问超时 slot=%s id=%s", pend.slot, pend.msg_id)
            self._finish(pend, {"event": "timeout"}, touch_card=True)
        for conf in [c for c in self._confirming.values() if c.deadline <= now]:
            LOG.info("确认超时 slot=%s id=%s", conf.slot, conf.msg_id)
            self._finish_confirm(conf, {"event": "timeout"}, prefix_key="prefix.timeout")
        cutoff = now - 2 * DEFAULT_TIMEOUT
        self._own_ids = {k: v for k, v in self._own_ids.items() if v > cutoff}

    # ---------------------------------------------------------------- 订阅侧事件（主线程消费）

    def _drain_events(self) -> None:
        while True:
            try:
                ev = self._events.get_nowait()
            except queue.Empty:
                return
            kind = ev.get("event")
            if kind == "_connected":
                self._on_connected()
            elif kind == "_disconnected":
                self._on_disconnected(ev)
            elif kind == "message":
                self._on_message(ev)
            elif kind == "_delivered":
                self._on_delivered(ev)
            # open / keepalive / message_clear / message_delete 等不需要处理

    def _on_message(self, ev: dict) -> None:
        mid = str(ev.get("id"))
        if mid in self._own_ids or ev.get("sequence_id") in self._own_ids:
            return  # 订阅流回显了我们自己发的提问 / 更新
        slot = self._slot_of.get(str(ev.get("topic")))
        if slot is None:
            LOG.info("收到不在池子里的 topic 的消息 id=%s，丢弃", mid)
            return
        text = ev.get("message")
        if not isinstance(text, str):
            text = ""
        mark = inject.parse_control_mark(text)
        if mark is not None and mark[1] == slot:
            # 自己生成的固定标记永远不交给 agent——不论作为回复还是注入。识别放在 pending 判断之前：
            # 回执 ①（无租约）发出后，新 agent 租到同一槽位并 ask，用户此时点旧回执的按钮，
            # 那串标记若走 pending 分支就成了 ask 的回复原文。槽位不符的标记当普通消息。
            if mark[0] == "confirmed":
                self._on_confirmed(slot, mid)
            else:
                self._on_control(slot, mark[0], mid)
            return
        conf = self._confirming.get(slot)
        if conf is not None:
            # 只有按钮点击算确认（要验的是整条通知链路）；此时没有目标 agent 可注，文字只能提醒 CLI——
            # 测试消息还没发出（topic 段）时连提醒都没意义，只记日志
            LOG.info("确认中收到文字 slot=%s id=%s，不算确认", slot, mid)
            if conf.msg_id is not None:
                self._send(conf.conn, {"event": "warning", "message": texts.t("daemon.warning.text_during_confirm", conf.lang, button=texts.t("confirm.button", conf.lang))})
            return
        pend = self._pending.get(slot)
        if pend is None:
            self.deliver(slot, ev)
            return
        LOG.info("回复到达 slot=%s id=%s", slot, mid)
        self._finish(pend, {"event": "reply", "text": text}, touch_card=True)

    # ---------------------------------------------------------------- 无 pending：注入 / 回执 / 控制按钮

    def deliver(self, slot: str, event: dict) -> None:
        """该槽位没有提问在等：这是用户的主动指令，排给注入线程。租约在这里（主线程）读，注入线程不碰状态。"""
        text = event.get("message")
        if not isinstance(text, str):
            text = ""
        mid = str(event.get("id"))
        try:
            leased_by = self._state().slots().get(slot, {}).get("leased_by")
        except StateError as e:
            # 租约文件坏了不能让一条手机消息把 daemon 打死；回执也不带本机路径
            LOG.error("读租约失败 slot=%s id=%s：%s", slot, mid, e)
            self._settle(slot, mid, inject.Outcome(False, "error", None, None, texts.t("receipt.lease_unreadable", self.lang), self.lang))
            return
        LOG.info("无 pending 的消息 slot=%s id=%s，排队注入 target=%s", slot, mid, leased_by)
        self._inflight[mid] = slot
        self._inject_q.put((slot, leased_by, mid, text))

    def _inject_loop(self) -> None:
        """注入线程：只跑 herdr 子进程，结果作为 _delivered 事件回主线程。串行——一个槽位同一时刻只处理一条、按到达顺序。"""
        while True:
            job = self._inject_q.get()
            if job is None:
                return
            slot, leased_by, mid, text = job
            try:  # 关停中也照跑：这条是与主线程的排空竞争到的，跑完在宽限期内回来就能正常落地
                outcome = inject.deliver(slot, leased_by, text, run=self._herdr, lang=self.lang)
            except Exception as e:  # 注入层不该抛；真抛了也不能让线程死掉、让后面的投递永远排队
                LOG.error("注入异常 slot=%s id=%s：%s", slot, mid, type(e).__name__, exc_info=True)
                outcome = inject.Outcome(False, "error", leased_by, None, texts.t("receipt.error", self.lang, error=type(e).__name__), self.lang)
            self._push({"event": "_delivered", "slot": slot, "id": mid, "outcome": outcome})

    def _on_delivered(self, ev: dict) -> None:
        self._inflight.pop(ev["id"], None)
        self._settle(ev["slot"], ev["id"], ev["outcome"])

    def _settle(self, slot: str, mid: str, outcome: inject.Outcome) -> None:
        """一次投递的结局落地：送达只记日志；没送达就发回执，并把该槽位上一张还开着的回执关掉。"""
        if outcome.delivered:
            LOG.info("已注入 slot=%s id=%s target=%s cli=%s", slot, mid, outcome.target, outcome.cli)
            return
        LOG.warning("投递失败 slot=%s id=%s reason=%s target=%s", slot, mid, outcome.reason, outcome.target)
        topic = self._state().topic_of(slot)
        rendered = inject.render_receipt(slot, outcome, reply_url=self.client.topic_url(topic))
        try:
            receipt_id = self._publish_own(topic, rendered.message, title=rendered.title, actions=rendered.actions)  # 不走它会成环：回执回显 → 无 pending → 再回执
        except NtfyError as e:
            LOG.warning("回执发布失败 slot=%s id=%s：%s", slot, mid, type(e).__name__)
            return
        old = self._receipts.pop(slot, None)
        if old is not None:
            # 旧回执的按钮不能继续亮着：标记只带槽位不带回执 id，点旧卡会改到新卡；关掉它，手机上只剩最新那张是活的
            self._close_receipt(topic, old, texts.t("prefix.superseded", old.lang), texts.t("receipt.superseded", old.lang))
        self._receipts[slot] = Receipt(slot=slot, msg_id=receipt_id, rendered=rendered)
        LOG.info("回执已发 slot=%s id=%s", slot, receipt_id)

    def _close_receipt(self, topic: str, receipt: Receipt, prefix: str, result: str) -> None:
        """同 seq 更新成无按钮的关闭态，再 clear 通知栏；两个返回 id 都登记，回显时才认得出不是回复。"""
        closed = inject.render_receipt_closed(receipt.rendered, prefix=prefix, result=result)
        try:
            resp = self.client.update(topic, receipt.msg_id, closed.message, title=closed.title)
            self._own_ids[str(resp.get("id"))] = time.monotonic()
        except (NtfyError, ValueError) as e:
            LOG.warning("更新回执失败 slot=%s id=%s：%s", receipt.slot, receipt.msg_id, type(e).__name__)
        try:
            resp = self.client.clear(topic, receipt.msg_id)  # 不论 update 成败都清通知栏
            self._own_ids[str(resp.get("id"))] = time.monotonic()
        except NtfyError as e:
            LOG.warning("clear 回执失败 slot=%s id=%s：%s", receipt.slot, receipt.msg_id, type(e).__name__)

    def _on_control(self, slot: str, action: str, mid: str) -> None:
        """控制按钮被点：释放 / 忽略。回执在内存里就原地更新它（同 seq）+ clear；不在（daemon 重启过）就发一条无按钮的说明，不能静默。"""
        LOG.info("控制标记 slot=%s id=%s action=%s", slot, mid, action)
        receipt = self._receipts.pop(slot, None)
        lang = receipt.lang if receipt is not None else self.lang  # 有回执就跟回执走，没有（daemon 重启过）就用 daemon 的
        if action == "release":
            prefix, result = self._release_by_button(slot, lang)
        else:
            prefix, result = texts.t("prefix.ignored", lang), texts.t("control.ignored", lang)
        topic = self._state().topic_of(slot)
        if receipt is None:
            try:
                self._publish_own(topic, result, title=prefix + inject.receipt_title(slot, lang))
            except NtfyError as e:
                LOG.warning("控制结果发布失败 slot=%s：%s", slot, type(e).__name__)
            return
        self._close_receipt(topic, receipt, prefix, result)

    def _release_by_button(self, slot: str, lang: str) -> tuple[str, str]:
        """「释放这个槽位」的结果（Title 前缀, 一句话）。文案里不带本机路径：状态层的报错含租约文件路径，那不该上手机。"""
        not_released = texts.t("prefix.not_released", lang)
        if slot in self._pending:
            return not_released, texts.t("control.busy_active", lang, slot=slot)
        if slot in self._confirming:
            return not_released, texts.t("control.busy_confirming", lang, slot=slot)
        try:
            if not self._state().slots().get(slot, {}).get("leased_by"):
                return not_released, texts.t("control.no_lease", lang, slot=slot)
            self._state().release(slot, self._active())
        except StateError as e:
            LOG.error("按钮释放失败 slot=%s：%s", slot, e)
            return not_released, texts.t("control.state_failed", lang, slot=slot)
        LOG.info("释放 slot=%s（手机控制按钮）", slot)
        return texts.t("prefix.released", lang), texts.t("control.released", lang, slot=slot)

    def _on_connected(self) -> None:
        was_down = self._warned_disconnect
        self._connected, self._failures = True, 0
        self._disconnected_since, self._warned_disconnect = None, False
        LOG.info("订阅已连上")
        if was_down:
            self._warn_pending("daemon.warning.reconnected")

    def _on_disconnected(self, ev: dict) -> None:
        if self._connected:
            self._disconnected_since = time.monotonic()
        self._connected = False
        if ev.get("reconnect_failed"):
            self._failures += 1  # 只计重连失败，断线本身不算
        LOG.warning("订阅断开：%s（重连失败 %s 次）", ev.get("kind"), self._failures)
        self._check_disconnect_warning()

    def _check_disconnect_warning(self) -> None:
        """断开到了门槛就进入「告警态」：现有 pending 立刻收到 warning，之后进来的 ask 在 sent 之后也会收到。"""
        if self._connected or self._warned_disconnect:
            return
        down = time.monotonic() - self._disconnected_since if self._disconnected_since else 0.0
        if self._failures >= self.warn_after_failures or down >= self.warn_after_seconds:
            self._warned_disconnect = True
            self._warn_pending("daemon.warning.disconnected", failures=self._failures, seconds=f"{down:.0f}")

    def _warn_pending(self, key: str, **fmt) -> None:
        """给每个正等着的 ask / 确认发一条 warning，各按自己的语言取文案。"""
        for pend in list(self._pending.values()):
            self._send(pend.conn, {"event": "warning", "message": texts.t(key, pend.lang, **fmt)})
        for conf in list(self._confirming.values()):
            if conf.msg_id is not None:  # 测试消息已发出、正等点击的才关心订阅断没断
                self._send(conf.conn, {"event": "warning", "message": texts.t(key, conf.lang, **fmt)})

    # ---------------------------------------------------------------- 订阅线程

    def _subscribe_loop(self) -> None:
        backoff = self.backoff_base
        while not self._stopping:
            gen = self._topics_gen
            topics = list(self._topics)
            since = self._cursor.since()  # 首连时是 None：冷启动不回放
            try:
                sub = self.client.subscribe(topics, since=since)
            except NtfyError as e:
                self._push({"event": "_disconnected", "kind": type(e).__name__, "reconnect_failed": True})
                time.sleep(backoff)
                backoff = min(backoff * 2, self.backoff_max)
                continue
            if self._adopt_subscription(sub, gen):
                sub.close()  # 建连期间 topic 列表变了：这条流订的是旧列表，立刻换
                continue
            backoff = self.backoff_base
            self._push({"event": "_connected"})  # subscribe() 返回就是建连成功（HTTP 200 已到）
            try:
                for ev in sub:
                    if not self._cursor.advance(ev):
                        continue  # 边界秒内回放的重复消息
                    if ev.get("event") == "message":
                        self._push(ev)
            except NtfyClosed:
                if self._stopping:
                    break
                continue  # 主线程要求重连（topic 列表变了），不退避
            except NtfyError as e:
                self._push({"event": "_disconnected", "kind": type(e).__name__})
                time.sleep(backoff)
                backoff = min(backoff * 2, self.backoff_max)
            finally:
                with self._sub_lock:
                    self._sub = None

    def _adopt_subscription(self, sub, gen: int) -> bool:
        """把刚建好的流登记为当前流；返回它是否已经过时（建连期间代次变了）。

        登记与代次判定在同一把锁里：restart_subscription() 也在这把锁里改代次、取当前流，
        所以 restart 要么看见这条流（去关它），要么让这里判成过时（自己关）——没有第三种。
        """
        with self._sub_lock:
            self._sub = sub
            return gen != self._topics_gen

    def _push(self, ev: dict) -> None:
        self._events.put(ev)
        self._wake()

    def restart_subscription(self) -> None:
        """池子变了（新增槽位）：按状态层现在的 topic 列表关掉当前流，订阅线程立即重连。

        正赶上建连中（还没有可关的流）也不会丢：代次 +1 与取当前流在同一把锁里，
        订阅线程采纳那条流时会发现代次变了、自己关掉重来。
        """
        state = self._state()
        self._topics = state.topics()
        self._slot_of = {t: s for s, t in zip(state.slot_names(), self._topics)}
        with self._sub_lock:
            self._topics_gen += 1
            sub = self._sub
        if sub is not None:
            sub.close()
