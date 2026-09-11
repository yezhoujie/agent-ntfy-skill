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

import validate

PROG = "agent-ntfy"
HOME = Path(os.environ.get("AGENT_NTFY_HOME", "~/.agent-ntfy")).expanduser()
DEFAULT_TIMEOUT = 12 * 3600
EXIT_REPLY, EXIT_INVALID, EXIT_TIMEOUT, EXIT_CHANNEL, EXIT_NEEDS_HUMAN, EXIT_INTERRUPTED = 0, 1, 2, 3, 4, 130
# error 事件的 kind → 退出码
EXIT_BY_KIND = {"invalid_input": EXIT_INVALID, "unknown_slot": EXIT_INVALID, "busy": EXIT_NEEDS_HUMAN, "no_free_slot": EXIT_NEEDS_HUMAN,
                "unconfirmed": EXIT_NEEDS_HUMAN}  # 其余 kind（state / publish_failed / daemon_stopping / bad_request …）都是通道故障 3
CONFIRM_TIMEOUT = 600  # 与 daemon.CONFIRM_TIMEOUT 同步（这里刻意不 import daemon）
TOPIC_HINT = ("topic 名只在你自己的终端里显示：请在你自己的终端跑  agent-ntfy confirm-sub {slot}\n"
              "  用户已经在手机上订阅过就加 --subscribed（不显示 topic，非终端也能跑）；只想看 topic 名用 --show-topic（会进调用方的输出）")
SUBSCRIBE_GUIDE = "在手机 ntfy app 里订阅上面这个 topic；订阅好后按回车，我会发一条带按钮的测试通知——看到它弹出来、点按钮，确认就完成了。"
START_HINT = ("daemon 没在跑。启动方式：\n"
              "  herdr 内 ：另开一个 pane 跑  agent-ntfy daemon      （可见、herdr 管生命周期）\n"
              "  非 herdr ：agent-ntfy daemon --detach              （脱离会话，靠 --status / --stop 管）\n"
              "  ⚠️ 别用 agent 自己内部的 shell 或后台任务起它——agent 一退出它就跟着没了")


def err(msg: str) -> None:
    print(f"{PROG}: {msg}", file=sys.stderr)


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
                raise ValueError("daemon 回的不是 JSON 对象")
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
    payload, problems = validate.check_json(text)
    if problems:
        sys.stderr.write(validate.format_problems(problems))
        return EXIT_INVALID
    leased_by, tag = identity()
    try:
        sock = connect(home)
    except OSError as e:
        err(f"连不上 daemon（{sock_path(home)}：{e.strerror or e}）。消息未发送。")
        print(START_HINT, file=sys.stderr)
        return EXIT_CHANNEL
    sent = False
    try:
        send_request(sock, {"cmd": "ask", "payload": payload, "leased_by": leased_by, "tag": tag, "timeout": args.timeout})
        for ev in read_events(sock):
            kind = ev.get("event")
            if kind == "sent":
                sent = True
            elif kind == "warning":
                err(f"提醒：{ev.get('message')}")
            elif kind == "reply":
                sys.stdout.write(str(ev.get("text", "")) + "\n")
                return EXIT_REPLY
            elif kind == "timeout":
                err(f"{args.timeout} 秒内没有收到回复（消息已发送，用户之后的回复会以指令形式送达）")
                return EXIT_TIMEOUT
            elif kind == "error":
                sent = bool(ev.get("sent", sent))
                err((f"消息已发送，但 {ev.get('message')}" if sent else f"消息未发送：{ev.get('message')}"))
                if ev.get("kind") == "no_free_slot":
                    for cand in ev.get("candidates") or []:
                        err(f"  可替换：{cand['slot']}（{'已过闸' if cand.get('subscribed') else '未过闸，选它要再动一次手机'}）")
                return EXIT_BY_KIND.get(str(ev.get("kind")), EXIT_CHANNEL)
        err("daemon 连接中断" + ("（消息已发送，回复无法再送回本次调用）" if sent else "（消息未发送）"))
        return EXIT_CHANNEL
    except (OSError, ValueError) as e:
        err(f"与 daemon 通信失败：{e}" + ("（消息已发送）" if sent else "（消息未发送）"))
        return EXIT_CHANNEL
    except KeyboardInterrupt:
        err("已中断" + ("（消息已发送；用户之后的回复会以指令形式送达）" if sent else "（消息未发送）"))
        return EXIT_INTERRUPTED
    finally:
        sock.close()


# ---------------------------------------------------------------- 一问一答的命令

def request(home: Path, req: dict) -> dict | None:
    """发一个一问一答的命令，返回第一条事件；连不上 daemon 返回 None（已打印提示）。"""
    try:
        with connect(home) as s:
            send_request(s, req)
            return next(read_events(s), {"event": "error", "kind": "protocol", "sent": False, "message": "daemon 没有回应"})
    except OSError as e:
        err(f"连不上 daemon（{sock_path(home)}：{e.strerror or e}）")
        print(START_HINT, file=sys.stderr)
        return None


def cmd_slots(args) -> int:
    ev = request(Path(args.home), {"cmd": "slots"})
    if ev is None:
        return EXIT_CHANNEL
    if ev.get("event") != "slots":
        err(str(ev.get("message")))
        return EXIT_CHANNEL
    for slot, rec in ev["slots"].items():
        gate = "已过闸" if rec.get("subscribed") else "未过闸"
        who = f"  {rec['leased_by']}  自 {rec['leased_at']}" if rec.get("leased_by") else ""
        print(f"{slot:<7} {rec['state']:<8} {gate}{who}")
    return 0


def cmd_release(args) -> int:
    req = {"cmd": "release", "slot": args.slot} if args.slot else {"cmd": "release", "leased_by": identity()[0]}
    ev = request(Path(args.home), req)
    if ev is None:
        return EXIT_CHANNEL
    if ev.get("event") != "released":
        err(str(ev.get("message")))
        return EXIT_BY_KIND.get(str(ev.get("kind")), EXIT_CHANNEL)
    print(f"已释放 {ev['slot']}")
    return 0


def cmd_confirm_sub(args) -> int:
    """可达性确认闸。默认两段：先显示 topic 让用户订阅、按回车后才发测试通知，等用户在通知栏点按钮。

    topic 名就是密码：默认只在 stdout 是终端时才显示，agent 代跑（stdout 被捕获）时退出 4 让它转告用户；
    用户已订阅过时 agent 可以带 --subscribed 代跑（不经过显示 topic 那段）。
    """
    home, slot = Path(args.home), args.slot
    if args.show_topic:
        ev = request(home, {"cmd": "confirm-sub", "slot": slot, "show_topic": True})
        if ev is None:
            return EXIT_CHANNEL
        if ev.get("event") != "topic":
            err(str(ev.get("message")))
            return EXIT_BY_KIND.get(str(ev.get("kind")), EXIT_CHANNEL)
        print(f"{slot} 的 topic：{ev['topic']}\n订阅地址：{ev['url']}")
        return 0
    if not args.subscribed and not sys.stdout.isatty():
        err(TOPIC_HINT.format(slot=slot))
        return EXIT_NEEDS_HUMAN
    try:
        sock = connect(home)
    except OSError as e:
        err(f"连不上 daemon（{sock_path(home)}：{e.strerror or e}）")
        print(START_HINT, file=sys.stderr)
        return EXIT_CHANNEL
    sent = False
    try:
        send_request(sock, {"cmd": "confirm-sub", "slot": slot, "again": args.again, "subscribed": args.subscribed, "timeout": args.timeout})
        for ev in read_events(sock):
            kind = ev.get("event")
            if kind == "already_confirmed":
                print(f"{slot} 已经确认过手机收得到通知，不用再做；换了手机要重新确认就加 --again")
                return 0
            elif kind == "topic":
                if args.subscribed:
                    err("协议错误：说了 --subscribed 却收到 topic 事件；不打印它。daemon 与 CLI 版本可能不一致")  # 两端各守一道
                    return EXIT_CHANNEL
                print(f"{slot} 的 topic：{ev['topic']}\n订阅地址：{ev['url']}\n{SUBSCRIBE_GUIDE}")
                try:
                    input("订阅好了就按回车…")
                except EOFError:
                    # stdin 到头（< /dev/null 之类）：没等到回车就不能发——先发再订阅正是两段式要避免的
                    err("没等到回车（stdin 已到头），确认取消。请在你自己的终端交互式地跑；用户已订阅过就用 --subscribed")
                    return EXIT_NEEDS_HUMAN
                try:
                    send_request(sock, {"ready": True})
                except OSError:
                    pass  # 等回车期间 daemon 已经收掉这条（超时 / 停止）：真实终态还在缓冲区里，继续读它，别报成通道故障
            elif kind == "sent":
                sent = True
                err(f"测试通知已发出，请在手机通知栏点「我收到了」（{args.timeout:g} 秒内）…")  # 进度走 stderr：stdout 只在退出 0 时有内容
            elif kind == "warning":
                err(f"提醒：{ev.get('message')}")
            elif kind == "confirmed":
                print(f"✅ {slot} 已确认：手机收得到通知，之后 agent 可以用它提问了")
                return 0
            elif kind == "timeout":
                if sent:
                    err(f"{args.timeout:g} 秒内没有收到按钮点击，确认未完成。通知没弹出来？按 README 的排查清单检查后再跑一次")
                else:
                    err(f"{args.timeout:g} 秒内没等到回车，确认未完成；测试通知还没发出。订阅好之后再跑一次")
                return EXIT_TIMEOUT
            elif kind == "error":
                err(str(ev.get("message")))
                return EXIT_BY_KIND.get(str(ev.get("kind")), EXIT_CHANNEL)
        err("daemon 连接中断，确认未完成")
        return EXIT_CHANNEL
    except (OSError, ValueError) as e:
        err(f"与 daemon 通信失败：{e}")
        return EXIT_CHANNEL
    except KeyboardInterrupt:
        err("已中断，确认未完成")
        return EXIT_INTERRUPTED
    finally:
        sock.close()


def cmd_add_slot(args) -> int:
    ev = request(Path(args.home), {"cmd": "add-slot"})
    if ev is None:
        return EXIT_CHANNEL
    if ev.get("event") != "added":
        err(str(ev.get("message")))
        return EXIT_BY_KIND.get(str(ev.get("kind")), EXIT_CHANNEL)
    print(f"已新建 {ev['slot']}（还没确认过手机收得到通知）。下一步：在你自己的终端跑  agent-ntfy confirm-sub {ev['slot']}")
    return 0


# ---------------------------------------------------------------- daemon 的起停

def cmd_daemon(args) -> int:
    import daemon  # 只有这一支需要它（连带钥匙串与租约文件）

    home = Path(args.home)
    if args.status:
        return daemon_status(home)
    if args.stop:
        return daemon_stop(home)
    if args.detach:
        return daemon_detach(home)
    try:
        daemon.Daemon(home, log_to_stderr=True).run()
    except daemon.DaemonError as e:
        err(str(e))
        return EXIT_CHANNEL
    return 0


def daemon_status(home: Path) -> int:
    import daemon
    pid = daemon.pid_alive(home / "daemon.pid")
    if not pid:
        print("daemon：未运行")
        return 1
    ev = request(home, {"cmd": "status"})
    if ev is None or ev.get("event") != "status":
        print(f"daemon：pid {pid} 活着，但 socket 无回应")
        return 1
    if ev["subscribed"]:
        sub = "已连上"
    elif ev["disconnected_for"] is None:
        sub = "连接中"  # 刚起来还没连上，或从没连上过
    else:
        sub = f"断开 {ev['disconnected_for']} 秒"
    print(f"daemon：pid {ev['pid']}  订阅：{sub}  等待中的提问：{ev['pending']}  确认中：{ev.get('confirming', 0)}  槽位：{ev['pool']}")
    return 0


def daemon_stop(home: Path) -> int:
    import daemon
    pid = daemon.pid_alive(home / "daemon.pid")
    if not pid:
        print("daemon：未运行")
        return 0
    # pid 文件可能是残留而 pid 被别的进程复用：先经 socket 问一声，对得上再发信号
    ev = request(home, {"cmd": "status"})
    if ev is None or ev.get("pid") != pid:
        err(f"pid 文件指向 {pid}，但 socket 上的 daemon 不是它（{ev.get('pid') if ev else '无回应'}），不发信号；请手动核实")
        return 1
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as e:
        err(f"向 pid {pid} 发 SIGTERM 失败：{e}")
        return 1
    for _ in range(300):  # 关停最坏拖 2 + 5×N 秒（在途注入的宽限期 + 每张未送达回执的发布上限）
        if daemon.pid_alive(home / "daemon.pid") is None:
            print(f"daemon：pid {pid} 已停")
            return 0
        time.sleep(0.1)
    err(f"daemon pid {pid} 30 秒内没退出")
    return 1


def daemon_detach(home: Path) -> int:
    """脱离会话起 daemon：新会话、stdio 接 /dev/null；起来后核一次 socket 能连上才算成功。"""
    import daemon
    if daemon.pid_alive(home / "daemon.pid"):
        err("已有 daemon 在跑")
        return EXIT_CHANNEL
    # --home 是顶层选项，必须放在子命令前面
    proc = subprocess.Popen([sys.executable, os.path.abspath(__file__), "--home", str(home), "daemon"],
                            start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        time.sleep(0.1)
        if proc.poll() is not None:
            err(f"daemon 没有起来（退出码 {proc.poll()}），看 {home / 'daemon.log'}")
            return EXIT_CHANNEL
        try:
            with connect(home) as s:  # 判据是它在 socket 上报出自己的 pid，不是 socket 文件出现
                send_request(s, {"cmd": "status"})
                ev = next(read_events(s), None)
        except (OSError, ValueError):
            continue
        if ev and ev.get("event") == "status" and ev.get("pid") == proc.pid:
            print(f"daemon：已在后台启动，pid {proc.pid}（日志 {home / 'daemon.log'}）")
            return 0
    err(f"daemon（pid {proc.pid}）5 秒内还没就绪，仍在启动（钥匙串弹窗？）；稍后用 agent-ntfy daemon --status 看，日志 {home / 'daemon.log'}")
    return EXIT_CHANNEL


# ---------------------------------------------------------------- 入口

def positive_seconds(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"不是数字：{text!r}")
    if value <= 0:
        raise argparse.ArgumentTypeError(f"要大于 0 秒：{text}")
    return value


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=PROG, description="经 ntfy.sh 把需要人拍板的事推到手机，并把裁决带回来")
    p.add_argument("--home", default=str(HOME), help=f"状态目录（默认 {HOME}）")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("ask", help="阻塞提问，JSON 从 stdin 读")
    a.add_argument("--timeout", type=positive_seconds, default=DEFAULT_TIMEOUT, help="等回复的秒数（默认 12 小时）")
    a.set_defaults(fn=cmd_ask)
    d = sub.add_parser("daemon", help="常驻订阅进程")
    g = d.add_mutually_exclusive_group()
    g.add_argument("--detach", action="store_true", help="脱离会话在后台跑")
    g.add_argument("--status", action="store_true")
    g.add_argument("--stop", action="store_true")
    d.set_defaults(fn=cmd_daemon)
    sub.add_parser("slots", help="看槽位池与租约").set_defaults(fn=cmd_slots)
    r = sub.add_parser("release", help="释放租约")
    r.add_argument("slot", nargs="?")
    r.set_defaults(fn=cmd_release)
    c = sub.add_parser("confirm-sub", help="可达性确认闸：验该槽位手机收得到通知（默认要在终端跑，会显示 topic 名）")
    c.add_argument("slot")
    c.add_argument("--again", action="store_true", help="已确认过的槽位重新确认（换手机后）")
    c.add_argument("--subscribed", action="store_true", help="用户已订阅：不显示 topic，直接发测试通知（非终端也能跑）")
    c.add_argument("--show-topic", action="store_true", help="只打印 topic 名就退出，不发测试通知（会进调用方的输出）")
    c.add_argument("--timeout", type=positive_seconds, default=CONFIRM_TIMEOUT, help="等按钮点击的秒数（默认 600）")
    c.set_defaults(fn=cmd_confirm_sub)
    sub.add_parser("add-slot", help="新建一个槽位").set_defaults(fn=cmd_add_slot)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
