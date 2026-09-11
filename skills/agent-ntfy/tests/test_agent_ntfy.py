"""CLI 入口：ask 的退出码与 stdout / stderr 形态、其余子命令的输出。进程内跑 main()，daemon 用测试替身，不打真网。"""

import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import agent_ntfy
from tests.test_daemon import Harness, wait_until
from tests.test_render import SAMPLE


def run(argv, stdin_text="", env=None):
    """跑 main()，返回 (退出码, stdout, stderr)。"""
    out, errbuf = io.StringIO(), io.StringIO()
    environ = {k: v for k, v in os.environ.items() if not k.startswith("HERDR_")}
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
        code, out, err = run(["--home", str(h.home), "confirm-sub", "slot1"])
        self.assertEqual(code, 3)
        self.assertIn("尚未提供", err)
        code, out, err = run(["--home", str(h.home), "add-slot"])
        self.assertEqual((code, out), (3, ""))
        self.assertIn("尚未提供", err)

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
                    conn.sendall(b'{"event":"status","pid":4242,"subscribed":true,"disconnected_for":null,"pending":0,"pool":5}\n')
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
                                                                     "disconnected_for": None, "pending": 0, "pool": 5}):
            code, out, err = run(["--home", str(h.home), "daemon", "--status"])
        self.assertEqual(code, 0)
        self.assertIn("连接中", out)
        self.assertNotIn("None", out)


if __name__ == "__main__":
    unittest.main()
