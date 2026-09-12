#!/usr/bin/env python3
"""agent-ntfy：让任意 AI CLI 经 ntfy.sh 把「需要人拍板的事」推到手机，并把人的裁决带回来。

子命令:
    ask [--timeout 秒]      阻塞提问：从 stdin 读一段 JSON，推到手机，等到回复后把回复原文打到 stdout
    daemon [--detach|--status|--stop]
                            常驻订阅进程（唯一的 ntfy 订阅者）：前台跑 / 脱离会话跑 / 看状态 / 停掉
    slots                   看槽位池与租约状态
    release [<槽位>]        释放租约；不给槽位就释放当前目标（本窗格）租的那个
    confirm-sub <槽位>      可达性确认闸：验「手机收得到通知」（要用户自己在终端跑：会显示 topic 名）
        [--subscribed]        用户已订阅、跳过显示 topic 那段直接发测试通知（agent 代跑用，非终端也行）
        [--show-topic]        只打印 topic 名就退出，不发（⚠️ 会进调用方的输出）
        [--again]             已确认过的槽位重新确认（换手机后）
        [--timeout 秒]        等按钮点击的秒数（默认 600）
    add-slot                新建一个槽位（之后要 confirm-sub）

confirm-sub / release / add-slot 的退出码: 0 成功 / 1 槽位名不对 / 2 超时没点按钮（confirm-sub）/ 3 通道故障（daemon 没跑、状态文件读写失败、发布失败）
    / 4 需要人介入（confirm-sub 在非终端且没给 --subscribed；槽位正忙）/ 130 被 Ctrl-C 中断

ask 的退出码（三种结局不能都表现为空输出）:
    0  拿到回复，stdout 是回复原文（末尾一个换行）
    1  输入校验未通过，消息未发送（stderr 有可照着改的文案）
    2  超时，消息已发送
    3  通道故障：daemon 没在跑 / 连接中途断开 / 向 ntfy 发布失败（stderr 写明是「未发送」还是「已发送但…」）
    4  需要人介入：槽位未过可达性闸 / 全部槽位已租用 / 该目标已有一个提问在等
  130  被 Ctrl-C 中断（stderr 仍说明消息发了没有）

除 daemon 外的子命令都是瘦客户端：经 unix socket 向 daemon 说话（一行 JSON 请求，若干行 JSON 事件），
不读租约文件、不碰钥匙串。

环境变量: AGENT_NTFY_HOME（默认 ~/.agent-ntfy）· HERDR_PANE_ID / HERDR_ENV（在 herdr 里时自动带上窗格标识）
          AGENT_NTFY_TARGET（不在 herdr 里时指定「我是谁」，同一个值复用同一个槽位）
          AGENT_NTFY_LANG（固定文案的语言 zh / en；ask 的 JSON 里给了 lang 以它为准；都没有就 en）

固定文案的语言只在这里解析一次（ask：JSON lang → AGENT_NTFY_LANG → en；其余子命令：AGENT_NTFY_LANG → en），
随请求交给 daemon；深层模块不读环境变量。
"""

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import texts
import validate

PROG = "agent-ntfy"
HOME = Path(os.environ.get("AGENT_NTFY_HOME", "~/.agent-ntfy")).expanduser()
DEFAULT_TIMEOUT = 12 * 3600
EXIT_REPLY, EXIT_INVALID, EXIT_TIMEOUT, EXIT_CHANNEL, EXIT_NEEDS_HUMAN, EXIT_INTERRUPTED = 0, 1, 2, 3, 4, 130
# error 事件的 kind → 退出码
EXIT_BY_KIND = {"invalid_input": EXIT_INVALID, "unknown_slot": EXIT_INVALID, "busy": EXIT_NEEDS_HUMAN, "no_free_slot": EXIT_NEEDS_HUMAN,
                "unconfirmed": EXIT_NEEDS_HUMAN}  # 其余 kind（state / publish_failed / daemon_stopping / bad_request …）都是通道故障 3
CONFIRM_TIMEOUT = 600  # 与 daemon.CONFIRM_TIMEOUT 同步（这里刻意不 import daemon）
STATE_KEYS = ("unassigned", "idle", "active", "confirming")  # daemon 的 slots 事件里 state_key 的取值；显示文案按语言取


def err(msg: str) -> None:
    print(f"{PROG}: {msg}", file=sys.stderr)


class ProtocolError(ValueError):
    """daemon 回的东西不合协议；文案由 CLI 按语言取（cli.protocol.<key>）。"""

    def __init__(self, key: str):
        super().__init__(key)
        self.key = key


class BadEnvLang(ValueError):
    """AGENT_NTFY_LANG 给了却不是 zh / en。响亮失败，不静默回退——语言错了整个进程的文案都会错。"""


def env_lang() -> str:
    """进程入口解析一次：AGENT_NTFY_LANG → en。ask 再拿 JSON 里的 lang 压过它。空串当没给；给了非法值抛 BadEnvLang。"""
    value = os.environ.get(texts.ENV_VAR)
    if value and not texts.is_lang(value):
        raise BadEnvLang(value)
    return texts.resolve(None, value)


def describe(e: BaseException, lang: str) -> str:
    """异常进人读文案：协议错按语言取表，其余（OSError 之类）原样。"""
    return texts.t(f"cli.protocol.{e.key}", lang) if isinstance(e, ProtocolError) else str(e)


def start_hint(lang: str) -> None:
    print(texts.t("cli.start_hint", lang), file=sys.stderr)


# ---------------------------------------------------------------- socket 协议（客户端侧）

def sock_path(home: Path = HOME) -> Path:
    return home / "daemon.sock"


def connect(home: Path = HOME) -> socket.socket:
    """连 daemon；连不上抛 OSError（调用方决定怎么说）。"""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(str(sock_path(home)))
    except OSError:
        s.close()
        raise
    return s


def send_request(sock: socket.socket, req: dict) -> None:
    sock.sendall((json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8"))


def read_events(sock: socket.socket) -> Iterator[dict]:
    """逐行读 daemon 回的事件，直到对端关连接。行不是 JSON 对象就当协议坏了，抛 ValueError。"""
    buf = b""
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            return
        buf += chunk
        while b"\n" in buf:
            line, _, buf = buf.partition(b"\n")
            if not line.strip():
                continue
            ev = json.loads(line.decode("utf-8"))
            if not isinstance(ev, dict):
                raise ProtocolError("not_object")
            yield ev


def identity() -> tuple[str, str | None]:
    """(leased_by, tag)：一个目标一个槽位，这个值决定「谁」在问。

    herdr 内两者都是本窗格 id，不接受覆盖（窗格就是注入目标）。不在 herdr 时 tag 交给 daemon 用槽位名，
    leased_by 先看 AGENT_NTFY_TARGET，没有就用主机名 + 会话 id——不用父进程 pid：agent 的工具壳每次调用
    父进程都不同，那样每问一次就烧掉一个槽位。
    """
    pane = os.environ.get("HERDR_PANE_ID")
    if os.environ.get("HERDR_ENV") and pane:
        return pane, pane
    target = os.environ.get("AGENT_NTFY_TARGET")
    if target:
        return target, None
    return f"host:{socket.gethostname()}|sid:{os.getsid(0)}", None


# ---------------------------------------------------------------- ask

def cmd_ask(args) -> int:
    home = Path(args.home)
    text = sys.stdin.read()
    payload, problems, lang = validate.check_json(text, env_lang())  # 报错与这张卡片都用同一个语言：JSON lang → 环境 → en
    if problems:
        sys.stderr.write(validate.format_problems(problems, lang))
        return EXIT_INVALID
    leased_by, tag = identity()
    try:
        sock = connect(home)
    except OSError as e:
        err(texts.t("cli.connect_failed.not_sent", lang, path=sock_path(home), error=e.strerror or e))
        start_hint(lang)
        return EXIT_CHANNEL
    sent = False
    try:
        send_request(sock, {"cmd": "ask", "payload": payload, "leased_by": leased_by, "tag": tag, "timeout": args.timeout, "lang": lang})
        for ev in read_events(sock):
            kind = ev.get("event")
            if kind == "sent":
                sent = True
            elif kind == "warning":
                err(texts.t("cli.reminder", lang, message=ev.get("message")))
            elif kind == "reply":
                sys.stdout.write(str(ev.get("text", "")) + "\n")
                return EXIT_REPLY
            elif kind == "timeout":
                err(texts.t("cli.ask.timeout", lang, seconds=args.timeout))
                return EXIT_TIMEOUT
            elif kind == "error":
                sent = bool(ev.get("sent", sent))
                err(texts.t("cli.ask.error_sent" if sent else "cli.ask.error_not_sent", lang, message=ev.get("message")))
                if ev.get("kind") == "no_free_slot":
                    for cand in ev.get("candidates") or []:
                        gate = texts.t("cli.ask.candidate.confirmed" if cand.get("subscribed") else "cli.ask.candidate.unconfirmed", lang)
                        err(texts.t("cli.ask.candidate", lang, slot=cand["slot"], gate=gate))
                return EXIT_BY_KIND.get(str(ev.get("kind")), EXIT_CHANNEL)
        err(texts.t("cli.ask.disconnected", lang) + texts.t("cli.ask.disconnected.sent" if sent else "cli.ask.disconnected.not_sent", lang))
        return EXIT_CHANNEL
    except (OSError, ValueError) as e:
        err(texts.t("cli.ask.comm_failed", lang, error=describe(e, lang)) + texts.t("cli.ask.comm_failed.sent" if sent else "cli.ask.disconnected.not_sent", lang))
        return EXIT_CHANNEL
    except KeyboardInterrupt:
        err(texts.t("cli.ask.interrupted", lang) + texts.t("cli.ask.interrupted.sent" if sent else "cli.ask.disconnected.not_sent", lang))
        return EXIT_INTERRUPTED
    finally:
        sock.close()


# ---------------------------------------------------------------- 一问一答的命令

def request(home: Path, req: dict, lang: str) -> dict | None:
    """发一个一问一答的命令（带上语言），返回第一条事件；连不上 daemon 返回 None（已打印提示）。"""
    try:
        with connect(home) as s:
            send_request(s, {**req, "lang": lang})
            return next(read_events(s), {"event": "error", "kind": "protocol", "sent": False, "message": texts.t("cli.no_response", lang)})
    except OSError as e:
        err(texts.t("cli.connect_failed", lang, path=sock_path(home), error=e.strerror or e))
        start_hint(lang)
        return None


def cmd_slots(args) -> int:
    lang = env_lang()
    ev = request(Path(args.home), {"cmd": "slots"}, lang)
    if ev is None:
        return EXIT_CHANNEL
    if ev.get("event") != "slots":
        err(str(ev.get("message")))
        return EXIT_CHANNEL
    width = max(8, *(len(texts.t(f"cli.slots.state.{k}", lang)) for k in STATE_KEYS))  # 列宽装得下该语言最长的状态词
    for slot, rec in ev["slots"].items():
        gate = texts.t("cli.slots.gate.confirmed" if rec.get("subscribed") else "cli.slots.gate.unconfirmed", lang)
        who = texts.t("cli.slots.since", lang, leased_by=rec["leased_by"], leased_at=rec["leased_at"]) if rec.get("leased_by") else ""
        state = texts.t(f"cli.slots.state.{rec['state_key']}", lang) if rec.get("state_key") in STATE_KEYS else rec["state"]
        print(f"{slot:<7} {state:<{width}} {gate}{who}")
    return 0


def cmd_release(args) -> int:
    lang = env_lang()
    req = {"cmd": "release", "slot": args.slot} if args.slot else {"cmd": "release", "leased_by": identity()[0]}
    ev = request(Path(args.home), req, lang)
    if ev is None:
        return EXIT_CHANNEL
    if ev.get("event") != "released":
        err(str(ev.get("message")))
        return EXIT_BY_KIND.get(str(ev.get("kind")), EXIT_CHANNEL)
    print(texts.t("cli.released", lang, slot=ev["slot"]))
    return 0


def cmd_confirm_sub(args) -> int:
    """可达性确认闸。默认两段：先显示 topic 让用户订阅、按回车后才发测试通知，等用户在通知栏点按钮。

    topic 名就是密码：默认只在 stdout 是终端时才显示，agent 代跑（stdout 被捕获）时退出 4 让它转告用户；
    用户已订阅过时 agent 可以带 --subscribed 代跑（不经过显示 topic 那段）。
    """
    home, slot, lang = Path(args.home), args.slot, env_lang()
    if args.show_topic:
        ev = request(home, {"cmd": "confirm-sub", "slot": slot, "show_topic": True}, lang)
        if ev is None:
            return EXIT_CHANNEL
        if ev.get("event") != "topic":
            err(str(ev.get("message")))
            return EXIT_BY_KIND.get(str(ev.get("kind")), EXIT_CHANNEL)
        print(texts.t("cli.confirm.topic", lang, slot=slot, topic=ev["topic"], url=ev["url"]))
        return 0
    if not args.subscribed and not sys.stdout.isatty():
        err(texts.t("cli.confirm.topic_hint", lang, slot=slot))
        return EXIT_NEEDS_HUMAN
    try:
        sock = connect(home)
    except OSError as e:
        err(texts.t("cli.connect_failed", lang, path=sock_path(home), error=e.strerror or e))
        start_hint(lang)
        return EXIT_CHANNEL
    sent = False
    try:
        send_request(sock, {"cmd": "confirm-sub", "slot": slot, "again": args.again, "subscribed": args.subscribed, "timeout": args.timeout, "lang": lang})
        for ev in read_events(sock):
            kind = ev.get("event")
            if kind == "already_confirmed":
                print(texts.t("cli.confirm.already", lang, slot=slot))
                return 0
            elif kind == "topic":
                if args.subscribed:
                    err(texts.t("cli.confirm.protocol", lang))  # 两端各守一道
                    return EXIT_CHANNEL
                print(texts.t("cli.confirm.topic", lang, slot=slot, topic=ev["topic"], url=ev["url"]) + "\n" + texts.t("cli.confirm.guide", lang))
                try:
                    input(texts.t("cli.confirm.enter", lang))
                except EOFError:
                    # stdin 到头（< /dev/null 之类）：没等到回车就不能发——先发再订阅正是两段式要避免的
                    err(texts.t("cli.confirm.no_enter", lang))
                    return EXIT_NEEDS_HUMAN
                try:
                    send_request(sock, {"ready": True})
                except OSError:
                    pass  # 等回车期间 daemon 已经收掉这条（超时 / 停止）：真实终态还在缓冲区里，继续读它，别报成通道故障
            elif kind == "sent":
                sent = True
                err(texts.t("cli.confirm.sent", lang, button=texts.t("confirm.button", lang), seconds=f"{args.timeout:g}"))  # 进度走 stderr：stdout 只在退出 0 时有内容
            elif kind == "warning":
                err(texts.t("cli.reminder", lang, message=ev.get("message")))
            elif kind == "confirmed":
                print(texts.t("cli.confirm.done", lang, slot=slot))
                return 0
            elif kind == "timeout":
                err(texts.t("cli.confirm.timeout" if sent else "cli.confirm.timeout_no_enter", lang, seconds=f"{args.timeout:g}"))
                return EXIT_TIMEOUT
            elif kind == "error":
                err(str(ev.get("message")))
                return EXIT_BY_KIND.get(str(ev.get("kind")), EXIT_CHANNEL)
        err(texts.t("cli.confirm.disconnected", lang))
        return EXIT_CHANNEL
    except (OSError, ValueError) as e:
        err(texts.t("cli.confirm.comm_failed", lang, error=describe(e, lang)))
        return EXIT_CHANNEL
    except KeyboardInterrupt:
        err(texts.t("cli.confirm.interrupted", lang))
        return EXIT_INTERRUPTED
    finally:
        sock.close()


def cmd_add_slot(args) -> int:
    lang = env_lang()
    ev = request(Path(args.home), {"cmd": "add-slot"}, lang)
    if ev is None:
        return EXIT_CHANNEL
    if ev.get("event") != "added":
        err(str(ev.get("message")))
        return EXIT_BY_KIND.get(str(ev.get("kind")), EXIT_CHANNEL)
    print(texts.t("cli.add_slot.done", lang, slot=ev["slot"]))
    return 0


# ---------------------------------------------------------------- daemon 的起停

def cmd_daemon(args) -> int:
    import daemon  # 只有这一支需要它（连带钥匙串与租约文件）

    home, lang = Path(args.home), env_lang()
    if args.status:
        return daemon_status(home, lang)
    if args.stop:
        return daemon_stop(home, lang)
    if args.detach:
        return daemon_detach(home, lang)
    try:
        daemon.Daemon(home, log_to_stderr=True, lang=lang).run()  # daemon 自己的语言在这里解析一次，之后不再看环境
    except daemon.DaemonError as e:
        err(e.text(lang))
        return EXIT_CHANNEL
    return 0


def daemon_status(home: Path, lang: str) -> int:
    import daemon
    pid = daemon.pid_alive(home / "daemon.pid")
    if not pid:
        print(texts.t("cli.status.not_running", lang))
        return 1
    ev = request(home, {"cmd": "status"}, lang)
    if ev is None or ev.get("event") != "status":
        print(texts.t("cli.status.no_socket", lang, pid=pid))
        return 1
    if ev["subscribed"]:
        sub = texts.t("cli.status.sub.connected", lang)
    elif ev["disconnected_for"] is None:
        sub = texts.t("cli.status.sub.connecting", lang)  # 刚起来还没连上，或从没连上过
    else:
        sub = texts.t("cli.status.sub.disconnected", lang, seconds=ev["disconnected_for"])
    print(texts.t("cli.status.line", lang, pid=ev["pid"], sub=sub, pending=ev["pending"], confirming=ev.get("confirming", 0), pool=ev["pool"]))
    return 0


def daemon_stop(home: Path, lang: str) -> int:
    import daemon
    pid = daemon.pid_alive(home / "daemon.pid")
    if not pid:
        print(texts.t("cli.status.not_running", lang))
        return 0
    # pid 文件可能是残留而 pid 被别的进程复用：先经 socket 问一声，对得上再发信号
    ev = request(home, {"cmd": "status"}, lang)
    if ev is None or ev.get("pid") != pid:
        err(texts.t("cli.stop.pid_mismatch", lang, pid=pid, other=ev.get("pid") if ev else texts.t("cli.stop.no_response", lang)))
        return 1
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as e:
        err(texts.t("cli.stop.signal_failed", lang, pid=pid, error=e))
        return 1
    for _ in range(300):  # 关停最坏拖 2 + 5×N 秒（在途注入的宽限期 + 每张未送达回执的发布上限）
        if daemon.pid_alive(home / "daemon.pid") is None:
            print(texts.t("cli.stop.done", lang, pid=pid))
            return 0
        time.sleep(0.1)
    err(texts.t("cli.stop.timeout", lang, pid=pid))
    return 1


def daemon_detach(home: Path, lang: str) -> int:
    """脱离会话起 daemon：新会话、stdio 接 /dev/null；起来后核一次 socket 能连上才算成功。子进程继承环境，语言由它自己再解析。"""
    import daemon
    if daemon.pid_alive(home / "daemon.pid"):
        err(texts.t("cli.detach.already", lang))
        return EXIT_CHANNEL
    # --home 是顶层选项，必须放在子命令前面
    proc = subprocess.Popen([sys.executable, os.path.abspath(__file__), "--home", str(home), "daemon"],
                            start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        time.sleep(0.1)
        if proc.poll() is not None:
            err(texts.t("cli.detach.died", lang, rc=proc.poll(), log=home / "daemon.log"))
            return EXIT_CHANNEL
        try:
            with connect(home) as s:  # 判据是它在 socket 上报出自己的 pid，不是 socket 文件出现
                send_request(s, {"cmd": "status"})
                ev = next(read_events(s), None)
        except (OSError, ValueError):
            continue
        if ev and ev.get("event") == "status" and ev.get("pid") == proc.pid:
            print(texts.t("cli.detach.started", lang, pid=proc.pid, log=home / "daemon.log"))
            return 0
    err(texts.t("cli.detach.not_ready", lang, pid=proc.pid, log=home / "daemon.log"))
    return EXIT_CHANNEL


# ---------------------------------------------------------------- 入口

def positive_seconds_in(lang: str):
    """argparse 的 --timeout 类型：报错文案按语言。"""
    def positive_seconds(text: str) -> float:
        try:
            value = float(text)
        except ValueError:
            raise argparse.ArgumentTypeError(texts.t("help.timeout.not_number", lang, text=repr(text))) from None
        if value <= 0:
            raise argparse.ArgumentTypeError(texts.t("help.timeout.not_positive", lang, text=text))
        return value
    return positive_seconds


def build_parser(lang: str) -> argparse.ArgumentParser:
    """--help 的文案也按进程入口解析出的语言取。"""
    def h(key: str, **fmt) -> str:
        return texts.t(f"help.{key}", lang, **fmt)

    p = argparse.ArgumentParser(prog=PROG, description=h("prog"))
    p.add_argument("--home", default=str(HOME), help=h("home", home=HOME))
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("ask", help=h("ask"))
    a.add_argument("--timeout", type=positive_seconds_in(lang), default=DEFAULT_TIMEOUT, help=h("ask.timeout"))
    a.set_defaults(fn=cmd_ask)
    d = sub.add_parser("daemon", help=h("daemon"))
    g = d.add_mutually_exclusive_group()
    g.add_argument("--detach", action="store_true", help=h("daemon.detach"))
    g.add_argument("--status", action="store_true", help=h("daemon.status"))
    g.add_argument("--stop", action="store_true", help=h("daemon.stop"))
    d.set_defaults(fn=cmd_daemon)
    sub.add_parser("slots", help=h("slots")).set_defaults(fn=cmd_slots)
    r = sub.add_parser("release", help=h("release"))
    r.add_argument("slot", nargs="?", help=h("release.slot"))
    r.set_defaults(fn=cmd_release)
    c = sub.add_parser("confirm-sub", help=h("confirm"))
    c.add_argument("slot", help=h("confirm.slot"))
    c.add_argument("--again", action="store_true", help=h("confirm.again"))
    c.add_argument("--subscribed", action="store_true", help=h("confirm.subscribed"))
    c.add_argument("--show-topic", action="store_true", help=h("confirm.show_topic"))
    c.add_argument("--timeout", type=positive_seconds_in(lang), default=CONFIRM_TIMEOUT, help=h("confirm.timeout"))
    c.set_defaults(fn=cmd_confirm_sub)
    sub.add_parser("add-slot", help=h("add_slot")).set_defaults(fn=cmd_add_slot)
    return p


def main(argv: list[str] | None = None) -> int:
    try:
        lang = env_lang()  # 先于一切：语言错了连 --help 都会错
    except BadEnvLang as e:
        err(texts.t("cli.bad_env_lang", texts.DEFAULT_LANG, value=str(e)))
        return EXIT_INVALID
    args = build_parser(lang).parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
