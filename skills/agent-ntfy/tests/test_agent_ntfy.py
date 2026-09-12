"""CLI 入口：ask 的退出码与 stdout / stderr 形态、其余子命令的输出。进程内跑 main()，daemon 用测试替身，不打真网。"""

import contextlib
import io
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import agent_ntfy
import inject
import texts
from tests.test_daemon import Harness, wait_until
from tests.test_render import SAMPLE


def Z(key, **fmt):
    return texts.t(key, "zh", **fmt)


def run(argv, stdin_text="", env=None):
    """跑 main()，返回 (退出码, stdout, stderr)。缺省把固定文案定成 zh（既有用例断言的都是中文）；env 里给 AGENT_NTFY_LANG 可覆盖。"""
    out, errbuf = io.StringIO(), io.StringIO()
    environ = {k: v for k, v in os.environ.items() if not k.startswith("HERDR_") and k != "AGENT_NTFY_LANG"}
    environ["AGENT_NTFY_LANG"] = "zh"
    environ.update(env or {})
    with mock.patch.dict(os.environ, environ, clear=True), mock.patch("sys.stdin", io.StringIO(stdin_text)), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(errbuf):
        code = agent_ntfy.main(argv)
    return code, out.getvalue(), errbuf.getvalue()


HERDR = {"HERDR_ENV": "1", "HERDR_PANE_ID": "wD:p1"}


class IdentityTest(unittest.TestCase):
    def test_inside_herdr_uses_pane_id_for_both(self):
        with mock.patch.dict(os.environ, HERDR):
            self.assertEqual(agent_ntfy.identity(), ("wD:p1", "wD:p1"))

    def test_outside_herdr_is_host_and_session_id_with_no_tag(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("HERDR_") and k != "AGENT_NTFY_TARGET"}
        with mock.patch.dict(os.environ, env, clear=True):
            leased_by, tag = agent_ntfy.identity()
        # 会话 id 而不是父进程 pid：agent 的工具壳每次调用父进程都不同，用它会每问一次烧一个槽位
        self.assertEqual(leased_by, f"host:{os.uname().nodename}|sid:{os.getsid(0)}")
        self.assertNotIn("pid:", leased_by)
        self.assertIsNone(tag)

    def test_outside_herdr_env_override_wins(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("HERDR_")}
        env["AGENT_NTFY_TARGET"] = "my-project"
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(agent_ntfy.identity(), ("my-project", None))

    def test_inside_herdr_ignores_override(self):
        with mock.patch.dict(os.environ, {**HERDR, "AGENT_NTFY_TARGET": "my-project"}):
            self.assertEqual(agent_ntfy.identity(), ("wD:p1", "wD:p1"))


class AskExitCodesTest(unittest.TestCase):
    def reply_when_sent(self, h, text):
        """另一个线程：等 daemon 发布后模拟手机回复。"""
        def go():
            wait_until(lambda: len(h.client.published) == 1)
            h.client.message(h.topic("slot1"), text)
        threading.Thread(target=go, daemon=True).start()

    # 0：拿到回复，stdout 是回复原文 + 一个换行，别的什么都不加
    def test_reply_exit_0(self):
        h = Harness(self)
        self.reply_when_sent(h, "留固定目录")
        code, out, err = run(["--home", str(h.home), "ask"], json.dumps(SAMPLE), HERDR)
        self.assertEqual((code, out), (0, "留固定目录\n"))
        self.assertEqual(err, "")
        self.assertEqual(h.client.published[0]["title"], "[wD:p1] " + SAMPLE["title"])  # leased_by / tag 来自环境
        wait_until(lambda: len(h.client.clears) == 1)

    # 1：校验不过，消息未发送，stdout 空，不需要 daemon 在跑
    def test_invalid_input_exit_1_without_daemon(self):
        code, out, err = run(["--home", "/nonexistent/agent-ntfy-home", "ask"], '{"title": "x",')
        self.assertEqual((code, out), (1, ""))
        self.assertIn("消息未发送", err)
        bad = {**SAMPLE, "options": SAMPLE["options"][:1], "recommend": "nope"}
        code, out, err = run(["--home", "/nonexistent/agent-ntfy-home", "ask"], json.dumps(bad))
        self.assertEqual((code, out), (1, ""))
        self.assertIn("输入校验未通过（2 处）", err)
        self.assertIn("消息未发送", err)

    # 2：超时，消息已发送，stdout 空
    def test_timeout_exit_2(self):
        h = Harness(self)
        code, out, err = run(["--home", str(h.home), "ask", "--timeout", "0.5"], json.dumps(SAMPLE), HERDR)
        self.assertEqual((code, out), (2, ""))
        self.assertIn("已发送", err)
        wait_until(lambda: len(h.client.clears) == 1)

    # 3：daemon 没在跑，stderr 给出启动方式，消息未发送
    def test_no_daemon_exit_3_with_start_hint(self):
        code, out, err = run(["--home", "/nonexistent/agent-ntfy-home", "ask"], json.dumps(SAMPLE))
        self.assertEqual((code, out), (3, ""))
        self.assertIn("消息未发送", err)
        self.assertIn("agent-ntfy daemon --detach", err)
        self.assertIn("herdr", err)

    # 3：daemon 中途停了，stderr 写明「已发送但」
    def test_daemon_stopping_exit_3_says_sent(self):
        h = Harness(self)
        threading.Thread(target=lambda: (wait_until(lambda: len(h.client.published) == 1), h.stop()), daemon=True).start()
        code, out, err = run(["--home", str(h.home), "ask"], json.dumps(SAMPLE), HERDR)
        self.assertEqual((code, out), (3, ""))
        self.assertIn("消息已发送，但", err)

    # 3：向 ntfy 发布失败，未发送
    def test_publish_failed_exit_3_says_not_sent(self):
        h = Harness(self)
        h.client.fail_publish = "HTTP 429"
        code, out, err = run(["--home", str(h.home), "ask"], json.dumps(SAMPLE), HERDR)
        self.assertEqual((code, out), (3, ""))
        self.assertIn("消息未发送", err)

    # 4：需要人介入——未过闸 / 全满 / 已有提问在等
    def test_needs_human_exit_4(self):
        h = Harness(self, subscribed=())
        code, out, err = run(["--home", str(h.home), "ask"], json.dumps(SAMPLE), HERDR)
        self.assertEqual((code, out), (4, ""))
        self.assertIn("confirm-sub slot1", err)
        self.assertIn("消息未发送", err)

    def test_all_leased_exit_4_lists_candidates(self):
        h = Harness(self, pool_size=1, subscribed=("slot1",))
        h.state.acquire("someone-else")
        code, out, err = run(["--home", str(h.home), "ask"], json.dumps(SAMPLE), HERDR)
        self.assertEqual((code, out), (4, ""))
        self.assertIn("slot1", err)
        self.assertIn("已过闸", err)

    def test_busy_exit_4(self):
        h = Harness(self)
        sock, first, events = h.ask(leased_by="wD:p1")
        code, out, err = run(["--home", str(h.home), "ask"], json.dumps(SAMPLE), HERDR)
        self.assertEqual((code, out), (4, ""))
        self.assertIn("等回复", err)
        sock.close()

    # Ctrl-C：不带 traceback，退出码 130（shell 的 SIGINT 惯例），stderr 说明消息发了没有
    def test_keyboard_interrupt_exit_3(self):
        h = Harness(self)
        real = agent_ntfy.read_events

        def interrupt_after_sent(sock):
            for ev in real(sock):
                yield ev
                raise KeyboardInterrupt
        with mock.patch.object(agent_ntfy, "read_events", interrupt_after_sent):
            code, out, err = run(["--home", str(h.home), "ask"], json.dumps(SAMPLE), HERDR)
        self.assertEqual((code, out), (130, ""))
        self.assertIn("中断", err)
        self.assertIn("已发送", err)

    # --timeout 必须是正数：在本地就拦，不用连 daemon
    def test_non_positive_timeout_rejected_locally(self):
        for bad in ("0", "-5"):
            with self.subTest(timeout=bad):
                with self.assertRaises(SystemExit) as cm:
                    run(["--home", "/nonexistent/agent-ntfy-home", "ask", "--timeout", bad], json.dumps(SAMPLE))
                self.assertEqual(cm.exception.code, 2)

    # 订阅断开的提醒走 stderr，不影响 stdout
    def test_warning_goes_to_stderr(self):
        h = Harness(self, backoff_base=0.05, backoff_max=0.1, warn_after_failures=2, warn_after_seconds=999)

        def go():
            wait_until(lambda: len(h.client.published) == 1)
            h.client.fail_subscribe = 2  # 门槛是 2 次重连失败
            h.client.drop()
            h.client.wait_subscription(2)
            h.client.message(h.topic("slot1"), "答")
        threading.Thread(target=go, daemon=True).start()
        code, out, err = run(["--home", str(h.home), "ask"], json.dumps(SAMPLE), HERDR)
        self.assertEqual((code, out), (0, "答\n"))
        self.assertIn("提醒", err)


class OtherCommandsTest(unittest.TestCase):
    def test_slots_release_status_confirm(self):
        h = Harness(self)
        code, out, err = run(["--home", str(h.home), "slots"])
        self.assertEqual(code, 0)
        self.assertIn("slot1", out)
        for t in h.state.topics():
            self.assertNotIn(t, out)
        sock, first, events = h.ask(leased_by="wD:p1")
        h.client.message(h.topic("slot1"), "答")
        next(events)
        sock.close()
        wait_until(lambda: len(h.client.clears) == 1)
        code, out, err = run(["--home", str(h.home), "release"], env=HERDR)  # 不给槽位：释放本窗格租的那个
        self.assertEqual((code, out), (0, "已释放 slot1\n"))
        code, out, err = run(["--home", str(h.home), "release", "slot1"])
        self.assertEqual(code, 3)
        self.assertIn("无需释放", err)
        code, out, err = run(["--home", str(h.home), "daemon", "--status"])
        self.assertEqual(code, 0)
        self.assertIn(f"pid {os.getpid()}", out)
        self.assertIn("已连上", out)
        self.assertIn("确认中：0", out)
        code, out, err = run(["--home", str(h.home), "add-slot"])
        self.assertEqual(code, 0)
        self.assertIn("slot6", out)
        self.assertIn("confirm-sub slot6", out)  # 一步一事：不自动接确认，只指路
        for t in h.store.load() or []:
            self.assertNotIn(t, out)



class ConfirmSubTest(unittest.TestCase):
    """confirm-sub 的三种形态：默认（TTY，两段）· --subscribed（非 TTY 可用）· --show-topic（只看 topic）。"""

    def click_when_sent(self, h, slot, n=1):
        def go():
            wait_until(lambda: len(h.client.published) == n)
            h.client.message(h.topic(slot), inject.control_mark("confirmed", slot))
        threading.Thread(target=go, daemon=True).start()

    def run_on_tty(self, argv, stdin_text="\n", stdin=None):
        """stdout 接一个伪终端：isatty() 为真。返回 (退出码, 终端上打印的文本, stderr)。stdin 给了对象就用它（可做门控）。"""
        master, slave = os.openpty()
        tty_out = os.fdopen(slave, "w", encoding="utf-8", buffering=1)
        captured = []

        def pump():
            while True:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    return
                if not chunk:
                    return
                captured.append(chunk)

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        errbuf = io.StringIO()
        environ = {k: v for k, v in os.environ.items() if not k.startswith("HERDR_") and k != "AGENT_NTFY_LANG"}
        environ["AGENT_NTFY_LANG"] = "zh"
        try:
            with mock.patch.dict(os.environ, environ, clear=True), mock.patch("sys.stdin", stdin or io.StringIO(stdin_text)), \
                    mock.patch("sys.stdout", tty_out), contextlib.redirect_stderr(errbuf):
                code = agent_ntfy.main(argv)
        finally:
            tty_out.close()
            reader.join(2)
            os.close(master)
        return code, b"".join(captured).decode("utf-8", errors="replace"), errbuf.getvalue()

    def test_default_on_non_tty_exits_4_without_touching_daemon(self):
        h = Harness(self, subscribed=())
        code, out, err = run(["--home", str(h.home), "confirm-sub", "slot4", "--timeout", "1"])  # run() 的 stdout 是 StringIO，不是 TTY
        self.assertEqual((code, out), (4, ""))
        self.assertIn("在你自己的终端跑", err)
        self.assertIn("agent-ntfy confirm-sub slot4", err)
        self.assertIn("--subscribed", err)
        for t in h.store.load() or []:
            self.assertNotIn(t, err)
        self.assertEqual(h.client.published, [])
        self.assertEqual(h.request(cmd="status")[0]["confirming"], 0)

    def test_default_on_tty_prints_topic_waits_for_enter_then_confirms(self):
        h = Harness(self, subscribed=())
        self.click_when_sent(h, "slot4")
        code, out, err = self.run_on_tty(["--home", str(h.home), "confirm-sub", "slot4", "--timeout", "5"], stdin_text="\n")
        self.assertEqual(code, 0, err)
        self.assertIn(h.topic("slot4"), out)  # topic 名只在这里出现
        self.assertIn(h.client.topic_url(h.topic("slot4")), out)
        self.assertIn(Z("cli.confirm.guide"), out)
        self.assertIn("✅", out)
        self.assertTrue(h.state.slots()["slot4"]["subscribed"])
        self.assertEqual(h.client.published[0]["title"], "[slot4] 确认你能收到通知")

    def test_default_on_tty_does_not_send_before_enter(self):
        h = Harness(self, subscribed=())
        enter = threading.Event()

        class GatedStdin(io.StringIO):
            def readline(self, *a):  # input() 会走到这里：用户还没按回车就一直等
                enter.wait(10)
                return "\n"

        result = {}

        def go():
            result["r"] = self.run_on_tty(["--home", str(h.home), "confirm-sub", "slot4", "--timeout", "5"], stdin=GatedStdin())

        t = threading.Thread(target=go, daemon=True)
        t.start()
        wait_until(lambda: h.request(cmd="status")[0]["confirming"] == 1, what="进入确认中")
        time.sleep(0.3)
        self.assertEqual(h.client.published, [])  # 用户还在订阅：没按回车之前不能发
        self.click_when_sent(h, "slot4")
        enter.set()
        t.join(10)
        code, out, err = result["r"]
        self.assertEqual(code, 0, err)
        self.assertEqual(len(h.client.published), 1)

    def test_timeout_while_waiting_for_enter_exits_2_with_the_right_words(self):
        h = Harness(self, subscribed=())

        class SlowStdin(io.StringIO):
            def readline(self, *a):  # 用户在 topic 段停留超过 timeout 才按回车
                time.sleep(1.2)
                return "\n"

        code, out, err = self.run_on_tty(["--home", str(h.home), "confirm-sub", "slot4", "--timeout", "0.5"], stdin=SlowStdin())
        self.assertEqual(code, 2, err)  # 是超时，不是「通道故障 Broken pipe」
        self.assertIn("还没发出", err)  # 测试通知根本没发过，不能说「没收到按钮点击」
        self.assertNotIn("Broken pipe", err)
        self.assertEqual(h.client.published, [])

    def test_stdin_eof_on_tty_cancels_instead_of_sending(self):
        h = Harness(self, subscribed=())
        code, out, err = self.run_on_tty(["--home", str(h.home), "confirm-sub", "slot4", "--timeout", "5"], stdin=io.StringIO(""))
        self.assertEqual(code, 4, err)
        self.assertIn("回车", err)
        time.sleep(0.2)
        self.assertEqual(h.client.published, [])  # 没等到回车就不发：先发再订阅正是两段式要避免的
        self.assertEqual(h.request(cmd="status")[0]["confirming"], 0)

    def test_subscribed_flag_works_without_tty_and_never_prints_topic(self):
        h = Harness(self, subscribed=())
        self.click_when_sent(h, "slot4")
        code, out, err = run(["--home", str(h.home), "confirm-sub", "slot4", "--subscribed", "--timeout", "5"])
        self.assertEqual(code, 0, err)
        self.assertIn("✅", out)
        for t in h.store.load() or []:
            self.assertNotIn(t, out + err)
        self.assertTrue(h.state.slots()["slot4"]["subscribed"])

    def test_show_topic_prints_and_exits_0_without_sending(self):
        h = Harness(self)
        code, out, err = run(["--home", str(h.home), "confirm-sub", "slot1", "--show-topic"])
        self.assertEqual(code, 0, err)
        self.assertIn(h.topic("slot1"), out)
        self.assertIn(h.client.topic_url(h.topic("slot1")), out)
        self.assertEqual(h.client.published, [])

    def test_already_confirmed_exits_0_and_mentions_again(self):
        h = Harness(self)
        code, out, err = run(["--home", str(h.home), "confirm-sub", "slot1", "--subscribed", "--timeout", "1"])
        self.assertEqual(code, 0, err)
        self.assertIn("--again", out)
        self.assertEqual(h.client.published, [])

    def test_timeout_exits_2(self):
        h = Harness(self, subscribed=())
        code, out, err = run(["--home", str(h.home), "confirm-sub", "slot4", "--subscribed", "--timeout", "0.5"])
        self.assertEqual((code, out), (2, ""))  # 进度行走 stderr：stdout 只在退出 0 时有内容
        self.assertIn("README", err)
        self.assertIn("已发出", err)
        self.assertFalse(h.state.slots()["slot4"]["subscribed"])

    def test_busy_exits_4(self):
        h = Harness(self)
        sock, first, events = h.ask(leased_by="wD:p1")  # slot1 活跃
        code, out, err = run(["--home", str(h.home), "confirm-sub", "slot1", "--subscribed", "--again"])
        self.assertEqual(code, 4)
        self.assertIn("等回复", err)
        sock.close()

    def test_publish_failure_exits_3_with_stdout_empty(self):
        h = Harness(self, subscribed=())
        h.client.fail_publish = "ntfy 不通"
        code, out, err = run(["--home", str(h.home), "confirm-sub", "slot4", "--subscribed", "--timeout", "5"])
        self.assertEqual((code, out), (3, ""))
        self.assertIn("发布", err)

    def test_topic_event_under_subscribed_is_a_protocol_error(self):
        h = Harness(self, subscribed=())
        with mock.patch("agent_ntfy.read_events", lambda sock: iter([{"event": "topic", "topic": "not-a-real-topic", "url": "x"}])):
            code, out, err = run(["--home", str(h.home), "confirm-sub", "slot4", "--subscribed", "--timeout", "5"])
        self.assertEqual((code, out), (3, ""))  # 说了不要 topic 还收到：两端各守一道，topic 名不打印
        self.assertNotIn("not-a-real-topic", out + err)
        self.assertIn("协议", err)

    def test_unknown_slot_exits_1_but_state_failure_exits_3(self):
        h = Harness(self)
        code, out, err = run(["--home", str(h.home), "confirm-sub", "slot9", "--subscribed"])
        self.assertEqual((code, out), (1, ""))  # 槽位名打错是输入错，不是通道故障
        self.assertIn("slot9", err)
        code, out, err = run(["--home", str(h.home), "confirm-sub", "slot9", "--show-topic"])
        self.assertEqual(code, 1)
        code, out, err = run(["--home", str(h.home), "release", "slot9"])
        self.assertEqual(code, 1)
        (h.home / "leases.json").write_text("{not json", encoding="utf-8")
        code, out, err = run(["--home", str(h.home), "release", "slot1"])
        self.assertEqual(code, 3)
        code, out, err = run(["--home", str(h.home), "add-slot"])
        self.assertEqual(code, 3)

    def test_no_daemon_exits_3_with_start_hint(self):
        code, out, err = run(["--home", "/nonexistent/agent-ntfy-home", "confirm-sub", "slot1", "--subscribed"])
        self.assertEqual((code, out), (3, ""))
        self.assertIn("daemon 没在跑", err)

    # --detach 起子进程的参数顺序：--home 是顶层选项，必须在子命令前面（放后面 argparse 直接退 2，daemon 根本没起）
    def test_detach_spawns_child_with_home_before_subcommand(self):
        import socket as socketmod
        import subprocess
        import shutil
        h = tempfile.mkdtemp(prefix="an-")  # 短路径：unix socket 路径限 104 字节
        self.addCleanup(shutil.rmtree, h, ignore_errors=True)
        recorded = {}

        class FakePopen:
            pid = 4242

            def __init__(self, argv, **kw):
                recorded["argv"] = argv
                recorded["session"] = kw.get("start_new_session")
                srv = socketmod.socket(socketmod.AF_UNIX, socketmod.SOCK_STREAM)
                srv.bind(str(Path(h) / "daemon.sock"))
                srv.listen(1)
                recorded["srv"] = srv

                def serve():  # 像 daemon 一样回一条 status，pid 就是这个「子进程」的
                    conn, _ = srv.accept()
                    conn.recv(4096)
                    conn.sendall(b'{"event":"status","pid":4242,"subscribed":true,"disconnected_for":null,"pending":0,"confirming":0,"pool":5}\n')
                    conn.close()
                threading.Thread(target=serve, daemon=True).start()

            def poll(self):
                return None

        with mock.patch.object(subprocess, "Popen", FakePopen):
            code, out, err = run(["--home", h, "daemon", "--detach"])
        recorded["srv"].close()
        self.assertEqual(code, 0, err)
        argv = recorded["argv"]
        self.assertLess(argv.index("--home"), argv.index("daemon"))
        self.assertEqual(argv[argv.index("--home") + 1], h)
        self.assertTrue(recorded["session"])
        self.assertIn("pid 4242", out)

    def test_commands_without_daemon(self):
        code, out, err = run(["--home", "/nonexistent/agent-ntfy-home", "slots"])
        self.assertEqual(code, 3)
        self.assertIn("agent-ntfy daemon", err)
        code, out, err = run(["--home", "/nonexistent/agent-ntfy-home", "daemon", "--status"])
        self.assertEqual((code, out), (1, "daemon：未运行\n"))

    # 刚起来还没连上 ntfy 时，--status 不能打出「断开 None 秒」
    def test_status_before_first_connection_says_connecting(self):
        h = Harness(self)
        with mock.patch.object(agent_ntfy, "request", return_value={"event": "status", "pid": os.getpid(), "subscribed": False,
                                                                     "disconnected_for": None, "pending": 0, "confirming": 0, "pool": 5}):
            code, out, err = run(["--home", str(h.home), "daemon", "--status"])
        self.assertEqual(code, 0)
        self.assertIn("连接中", out)
        self.assertNotIn("None", out)


if __name__ == "__main__":
    unittest.main()
