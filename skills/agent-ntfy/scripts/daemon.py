"""常驻 daemon：唯一的 ntfy 订阅者、唯一的状态持有者；其余子命令经 unix socket 向它说话。

    agent → ask ──unix socket──▶ daemon ──SSE──▶ ntfy.sh ──▶ 手机
                ◀──────────────         ◀─────────────────── 回复 / 指令

只做通路：把提问送到手机、把人的话原样送回 ask；不解释内容、不代答。

文件都在 AGENT_NTFY_HOME（默认 ~/.agent-ntfy/，0700）：leases.json · daemon.sock · daemon.pid · daemon.log（0600）。
日志只写槽位名 / 消息 id / 事件类型 / 错误类别——topic 是密码，正文与回复是用户的项目信息，都不落日志。

socket 协议是 JSON Lines：客户端连上后发一行 {"cmd": ...}，daemon 回若干行事件（每行一个 JSON 对象）。
ask 的连接保持到终态（reply / timeout / error）：daemon 若死了，连接当场断开，ask 立刻失败——这就是它的响亮信号；
其余命令一问一答即关。error 事件必带 sent：调用方要据此知道消息发出去了没有。

线程模型：主线程用 selectors 跑一切状态变化（socket、pending、active、租约）；订阅线程只把 ntfy 事件放进队列，
再往唤醒管道写一个字节。游标（最后事件的 time + 边界秒内已见 id）由订阅线程独占，主线程只读它做 status。
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

import render
import validate
from ntfyclient import NtfyClient, NtfyClosed, NtfyError
from state import LEASES_PATH, Lease, NeedsUserDecision, SecretStore, State, StateError

HOME_DIR = LEASES_PATH.parent
DEFAULT_TIMEOUT = 12 * 3600  # ntfy 缓存时长：手机离线超过这个时长就收不到了，再等没有意义
BACKOFF_BASE = 1.0
BACKOFF_MAX = 30.0
WARN_AFTER_FAILURES = 3  # 连续重连失败这么多次，或
WARN_AFTER_SECONDS = 60.0  # 断开这么久 ⇒ 向每个 pending 的 ask 发 warning，不许静默重试
TIMEOUT_PREFIX = "⌛ 已超时 · "
CANCELLED_PREFIX = "⚠️ 已取消 · "  # ask 那端先断开（Ctrl-C / 被 kill）：没人等了，卡片按超时同款收掉
MAX_REQUEST_BYTES = 1024 * 1024  # 一行请求的上限：本地 0600 socket 威胁不大，但不能让一个不发换行的客户端把内存吃光
SEND_TIMEOUT = 5.0  # 往客户端写事件的阻塞上限
LOG = logging.getLogger("agent-ntfy.daemon")


class DaemonError(Exception):
    """启动前就能判定的失败：已有实例在跑、目录不可用。"""


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
    rendered: render.Rendered
    msg_id: str
    deadline: float
    warned: bool = False


@dataclass
class Client:
    """一条 socket 连接的读缓冲。"""

    sock: socket.socket
    buf: bytes = b""
    pending: Pending | None = None


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
                 pool_size: int | None = None, log_to_stderr: bool = False,
                 backoff_base: float = BACKOFF_BASE, backoff_max: float = BACKOFF_MAX,
                 warn_after_failures: int = WARN_AFTER_FAILURES, warn_after_seconds: float = WARN_AFTER_SECONDS):
        self.home = Path(home)
        self.paths = paths(self.home)
        self.client = client or NtfyClient()
        self._store = store
        self._pool_size = pool_size
        self.log_to_stderr = log_to_stderr
        self.backoff_base, self.backoff_max = backoff_base, backoff_max
        self.warn_after_failures, self.warn_after_seconds = warn_after_failures, warn_after_seconds
        self.state: State | None = None
        self._topics: list[str] = []
        self._slot_of: dict[str, str] = {}
        self._pending: dict[str, Pending] = {}  # slot → Pending；键集合就是「活跃」
        self._own_ids: dict[str, float] = {}  # 我们自己发布的消息 id → 发布时刻；订阅流会回显它们，不是回复
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
            raise DaemonError(f"起不了 socket {self.paths['sock']}：{e}。unix socket 路径有长度上限（macOS 104 字节），"
                              "换一个短一点的 AGENT_NTFY_HOME") from e
        except StateError as e:
            LOG.error("启动失败：%s: %s", type(e).__name__, e)  # state 层的报错文案不含 topic
            self._abort()
            raise DaemonError(f"状态初始化失败：{e}") from e
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                signal.signal(sig, lambda *_: self.stop())
        self._thread = threading.Thread(target=self._subscribe_loop, name="ntfy-subscriber", daemon=True)
        self._thread.start()
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
        LOG.setLevel(logging.INFO)
        fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        os.close(os.open(self.paths["log"], os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600))  # 先以 0600 建好再交给 FileHandler
        handlers: list[logging.Handler] = [logging.FileHandler(self.paths["log"], encoding="utf-8")]
        if self.log_to_stderr:
            handlers.append(logging.StreamHandler(sys.stderr))
        for h in handlers:
            h.setFormatter(fmt)
            LOG.addHandler(h)
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
                raise DaemonError(f"已有 daemon 在跑（{sock_path} 连得上），不起第二个")
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
        LOG.info("daemon 收尾：pending=%s", len(self._pending))
        for pend in list(self._pending.values()):
            # 不改手机上的卡片：daemon 停了不代表用户不会回，重启后那条回复走无 pending 分支
            self._finish(pend, {"event": "error", "kind": "daemon_stopping", "sent": True,
                                "message": "daemon 正在停止；提问已发出，用户之后的回复会以指令形式送达"}, touch_card=False)
        with self._sub_lock:
            sub = self._sub
        if sub is not None:
            sub.close()
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
            LOG.removeHandler(h)
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
        soonest = min((p.deadline for p in self._pending.values()), default=now + 5.0)
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
            self._drop_client(c)
            return
        c.buf += data
        if c.pending is not None:
            c.buf = b""
            return  # ask 期间客户端不该再发东西，收到也忽略
        if b"\n" not in c.buf:
            if len(c.buf) > MAX_REQUEST_BYTES:
                self._send(c.sock, {"event": "error", "kind": "bad_request", "sent": False,
                                    "message": f"请求超过 {MAX_REQUEST_BYTES} 字节还没见到换行"})
                self._drop_client(c)
            return
        line, _, c.buf = c.buf.partition(b"\n")
        try:
            req = json.loads(line.decode("utf-8"))
            if not isinstance(req, dict):
                raise ValueError("不是 JSON 对象")
        except (ValueError, UnicodeDecodeError) as e:
            self._send(c.sock, {"event": "error", "kind": "bad_request", "sent": False, "message": f"请求不是一行 JSON 对象：{e}"})
            self._drop_client(c)
            return
        self._dispatch(c, req)

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
            self._reply_once(c, {"event": "slots", "slots": self._state().slots(self._active())})
        elif cmd == "release":
            self._reply_once(c, self._cmd_release(req))
        elif cmd == "status":
            self._reply_once(c, self._status())
        elif cmd in ("confirm-sub", "add-slot"):
            self._reply_once(c, {"event": "error", "kind": "not_implemented", "sent": False, "message": f"{cmd} 尚未提供"})
        else:
            self._reply_once(c, {"event": "error", "kind": "bad_request", "sent": False, "message": f"未知命令：{cmd!r}"})

    def _reply_once(self, c: Client, event: dict) -> None:
        self._send(c.sock, event)
        self._drop_client(c)

    def _state(self) -> State:
        assert self.state is not None
        return self.state

    def _active(self) -> set[str]:
        return set(self._pending)

    def _status(self) -> dict:
        return {"event": "status", "pid": os.getpid(), "subscribed": self._connected,
                "disconnected_for": None if self._connected or self._disconnected_since is None
                else round(time.monotonic() - self._disconnected_since, 1),
                "pending": len(self._pending), "pool": len(self._topics), "cursor": self._cursor.since()}

    def _cmd_release(self, req: dict) -> dict:
        slot = req.get("slot")
        leased_by = req.get("leased_by")
        if not slot and leased_by:
            slot = next((s for s, rec in self._state().slots().items() if rec["leased_by"] == leased_by), None)
            if slot is None:
                return {"event": "error", "kind": "no_lease", "sent": False, "message": "这个目标没有租着任何槽位"}
        if not isinstance(slot, str):
            return {"event": "error", "kind": "bad_request", "sent": False, "message": "release 要给 slot 或 leased_by"}
        if slot in self._pending:
            return {"event": "error", "kind": "active", "sent": False, "message": f"槽位 {slot} 正有提问等回复，不能释放"}
        try:
            self._state().release(slot, self._active())
        except StateError as e:
            return {"event": "error", "kind": "state", "sent": False, "message": str(e)}
        LOG.info("释放 slot=%s", slot)
        return {"event": "released", "slot": slot}

    # ---------------------------------------------------------------- ask

    def _cmd_ask(self, c: Client, req: dict) -> None:
        payload = req.get("payload")
        leased_by = req.get("leased_by")
        tag = req.get("tag")
        timeout = req.get("timeout", DEFAULT_TIMEOUT)
        if not isinstance(leased_by, str) or not leased_by or not isinstance(timeout, (int, float)) or timeout <= 0:
            self._reply_once(c, {"event": "error", "kind": "bad_request", "sent": False, "message": "ask 要给非空 leased_by 与正数 timeout"})
            return
        problems = validate.check(payload)  # ask 已在本地校验过；这里再核一次（长度）作防御，不重复报文案
        if problems:
            self._reply_once(c, {"event": "error", "kind": "invalid_input", "sent": False,
                                 "message": "输入校验未通过：" + "；".join(f"{p.field}: {p.message}" for p in problems)})
            return
        assert isinstance(payload, dict)
        state = self._state()
        slots = state.slots()  # 这次 ask 里租约文件只读这一次；acquire() 内部会再读一次，那是状态层的事
        existing = next((s for s, rec in slots.items() if rec["leased_by"] == leased_by), None)
        if existing in self._pending:
            self._reply_once(c, {"event": "error", "kind": "busy", "sent": False,
                                 "message": f"这个目标已有一个提问在 {existing} 上等回复，先等它结束"})
            return
        try:
            if existing:
                # 同一个目标复用自己的租约（一个目标一个 topic），不刷新 leased_at——那是租约起点，不是上次提问时间
                lease = Lease(slot=existing, topic=state.topic_of(existing), subscribed=slots[existing]["subscribed"])
            else:
                lease = state.acquire(leased_by, self._active())
        except NeedsUserDecision as e:
            cands = [{"slot": s, "subscribed": slots[s]["subscribed"]} for s in e.candidates]
            self._reply_once(c, {"event": "error", "kind": "no_free_slot", "sent": False, "candidates": cands,
                                 "message": "全部槽位已租用。两条路：agent-ntfy release <slot>（替换一个空闲槽位，然后重试）"
                                            "或 agent-ntfy add-slot（新建）；"
                                            + ("可替换：" + ", ".join(f"{x['slot']}{'（已过闸）' if x['subscribed'] else '（未过闸，要再动一次手机）'}" for x in cands)
                                               if cands else "没有可替换的空闲槽位，只剩新建")})
            return
        except StateError as e:
            self._reply_once(c, {"event": "error", "kind": "state", "sent": False, "message": str(e)})
            return
        if not lease.subscribed:
            self._reply_once(c, {"event": "error", "kind": "unconfirmed", "sent": False,
                                 "message": f"槽位 {lease.slot} 还没确认过「手机收得到通知」。先在手机订阅它的 topic，"
                                            f"再跑 agent-ntfy confirm-sub {lease.slot}，然后重试"})
            return
        if not isinstance(tag, str) or not tag:
            tag = lease.slot
        tag = tag.encode("utf-8")[:render.TAG_MAX_BYTES].decode("utf-8", errors="ignore")  # tag 是通路自己的东西，超了截不报错
        rendered = render.render_question(payload, tag=tag, reply_url=self.client.topic_url(lease.topic))
        try:
            msg_id = self._publish_own(lease.topic, rendered.message, title=rendered.title, actions=rendered.actions)
        except NtfyError as e:
            LOG.warning("发布失败 slot=%s：%s", lease.slot, type(e).__name__)
            self._reply_once(c, {"event": "error", "kind": "publish_failed", "sent": False, "message": f"向 ntfy 发布失败：{e}"})
            return
        pend = Pending(slot=lease.slot, topic=lease.topic, leased_by=leased_by, conn=c.sock, rendered=rendered,
                       msg_id=msg_id, deadline=time.monotonic() + float(timeout))
        self._pending[lease.slot] = pend
        c.pending = pend
        LOG.info("提问已发 slot=%s id=%s timeout=%ss", lease.slot, msg_id, timeout)
        self._send(c.sock, {"event": "sent", "slot": lease.slot, "id": msg_id})
        if self._warned_disconnect:  # 订阅正断着：发布走 HTTP 照样通，但回复此刻收不到，得让它知道
            self._send(c.sock, {"event": "warning", "message": "ntfy 订阅目前断开、仍在重连；提问已发出，回复要等恢复后回放"})

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
                prefix = CANCELLED_PREFIX if event["event"] == "cancelled" else TIMEOUT_PREFIX
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
            # open / keepalive / message_clear / message_delete 等不需要处理

    def _on_message(self, ev: dict) -> None:
        mid = str(ev.get("id"))
        if mid in self._own_ids or ev.get("sequence_id") in self._own_ids:
            return  # 订阅流回显了我们自己发的提问 / 更新
        slot = self._slot_of.get(str(ev.get("topic")))
        if slot is None:
            LOG.info("收到不在池子里的 topic 的消息 id=%s，丢弃", mid)
            return
        pend = self._pending.get(slot)
        if pend is None:
            self.deliver(slot, ev)
            return
        text = ev.get("message")
        if not isinstance(text, str):
            text = ""
        LOG.info("回复到达 slot=%s id=%s", slot, mid)
        self._finish(pend, {"event": "reply", "text": text}, touch_card=True)

    def deliver(self, slot: str, event: dict) -> None:
        """该槽位没有提问在等：这是用户的主动指令。目前只记日志并丢弃，注入与投递失败回执由注入层提供。"""
        LOG.info("无 pending 的消息 slot=%s id=%s（丢弃）", slot, event.get("id"))

    def _on_connected(self) -> None:
        was_down = self._warned_disconnect
        self._connected, self._failures = True, 0
        self._disconnected_since, self._warned_disconnect = None, False
        LOG.info("订阅已连上")
        if was_down:
            self._warn_pending("ntfy 订阅已恢复")

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
            self._warn_pending(f"ntfy 订阅已断开（连续失败 {self._failures} 次，{down:.0f} 秒），仍在重连；期间到达的回复要等恢复后回放")

    def _warn_pending(self, message: str) -> None:
        for pend in list(self._pending.values()):
            self._send(pend.conn, {"event": "warning", "message": message})

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
