"""CLI 入口：ask 的退出码与 stdout / stderr 形态、其余子命令的输出。进程内跑 main()，daemon 用测试替身，不打真网。"""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import agent_ntfy
import inject
import ipc
import platform_
import projstate
import tests.test_inject as ti
import texts
from inject import HerdrResult
from tests.test_daemon import Harness, wait_until
from tests.test_inject import FakeHerdr, herdr_error
from tests.test_render import NOTIFY, SAMPLE


def Z(key, **fmt):
    return texts.t(key, "zh", **fmt)


CWD = object()  # run(root=CWD)：不钉项目根，让 CLI 从真实 cwd 解析（专门验这条解析路径的用例才用）


def run(argv, stdin_text="", env=None, root=None):
    """跑 main()，返回 (退出码, stdout, stderr)。缺省把固定文案定成 zh（既有用例断言的都是中文）；env 里给 AGENT_NTFY_LANG 可覆盖。

    项目根缺省钉在一个一次性的临时目录：身份 / tag / 状态文件都以它为准，测试进程落在哪个仓里、那个仓开没开远程模式
    都不影响结果，也不会把那个仓的状态文件改掉。root 给了目录就钉在那；给 CWD 才走真实的 cwd 解析。
    """
    out, errbuf = io.StringIO(), io.StringIO()
    environ = {k: v for k, v in os.environ.items() if not k.startswith("HERDR_") and k not in ("AGENT_NTFY_LANG", "AGENT_NTFY_TARGET")}
    environ["AGENT_NTFY_LANG"] = "zh"
    environ.update(env or {})
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.dict(os.environ, environ, clear=True))
        stack.enter_context(mock.patch("sys.stdin", io.StringIO(stdin_text)))
        stack.enter_context(contextlib.redirect_stdout(out))
        stack.enter_context(contextlib.redirect_stderr(errbuf))
        if root is None:
            root = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="proj-"))).resolve()
        if root is not CWD:
            stack.enter_context(mock.patch.object(projstate, "project_root", return_value=Path(root)))
        code = agent_ntfy.main(argv)
    return code, out.getvalue(), errbuf.getvalue()


def temp_root(case):
    """一个临时项目根（短路径、已 resolve）。"""
    root = Path(tempfile.mkdtemp(prefix="proj-")).resolve()
    case.addCleanup(shutil.rmtree, root, ignore_errors=True)
    return root


def owner(root):
    """项目根对应的租约主体。"""
    return f"proj:{root}"


HERDR = {"HERDR_ENV": "1", "HERDR_PANE_ID": "wD:p1"}


class IdentityTest(unittest.TestCase):
    """identity() = Identity(leased_by, pane, tag)：租约主体是项目，pane 只在 herdr 里才有，tag 是项目目录名。"""

    def setUp(self):
        self.root = temp_root(self)
        patcher = mock.patch.object(projstate, "project_root", return_value=self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def env(self, **extra):
        base = {k: v for k, v in os.environ.items() if not k.startswith("HERDR_") and k != "AGENT_NTFY_TARGET"}
        return mock.patch.dict(os.environ, {**base, **extra}, clear=True)

    def test_outside_herdr_owner_is_the_project_and_tag_its_dir_name(self):
        with self.env():
            ident = agent_ntfy.identity()
        self.assertEqual(ident, (owner(self.root), None, self.root.name))
        self.assertEqual((ident.leased_by, ident.pane, ident.tag), (owner(self.root), None, self.root.name))
        self.assertNotIn("sid:", ident.leased_by)  # 不再是主机名 + 会话 id

    def test_inside_herdr_adds_pane_but_owner_is_still_the_project(self):
        with self.env(**HERDR):
            self.assertEqual(agent_ntfy.identity(), (owner(self.root), "wD:p1", self.root.name))

    def test_pane_needs_herdr_env_not_just_a_pane_id(self):
        with self.env(HERDR_PANE_ID="wD:p1"):  # 残留的变量：不在 herdr 里就没有注入目标
            self.assertIsNone(agent_ntfy.identity().pane)
        with self.env(HERDR_ENV="1"):
            self.assertIsNone(agent_ntfy.identity().pane)

    def test_target_override_wins_inside_and_outside_herdr(self):
        with self.env(AGENT_NTFY_TARGET="my-project"):
            self.assertEqual(agent_ntfy.identity(), ("my-project", None, self.root.name))
        with self.env(AGENT_NTFY_TARGET="my-project", **HERDR):
            self.assertEqual(agent_ntfy.identity(), ("my-project", "wD:p1", self.root.name))

    def test_tag_falls_back_to_owner_when_root_has_no_name(self):
        with mock.patch.object(projstate, "project_root", return_value=Path("/")), self.env():
            ident = agent_ntfy.identity()
        self.assertEqual(ident.tag, ident.leased_by)


class RequireConfirmedTest(unittest.TestCase):
    """require_confirmed()：本项目开着远程模式（state.json 的 away 恰为 true）才为真。"""

    def setUp(self):
        self.root = temp_root(self)
        patcher = mock.patch.object(projstate, "project_root", return_value=self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_false_without_dir_or_switch(self):
        self.assertFalse(agent_ntfy.require_confirmed())  # 没启用过
        projstate.ensure(self.root)
        self.assertFalse(agent_ntfy.require_confirmed())  # 目录在、文件不在
        projstate.save(self.root, away=False)
        self.assertFalse(agent_ntfy.require_confirmed())
        projstate.save(self.root, away="true")  # 脏值不算开
        self.assertFalse(agent_ntfy.require_confirmed())

    def test_true_when_away_is_on(self):
        projstate.save(self.root, away=True)
        self.assertTrue(agent_ntfy.require_confirmed())


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
        root = temp_root(self)
        self.reply_when_sent(h, "留固定目录")
        code, out, err = run(["--home", str(h.home), "ask"], json.dumps(SAMPLE), HERDR, root=root)
        self.assertEqual((code, out), (0, "留固定目录\n"))
        self.assertEqual(err, "")
        self.assertEqual(h.client.published[0]["title"], f"[{root.name}] " + SAMPLE["title"])  # tag 是项目目录名
        rec = h.state.slots()["slot1"]
        self.assertEqual((rec["leased_by"], rec["pane"]), (owner(root), "wD:p1"))  # 租约主体是项目，窗格来自环境
        wait_until(lambda: len(h.client.clears) == 1)

    # 远程模式开着：只用已过闸的槽位——池里没有就退 4 报「全满」，不去租一个未过闸的
    def test_away_on_requires_confirmed_slot(self):
        h = Harness(self, pool_size=2, subscribed=("slot1",))
        h.state.acquire("proj:/w/other")  # 唯一已过闸的被别的项目占着
        root = temp_root(self)
        projstate.save(root, away=True)
        code, out, err = run(["--home", str(h.home), "ask"], json.dumps(SAMPLE), HERDR, root=root)
        self.assertEqual((code, out), (4, ""))
        self.assertIn("slot1", err)
        self.assertNotIn("slot2", err)  # 未过闸的不在候选里
        self.assertEqual(h.state.slots()["slot2"]["leased_by"], None)
        projstate.save(root, away=False)  # 关掉就照旧：租 slot2，提示去过闸
        code, out, err = run(["--home", str(h.home), "ask"], json.dumps(SAMPLE), HERDR, root=root)
        self.assertEqual(code, 4)
        self.assertIn("confirm-sub slot2", err)

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
        root = temp_root(self)
        sock, first, events = h.ask(leased_by=owner(root))  # 同一项目（别的窗格 / 会话）已有提问挂着
        code, out, err = run(["--home", str(h.home), "ask"], json.dumps(SAMPLE), HERDR, root=root)
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

    # cwd 已被删（项目根定不出来）：退 3 + 一句人读的「未发送」，不是 traceback、更不是冒充「输入无效」的退 1
    def test_unresolvable_project_root_exits_3_not_traceback(self):
        h = Harness(self)
        gone = FileNotFoundError(2, "No such file or directory")
        with mock.patch.object(Path, "cwd", side_effect=gone):
            code, out, err = run(["--home", str(h.home), "ask"], json.dumps(SAMPLE), HERDR, root=CWD)
        self.assertEqual((code, out), (3, ""))
        self.assertIn("消息未发送", err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(h.client.published, [])
        for argv in (["slots"], ["release"]):
            with mock.patch.object(Path, "cwd", side_effect=gone):
                code, out, err = run(["--home", str(h.home), *argv], root=CWD)
            self.assertEqual((code, out), (3, ""), argv)
            self.assertNotIn("Traceback", err)
            self.assertIn("No such file", err)
        with mock.patch.object(Path, "cwd", side_effect=gone):
            code, out, err = run(["--home", str(h.home), "release", "slot1"], root=CWD)  # 指名槽位：用不着项目根
        self.assertEqual(code, 3)  # slot1 没租约 ⇒ 无需释放（既有语义），不是项目根的错
        self.assertIn("无需释放", err)

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


class NotifyExitCodesTest(unittest.TestCase):
    """notify 的退出码：0 已发 · 1 输入不合法 · 3 通道故障 · 4 需要人。没有 2——它不等回复。"""

    # 0：发出去了，stdout 一行说明发到了哪个槽位；状态文件同 ask 回写；不占「提问中」
    def test_sent_exit_0(self):
        h = Harness(self)
        root = temp_root(self)
        projstate.ensure(root)
        code, out, err = run(["--home", str(h.home), "notify"], json.dumps(NOTIFY), HERDR, root=root)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, Z("cli.notify.sent", slot="slot1") + "\n")
        pub = h.client.published[0]
        self.assertEqual((pub["title"], pub["actions"]), (f"[{root.name}] " + NOTIFY["title"], []))
        rec = h.state.slots()["slot1"]
        self.assertEqual((rec["leased_by"], rec["pane"]), (owner(root), "wD:p1"))
        st = projstate.load(root)
        self.assertEqual((st["slot"], st["confirmed"], st["target"]), ("slot1", True, owner(root)))
        self.assertEqual(h.request(cmd="status")[0]["pending"], 0)

    # 0：提问挂着时也能通报进展
    def test_sent_while_own_question_is_pending(self):
        h = Harness(self)
        root = temp_root(self)
        sock, first, events = h.ask(leased_by=owner(root))
        code, out, err = run(["--home", str(h.home), "notify"], json.dumps(NOTIFY), HERDR, root=root)
        self.assertEqual((code, err), (0, ""))
        self.assertIn(first["slot"], out)
        sock.close()

    # 1：校验不过，消息未发送，stdout 空，不需要 daemon 在跑；抬头点名 notify
    def test_invalid_input_exit_1_without_daemon(self):
        code, out, err = run(["--home", "/nonexistent/agent-ntfy-home", "notify"], '{"title": "x"}')
        self.assertEqual((code, out), (1, ""))
        self.assertIn("agent-ntfy notify", err)
        self.assertIn("消息未发送", err)
        self.assertIn("body", err)
        code, out, err = run(["--home", "/nonexistent/agent-ntfy-home", "notify"], json.dumps({**NOTIFY, "body": "字" * 2000}))
        self.assertEqual(code, 1)
        self.assertIn("body", err)
        code, out, err = run(["--home", "/nonexistent/agent-ntfy-home", "notify"], '{"title":')
        self.assertEqual((code, out), (1, ""))

    # 3：daemon 没在跑 / 发布失败，stderr 写明「消息未发送」
    def test_channel_failure_exit_3_says_not_sent(self):
        code, out, err = run(["--home", "/nonexistent/agent-ntfy-home", "notify"], json.dumps(NOTIFY))
        self.assertEqual((code, out), (3, ""))
        self.assertIn("消息未发送", err)
        self.assertIn("agent-ntfy daemon --detach", err)
        h = Harness(self)
        h.client.fail_publish = "HTTP 429"
        code, out, err = run(["--home", str(h.home), "notify"], json.dumps(NOTIFY), HERDR)
        self.assertEqual((code, out), (3, ""))
        self.assertIn("消息未发送", err)

    # 4：未过闸 / 离席且租不到已过闸槽位
    def test_needs_human_exit_4(self):
        h = Harness(self, subscribed=())
        root = temp_root(self)
        projstate.ensure(root)
        code, out, err = run(["--home", str(h.home), "notify"], json.dumps(NOTIFY), HERDR, root=root)
        self.assertEqual((code, out), (4, ""))
        self.assertIn("confirm-sub slot1", err)
        self.assertIn("消息未发送", err)
        self.assertEqual((projstate.load(root)["slot"], projstate.load(root)["confirmed"]), ("slot1", False))  # 让 agent 知道该确认哪个
        h2 = Harness(self, pool_size=2, subscribed=("slot1",))
        h2.state.acquire("proj:/w/other")
        root2 = temp_root(self)
        projstate.save(root2, away=True)
        code, out, err = run(["--home", str(h2.home), "notify"], json.dumps(NOTIFY), HERDR, root=root2)
        self.assertEqual((code, out), (4, ""))
        self.assertIn("slot1", err)
        self.assertIn("已过闸", err)
        self.assertNotIn("slot2", err)

    # 3：daemon 回的东西不合协议——退 3 + 「消息未发送」，不是 traceback 退 1
    def test_protocol_error_exit_3_says_not_sent(self):
        h = Harness(self)

        def broken(sock):
            raise agent_ntfy.ProtocolError("not_object")
            yield  # noqa: unreachable，只为让它是生成器

        with mock.patch.object(agent_ntfy, "read_events", broken):
            code, out, err = run(["--home", str(h.home), "notify"], json.dumps(NOTIFY), HERDR)
        self.assertEqual((code, out), (3, ""))
        self.assertIn("消息未发送", err)
        self.assertNotIn("Traceback", err)

    # 没有 --timeout：它不等回复（子命令本身在，只是没这个选项）
    def test_no_timeout_option(self):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"AGENT_NTFY_LANG": "zh"}), contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as cm:
            agent_ntfy.main(["notify", "--help"])
        self.assertEqual(cm.exception.code, 0)
        self.assertNotIn("--timeout", out.getvalue())
        with self.assertRaises(SystemExit) as cm:
            run(["--home", "/nonexistent/agent-ntfy-home", "notify", "--timeout", "5"], json.dumps(NOTIFY))
        self.assertEqual(cm.exception.code, 2)


class OtherCommandsTest(unittest.TestCase):
    def test_slots_release_status_confirm(self):
        h = Harness(self)
        root = temp_root(self)
        code, out, err = run(["--home", str(h.home), "slots"])
        self.assertEqual(code, 0)
        self.assertIn("slot1", out)
        for t in h.state.topics():
            self.assertNotIn(t, out)
        sock, first, events = h.ask(leased_by=owner(root))
        h.client.message(h.topic("slot1"), "答")
        next(events)
        sock.close()
        wait_until(lambda: len(h.client.clears) == 1)
        code, out, err = run(["--home", str(h.home), "release"], env=HERDR, root=root)  # 不给槽位：释放本项目租的那个
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

    # slots 每行：租约主体打完整值（proj:<路径> 可复制），窗格有就加在行尾；顺手把本项目租约的窗格刷新成当前窗格
    def test_slots_prints_full_owner_and_pane_and_refreshes_own_pane(self):
        h = Harness(self)
        root = temp_root(self)
        h.state.acquire("proj:/w/other/very/long/path", pane="wX:p9")
        h.state.acquire(owner(root), pane="wD:p1")
        h.state.acquire("host:old|sid:1")  # 升级前留下的旧租约：没有窗格，照常显示
        code, out, err = run(["--home", str(h.home), "slots"], env={**HERDR, "HERDR_PANE_ID": "wD:p2"}, root=root)
        self.assertEqual((code, err), (0, ""))
        lines = out.splitlines()
        self.assertIn("proj:/w/other/very/long/path", lines[0])
        self.assertTrue(lines[0].endswith("wX:p9"), lines[0])
        self.assertIn(owner(root), lines[1])
        self.assertTrue(lines[1].endswith("wD:p2"), lines[1])  # 刷新成本次跑命令的窗格
        self.assertEqual(h.state.slots()["slot2"]["pane"], "wD:p2")
        self.assertIn("host:old|sid:1", lines[2])
        self.assertNotIn("None", lines[2])
        self.assertEqual(h.state.slots()["slot1"]["pane"], "wX:p9")  # 别的项目的窗格不动
        code, out, err = run(["--home", str(h.home), "slots"], root=root)  # 不在 herdr 里跑：本项目的窗格清空
        self.assertEqual(code, 0)
        self.assertIsNone(h.state.slots()["slot2"]["pane"])
        self.assertFalse(out.splitlines()[1].rstrip().endswith("wD:p2"))


class ConfirmSubTest(unittest.TestCase):
    """confirm-sub 的三种形态：默认（TTY，两段）· --subscribed（非 TTY 可用）· --show-topic（只看 topic）。"""

    def click_when_sent(self, h, slot, n=1):
        def go():
            wait_until(lambda: len(h.client.published) == n)
            h.client.message(h.topic(slot), inject.control_mark("confirmed", slot))
        threading.Thread(target=go, daemon=True).start()

    def run_on_tty(self, argv, stdin_text="\n", stdin=None, env=None):
        """stdout 当成终端：isatty() 为真（替身，不开伪终端）。返回 (退出码, 终端上打印的文本, stderr)。stdin 给了对象就用它（可做门控）；
        env 里给的键覆盖（HERDR_* 缺省被滤掉，要模拟在窗格里就从这里给）。"""
        tty_out, errbuf = io.StringIO(), io.StringIO()
        environ = {k: v for k, v in os.environ.items() if not k.startswith("HERDR_") and k != "AGENT_NTFY_LANG"}
        environ["AGENT_NTFY_LANG"] = "zh"
        environ.update(env or {})
        with mock.patch.dict(os.environ, environ, clear=True), mock.patch("sys.stdin", stdin or io.StringIO(stdin_text)), \
                mock.patch("sys.stdout", tty_out), mock.patch.object(tty_out, "isatty", return_value=True), contextlib.redirect_stderr(errbuf):
            code = agent_ntfy.main(argv)
        return code, tty_out.getvalue(), errbuf.getvalue()

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

    # 自动开的窗格带 --close-pane：确认成功后问一句「关闭这个窗格？[Y/n]」——回车 / y 关（herdr pane close 当前窗格），n 保留；
    # 失败 / 超时不问；不带旗标不问；不在 herdr 窗格里不问
    def _confirm_on_tty(self, h, argv, stdin_text, *, env=None):
        fake = FakeHerdr()
        with mock.patch("agent_ntfy.herdr_run", fake):
            code, out, err = self.run_on_tty(argv, stdin_text=stdin_text, env={**HERDR, **(env or {})})
        return code, out, err, fake

    def test_close_pane_flag_closes_pane_on_enter_after_success(self):
        h = Harness(self, subscribed=())
        self.click_when_sent(h, "slot4")
        code, out, err, fake = self._confirm_on_tty(h, ["--home", str(h.home), "confirm-sub", "slot4", "--timeout", "5", "--close-pane"], "\n\n")
        self.assertEqual(code, 0, err)
        self.assertIn(Z("cli.confirm.close_pane").strip(), out)
        self.assertEqual([c[1:] for c in fake.calls], [["pane", "close", "wD:p1"]])  # 关的是自己所在的窗格

    def test_close_pane_flag_keeps_pane_on_n(self):
        h = Harness(self, subscribed=())
        self.click_when_sent(h, "slot4")
        code, out, err, fake = self._confirm_on_tty(h, ["--home", str(h.home), "confirm-sub", "slot4", "--timeout", "5", "--close-pane"], "\nn\n")
        self.assertEqual(code, 0, err)
        self.assertIn(Z("cli.confirm.pane_kept"), out)
        self.assertEqual(fake.calls, [])

    def test_close_pane_flag_does_not_ask_after_failure(self):
        h = Harness(self, subscribed=())
        code, out, err, fake = self._confirm_on_tty(h, ["--home", str(h.home), "confirm-sub", "slot4", "--timeout", "0.5", "--close-pane"], "\n\n")
        self.assertEqual(code, 2, err)  # 没人点按钮 ⇒ 超时
        self.assertNotIn(Z("cli.confirm.close_pane").strip(), out)
        self.assertEqual(fake.calls, [])

    def test_without_close_pane_flag_never_asks(self):
        h = Harness(self, subscribed=())
        self.click_when_sent(h, "slot4")
        code, out, err, fake = self._confirm_on_tty(h, ["--home", str(h.home), "confirm-sub", "slot4", "--timeout", "5"], "\n\n")
        self.assertEqual(code, 0, err)
        self.assertNotIn(Z("cli.confirm.close_pane").strip(), out)
        self.assertEqual(fake.calls, [])

    def test_close_pane_flag_on_non_tty_never_prompts(self):
        # agent 自己（stdout 被捕获）在 herdr 里跑 confirm-sub --close-pane：走「已确认过」/「开窗格」两条非 TTY 路径都退 0，
        # 但此时 HERDR_PANE_ID 是 agent 自己的窗格——绝不能问、更不能关
        fake = FakeHerdr()
        with mock.patch("agent_ntfy.herdr_run", fake):
            h = Harness(self)  # slot1 已过闸
            code, out, err = run(["--home", str(h.home), "confirm-sub", "slot1", "--close-pane"], "\n", env=HERDR)
            self.assertEqual(code, 0, err)
            h2 = Harness(self, subscribed=())
            code2, out2, err2 = run(["--home", str(h2.home), "confirm-sub", "slot4", "--close-pane"], "\n", env=HERDR)
            self.assertEqual(code2, 0, err2)
        self.assertNotIn(Z("cli.confirm.close_pane").strip(), out + out2)
        self.assertNotIn(Z("cli.confirm.pane_kept"), out + out2)
        self.assertNotIn(["pane", "close"], [c[1:3] for c in fake.calls])

    def test_close_pane_flag_outside_herdr_does_nothing(self):
        h = Harness(self, subscribed=())
        self.click_when_sent(h, "slot4")
        fake = FakeHerdr()
        with mock.patch("agent_ntfy.herdr_run", fake):
            code, out, err = self.run_on_tty(["--home", str(h.home), "confirm-sub", "slot4", "--timeout", "5", "--close-pane"], stdin_text="\n\n")
        self.assertEqual(code, 0, err)
        self.assertNotIn(Z("cli.confirm.close_pane").strip(), out)
        self.assertEqual(fake.calls, [])

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

    # 同一形态在 Windows 上：向 daemon 已关掉的连接发 ready 会把连接 abort（WSAECONNABORTED），缓冲区里的 timeout 事件跟着丢，
    # 之后的 recv 也失败 ⇒ 退 3「通信失败」。CLI 发 ready 之前先探一眼 socket：终态已到就不发——根本没发，就没有 abort
    def test_timeout_while_waiting_for_enter_does_not_send_ready_into_a_closed_connection(self):
        h = Harness(self, subscribed=())

        class SlowStdin(io.StringIO):
            def readline(self, *a):
                time.sleep(1.2)
                return "\n"

        class AbortableSocket:
            """真 socket 的替身：aborted 之后 recv 也抛 10053，像 Windows 上被 abort 的连接那样把缓冲区一并作废。"""

            def __init__(self, real):
                self.real, self.aborted = real, False

            def recv(self, n):
                if self.aborted:
                    raise OSError(10053, "An established connection was aborted by the software in your host machine")
                return self.real.recv(n)

            def __getattr__(self, name):  # sendall / fileno / close 原样交给真 socket
                return getattr(self.real, name)

        real_connect, real_send = agent_ntfy.connect, agent_ntfy.send_request
        ready_sent = []

        def fake_send(sock, req, *, home=None):
            if req == {"ready": True}:
                ready_sent.append(req)
                sock.aborted = True
                raise OSError(10053, "An established connection was aborted by the software in your host machine")
            real_send(sock, req, home=home)

        with mock.patch.object(agent_ntfy, "connect", lambda home: AbortableSocket(real_connect(home))), \
                mock.patch.object(agent_ntfy, "send_request", fake_send):
            code, out, err = self.run_on_tty(["--home", str(h.home), "confirm-sub", "slot4", "--timeout", "0.5"], stdin=SlowStdin())
        self.assertEqual(code, 2, err)  # 不探就发：3 + 「通信失败 … 10053」
        self.assertEqual(ready_sent, [], "终态已在缓冲区里，ready 就不该发")
        self.assertIn("还没发出", err)
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
        import subprocess
        h = tempfile.mkdtemp(prefix="an-")  # 短路径：unix 传输下 socket 路径有长度上限
        self.addCleanup(shutil.rmtree, h, ignore_errors=True)
        recorded = {}

        class FakePopen:
            pid = 4242

            def __init__(self, argv, **kw):
                recorded["argv"], recorded["kw"] = argv, kw
                srv, cleanup = ipc.listen(Path(h))  # 像 daemon 一样占住端点、回一条 status，pid 就是这个「子进程」的
                srv.setblocking(True)
                recorded["srv"], recorded["cleanup"] = srv, cleanup

                def serve():
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
        recorded["cleanup"]()
        self.assertEqual(code, 0, err)
        argv = recorded["argv"]
        self.assertLess(argv.index("--home"), argv.index("daemon"))
        self.assertEqual(argv[argv.index("--home") + 1], h)
        kw = recorded["kw"]
        self.assertEqual((kw["stdin"], kw["stdout"], kw["stderr"]), (subprocess.DEVNULL,) * 3)
        if sys.platform == "win32":
            self.assertEqual(kw["creationflags"], platform_.DETACHED_PROCESS | platform_.CREATE_NEW_PROCESS_GROUP)
            self.assertNotIn("start_new_session", kw)
        else:
            self.assertIs(kw["start_new_session"], True)
            self.assertNotIn("creationflags", kw)
        self.assertIn("pid 4242", out)

    # 已有 daemon 在跑：--detach 不起第二个（判据是探活，不是 pid 文件）
    def test_detach_refuses_when_a_daemon_answers(self):
        h = Harness(self)
        with mock.patch("agent_ntfy._spawn_daemon") as spawn:
            code, out, err = run(["--home", str(h.home), "daemon", "--detach"])
        self.assertEqual((code, out), (3, ""))
        self.assertIn(Z("cli.detach.already"), err)
        spawn.assert_not_called()

    def test_commands_without_daemon(self):
        code, out, err = run(["--home", "/nonexistent/agent-ntfy-home", "slots"])
        self.assertEqual(code, 3)
        self.assertIn("agent-ntfy daemon", err)
        code, out, err = run(["--home", "/nonexistent/agent-ntfy-home", "daemon", "--status"])
        self.assertEqual((code, out), (1, "daemon：未运行\n"))

    # 刚起来还没连上 ntfy 时，--status 不能打出「断开 None 秒」
    def test_status_before_first_connection_says_connecting(self):
        h = Harness(self)
        with mock.patch.object(agent_ntfy, "probe", return_value={"event": "status", "pid": os.getpid(), "subscribed": False,
                                                                   "disconnected_for": None, "pending": 0, "confirming": 0, "pool": 5}):
            code, out, err = run(["--home", str(h.home), "daemon", "--status"])
        self.assertEqual(code, 0)
        self.assertIn("连接中", out)
        self.assertNotIn("None", out)

    # --status 行尾打传输类型；探活走 socket，不看 pid 文件
    def test_status_prints_transport(self):
        h = Harness(self)
        code, out, err = run(["--home", str(h.home), "daemon", "--status"])
        self.assertEqual((code, err), (0, ""))
        self.assertIn(f"pid {os.getpid()}", out)
        self.assertIn(Z("cli.status.transport", transport=h.transport), out)

    # pid 文件在、socket 无应答（daemon 死了没清文件，或卡在初始化）：说「无应答」而不是「未运行」，rc 1
    def test_status_with_stale_pid_file_says_no_answer(self):
        home = Path(tempfile.mkdtemp(prefix="an-")) / "h"
        self.addCleanup(shutil.rmtree, home.parent, ignore_errors=True)
        home.mkdir()
        (home / "daemon.pid").write_text("4242\n")
        code, out, err = run(["--home", str(home), "daemon", "--status"])
        self.assertEqual((code, out), (1, Z("cli.status.no_socket", pid=4242) + "\n"))
        code, out, err = run(["--home", "/nonexistent/agent-ntfy-home", "daemon", "--status"])
        self.assertEqual((code, out), (1, Z("cli.status.not_running") + "\n"))

    # --stop 的等待有总预算（30 s）：daemon 答应了 stopping 却一直不退，到点报超时 rc 1，不会因为每轮探活各等一会儿而拖成几分钟
    def test_stop_gives_up_after_the_overall_budget(self):
        home = Path(tempfile.mkdtemp(prefix="an-")) / "h"
        self.addCleanup(shutil.rmtree, home.parent, ignore_errors=True)
        home.mkdir()
        (home / "daemon.pid").write_text("4242\n")
        started = time.monotonic()
        with mock.patch.object(agent_ntfy, "probe", return_value={"event": "status", "pid": 4242}), \
                mock.patch.object(agent_ntfy, "request", return_value={"event": "stopping", "pid": 4242}), \
                mock.patch.object(agent_ntfy, "STOP_TIMEOUT", 0.5):
            code, out, err = run(["--home", str(home), "daemon", "--stop"])
        self.assertEqual((code, out), (1, ""))
        self.assertIn(Z("cli.stop.timeout", pid=4242), err)
        self.assertLess(time.monotonic() - started, 1.5)

    # --stop 碰到「已 bind 但不应答」（卡在初始化）的 daemon：与 --status 同口径——无应答 + pid 文件仍在 ⇒ rc 1，不说「未运行」
    def test_stop_on_a_silent_listener_says_no_answer(self):
        home = Path(tempfile.mkdtemp(prefix="an-")) / "h"
        self.addCleanup(shutil.rmtree, home.parent, ignore_errors=True)
        home.mkdir()
        (home / "daemon.pid").write_text("4242\n")
        listener, cleanup = ipc.listen(home)  # 端点绑上了（连接能进 backlog），但没人 accept、没人回话：就是卡在初始化的样子
        self.addCleanup(cleanup)
        self.addCleanup(listener.close)
        with mock.patch("agent_ntfy.PROBE_TIMEOUT", 0.3):
            code, out, err = run(["--home", str(home), "daemon", "--stop"])
        self.assertEqual((code, out), (1, Z("cli.status.no_socket", pid=4242) + "\n"))

    # 在 Windows 分支下 --detach 起子进程用的是 creationflags（经 CLI 一路走到 platform_.spawn_detached）
    def test_detach_on_windows_uses_creationflags(self):
        import subprocess
        h = tempfile.mkdtemp(prefix="an-")
        self.addCleanup(shutil.rmtree, h, ignore_errors=True)
        recorded = {}

        class FakePopen:
            pid = 4242

            def __init__(self, argv, **kw):
                recorded["kw"] = kw

            def poll(self):
                return 7  # 起来就退：daemon_detach 报退出码，用例只看 Popen 参数

        with mock.patch.object(subprocess, "Popen", FakePopen), mock.patch("platform_._platform", return_value="win32"):
            code, out, err = run(["--home", h, "daemon", "--detach"])
        self.assertEqual(code, 3)
        self.assertEqual(recorded["kw"]["creationflags"], platform_.DETACHED_PROCESS | platform_.CREATE_NEW_PROCESS_GROUP)
        self.assertNotIn("start_new_session", recorded["kw"])

    # AGENT_NTFY_IPC 给了非法值：每个子命令都在入口响亮退 1 + 人读文案，不是 traceback、不静默回退
    def test_invalid_env_ipc_fails_loudly_everywhere(self):
        for argv, stdin in ((["slots"], ""), (["ask"], json.dumps(SAMPLE)), (["daemon", "--status"], ""), (["daemon", "--stop"], ""),
                            (["daemon", "--detach"], ""), (["daemon"], ""), (["release"], "")):
            code, out, err = run(["--home", "/nonexistent/agent-ntfy-home", *argv], stdin, env={"AGENT_NTFY_IPC": "bogus"})
            self.assertEqual((code, out), (1, ""), (argv, err))
            self.assertIn("bogus", err)
            self.assertIn("unix / tcp", err)
            self.assertNotIn("Traceback", err)

    # --stop 经 socket 的 stop 命令停 daemon，等它退干净；不再向 pid 发信号（三平台同一条路）
    def test_stop_goes_through_the_socket_not_a_signal(self):
        h = Harness(self)
        with mock.patch.object(agent_ntfy.os, "kill", side_effect=AssertionError("must not signal")):
            code, out, err = run(["--home", str(h.home), "daemon", "--stop"])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, Z("cli.stop.done", pid=os.getpid()) + "\n")
        h.thread.join(5)
        self.assertFalse(h.thread.is_alive())
        self.assertFalse(h.endpoint.exists())
        self.assertFalse((h.home / "daemon.pid").exists())
        code, out, err = run(["--home", str(h.home), "daemon", "--stop"])  # 已经停了：说未运行，rc 0
        self.assertEqual((code, out), (0, Z("cli.status.not_running") + "\n"))


class HerdrHelpersTest(unittest.TestCase):
    """CLI 侧的 herdr 能力判定与 daemon 探活。herdr 一律替身。"""

    def test_herdr_available_needs_env_and_a_working_cli(self):
        fake = FakeHerdr()
        with mock.patch("agent_ntfy.herdr_run", fake):
            with mock.patch.dict(os.environ, {"HERDR_ENV": "", "HERDR_PANE_ID": ""}):
                self.assertFalse(agent_ntfy.herdr_available())
            with mock.patch.dict(os.environ, {"HERDR_ENV": "1", "HERDR_PANE_ID": ""}):
                self.assertFalse(agent_ntfy.herdr_available())
            self.assertEqual(fake.calls, [])  # 环境变量不全就不去跑 herdr
            with mock.patch.dict(os.environ, HERDR):
                self.assertTrue(agent_ntfy.herdr_available())
                self.assertEqual(fake.calls, [["herdr", "pane", "list"]])
                fake.list_result = HerdrResult(rc=1, stdout="", stderr=herdr_error("pane:list", "server_not_running"))
                self.assertFalse(agent_ntfy.herdr_available())

    # 探活是静默的：没 daemon 就是 None，stderr 一个字都不打（away on 要拿它决定起不起 daemon，不是报错）
    def test_probe_is_silent_when_no_daemon(self):
        errbuf = io.StringIO()
        with contextlib.redirect_stderr(errbuf):
            self.assertIsNone(agent_ntfy.probe(Path("/nonexistent/agent-ntfy-home")))
        self.assertEqual(errbuf.getvalue(), "")

    # daemon 已 bind 但还没进主循环（比如卡在初始化）：探活要在有限时间内放弃，不能让 away on 挂死
    def test_probe_gives_up_on_a_silent_listener(self):
        home = Path(tempfile.mkdtemp(prefix="home-"))
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        srv, cleanup = ipc.listen(home)  # 按当前传输起监听（unix 的 sock 文件 / tcp 的 port 文件都到位），只 bind 不 accept
        self.addCleanup(cleanup)
        self.addCleanup(srv.close)
        started = time.monotonic()
        with mock.patch("agent_ntfy.PROBE_TIMEOUT", 0.3):
            self.assertIsNone(agent_ntfy.probe(home))
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 0.25)  # 真等到了监听端超时，不是连都没连上就返回（那样任何传输下都会「通过」）；留 50 ms 给 Windows 的定时器粒度（实测早醒 0.2 ms）
        self.assertLess(elapsed, 1.5)

    def test_request_gives_up_on_a_silent_listener(self):
        # daemon 接了连接却不回第一条事件：一问一答命令要在 REQUEST_TIMEOUT 内退出并报「没有回应」，不能挂死
        home = Path(tempfile.mkdtemp(prefix="home-"))
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        srv, cleanup = ipc.listen(home)
        self.addCleanup(cleanup)
        self.addCleanup(srv.close)
        errbuf = io.StringIO()
        started = time.monotonic()
        with mock.patch("agent_ntfy.REQUEST_TIMEOUT", 0.3), contextlib.redirect_stderr(errbuf):
            ev = agent_ntfy.request(home, {"cmd": "slots", "leased_by": "proj:/x", "pane": None}, "zh", not_sent=True)
        elapsed = time.monotonic() - started
        self.assertIsNone(ev)
        self.assertGreaterEqual(elapsed, 0.25)
        self.assertLess(elapsed, 1.5)
        self.assertIn("daemon 没有回应", errbuf.getvalue())
        self.assertIn("消息未发送", errbuf.getvalue())

    # 语言四级解析：--lang > AGENT_NTFY_LANG > 系统 locale（中文 ⇒ zh）> en；只在进程入口解析一次
    def test_lang_flag_beats_env(self):
        h = Harness(self)
        code, out, err = run(["--lang", "en", "--home", str(h.home), "daemon", "--status"], env={"AGENT_NTFY_LANG": "zh"})
        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith("daemon: pid"), out)

    def test_lang_flag_equals_form_and_help(self):
        h = Harness(self)
        code, out, err = run(["--lang=zh", "--home", str(h.home), "daemon", "--status"], env={"AGENT_NTFY_LANG": "en"})
        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith("daemon：pid"), out)
        help_out = io.StringIO()
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stdout(help_out):
            agent_ntfy.main(["--lang", "zh", "--help"])
        self.assertEqual(cm.exception.code, 0)
        self.assertIn("文案语言", help_out.getvalue())  # 预扫的 --lang 决定 --help 的语言
        with self.assertRaises(SystemExit) as cm:
            run(["--home", str(h.home), "daemon", "--status", "--lang", "zh"])  # 顶层选项放在子命令后面：argparse 不认
        self.assertEqual(cm.exception.code, 2)

    def test_lang_flag_rejects_unknown_value(self):
        with self.assertRaises(SystemExit) as cm:
            run(["--lang", "fr", "slots"])
        self.assertEqual(cm.exception.code, 2)  # argparse 的 choices 拦下

    def test_locale_zh_is_used_when_env_unset(self):
        h = Harness(self)
        for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
            with self.subTest(var=var):
                env = {"AGENT_NTFY_LANG": "", "LC_ALL": "", "LC_MESSAGES": "", "LANG": "", var: "zh_CN.UTF-8"}
                code, out, err = run(["--home", str(h.home), "daemon", "--status"], env=env)
                self.assertEqual(code, 0, err)
                self.assertTrue(out.startswith("daemon：pid"), (var, out))

    def test_locale_non_zh_falls_back_to_english(self):
        h = Harness(self)
        env = {"AGENT_NTFY_LANG": "", "LC_ALL": "en_US.UTF-8", "LC_MESSAGES": "zh_CN.UTF-8", "LANG": "zh_CN.UTF-8"}  # LC_ALL 优先
        code, out, err = run(["--home", str(h.home), "daemon", "--status"], env=env)
        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith("daemon: pid"), out)
        with mock.patch.dict(os.environ, {"AGENT_NTFY_LANG": "", "LC_ALL": "", "LC_MESSAGES": "", "LANG": ""}), \
                mock.patch("locale.getlocale", return_value=("Chinese (Simplified)_China", "936")):
            self.assertEqual(agent_ntfy.resolve_lang(), "zh")  # Windows 常没有那三个变量：看 locale.getlocale()
        with mock.patch.dict(os.environ, {"AGENT_NTFY_LANG": "", "LC_ALL": "", "LC_MESSAGES": "", "LANG": ""}), \
                mock.patch("locale.getlocale", return_value=(None, None)):
            self.assertEqual(agent_ntfy.resolve_lang(), "en")

    def test_env_lang_beats_locale(self):
        h = Harness(self)
        code, out, err = run(["--home", str(h.home), "daemon", "--status"], env={"AGENT_NTFY_LANG": "en", "LC_ALL": "zh_CN.UTF-8"})
        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith("daemon: pid"), out)

    def test_probe_returns_status_event(self):
        h = Harness(self)
        ev = agent_ntfy.probe(h.home)
        self.assertIsNotNone(ev)
        assert ev is not None
        self.assertEqual((ev["event"], ev["pid"]), ("status", os.getpid()))


class ConfirmSubPaneTest(unittest.TestCase):
    """confirm-sub 在非 TTY + herdr 可用时：开一个新窗格、在里面跑默认形态，打印窗格 id 立即返回 0；herdr 不可用仍退 4。"""

    def test_non_tty_in_herdr_opens_pane_and_returns_0(self):
        h = Harness(self, subscribed=())
        fake = FakeHerdr()
        with mock.patch("agent_ntfy.herdr_run", fake):
            code, out, err = run(["--home", str(h.home), "confirm-sub", "slot4", "--again"], env=HERDR)
        self.assertEqual(code, 0, err)
        self.assertIn("wD:p7", out.splitlines()[0])  # 窗格 id 在首行，agent 一眼能取到
        self.assertIn("slot4", out)
        self.assertEqual([c[1:3] for c in fake.calls], [["pane", "list"], ["pane", "split"], ["pane", "run"]])
        split, ran = fake.calls[1], fake.calls[2]
        self.assertEqual(split[3:5], ["--pane", "wD:p1"])  # 在当前窗格下方开
        self.assertEqual(split[split.index("--cwd") + 1], os.getcwd())
        self.assertEqual(ran[3], "wD:p7")
        argv = ti.split_pane_command(ran[4])
        self.assertEqual(argv[:4], [sys.executable, os.path.abspath(agent_ntfy.__file__), "--lang", "zh"])  # 新窗格是新 shell，不继承调用方的语言：--lang 显式带上
        self.assertEqual(argv[4:], ["--home", str(h.home), "confirm-sub", "slot4", "--close-pane", "--again"])  # --home 在子命令前；自动开的窗格带 --close-pane；--again 原样转进去
        # 本进程不碰 daemon：没发测试通知、没进确认中；topic 名不进本进程的输出
        self.assertEqual(h.client.published, [])
        self.assertEqual(h.request(cmd="status")[0]["confirming"], 0)
        for t in h.store.load() or []:
            self.assertNotIn(t, out + err)

    # 开窗格之前先经 daemon 的 slots 视图核一遍：槽位不存在 ⇒ 退 1（沿用 unknown_slot 路径），不开窗格
    def test_non_tty_unknown_slot_exits_1_without_opening_pane(self):
        h = Harness(self, subscribed=())
        fake = FakeHerdr()
        with mock.patch("agent_ntfy.herdr_run", fake):
            code, out, err = run(["--home", str(h.home), "confirm-sub", "slot9"], env=HERDR)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("slot9", err)
        self.assertNotIn(["pane", "split"], [c[1:3] for c in fake.calls])

    # 已过闸且没带 --again ⇒ 打「已确认过」退 0，不开窗格；带 --again 才开
    def test_non_tty_confirmed_slot_says_already_unless_again(self):
        h = Harness(self)  # 全部已过闸
        fake = FakeHerdr()
        with mock.patch("agent_ntfy.herdr_run", fake):
            code, out, err = run(["--home", str(h.home), "confirm-sub", "slot2"], env=HERDR)
            self.assertEqual(code, 0, err)
            self.assertEqual(out, Z("cli.confirm.already", slot="slot2") + "\n")
            self.assertNotIn(["pane", "split"], [c[1:3] for c in fake.calls])
            code, out, err = run(["--home", str(h.home), "confirm-sub", "slot2", "--again", "--timeout", "45"], env=HERDR)
            self.assertEqual(code, 0, err)
            self.assertIn("wD:p7", out)
        self.assertEqual(ti.split_pane_command(fake.calls[-1][4])[-6:], ["confirm-sub", "slot2", "--close-pane", "--again", "--timeout", "45"])  # 两个旗标都原样转进去

    # daemon 没跑 ⇒ 退 3（同现状），不开窗格
    def test_non_tty_without_daemon_exits_3(self):
        fake = FakeHerdr()
        with mock.patch("agent_ntfy.herdr_run", fake):
            code, out, err = run(["--home", "/nonexistent/agent-ntfy-home", "confirm-sub", "slot1"], env=HERDR)
        self.assertEqual((code, out), (3, ""))
        self.assertIn("daemon 没在跑", err)
        self.assertNotIn(["pane", "split"], [c[1:3] for c in fake.calls])

    # 调用方是 en 时窗格里也是 en
    def test_non_tty_pane_command_carries_english_when_caller_is_english(self):
        h = Harness(self, subscribed=())
        fake = FakeHerdr()
        with mock.patch("agent_ntfy.herdr_run", fake):
            code, out, err = run(["--home", str(h.home), "confirm-sub", "slot4"], env={**HERDR, "AGENT_NTFY_LANG": "en"})
        self.assertEqual(code, 0, err)
        self.assertEqual(ti.split_pane_command(fake.calls[-1][4])[2:4], ["--lang", "en"], fake.calls[-1][4])
        self.assertIn("Tell the user", out)

    def test_non_tty_in_herdr_but_cli_broken_still_exits_4(self):
        h = Harness(self, subscribed=())
        fake = FakeHerdr()
        fake.list_result = HerdrResult(rc=1, stdout="", stderr=herdr_error("pane:list", "server_not_running"))
        with mock.patch("agent_ntfy.herdr_run", fake):
            code, out, err = run(["--home", str(h.home), "confirm-sub", "slot4"], env=HERDR)
        self.assertEqual((code, out), (4, ""))
        self.assertIn("agent-ntfy confirm-sub slot4", err)
        self.assertEqual([c[1:3] for c in fake.calls], [["pane", "list"]])

    def test_non_tty_split_failure_exits_4(self):
        h = Harness(self, subscribed=())
        fake = FakeHerdr()
        fake.split_result = HerdrResult(rc=1, stdout="", stderr=herdr_error("pane:split", "pane_not_found"))
        with mock.patch("agent_ntfy.herdr_run", fake):
            code, out, err = run(["--home", str(h.home), "confirm-sub", "slot4"], env=HERDR)
        self.assertEqual((code, out), (4, ""))
        self.assertIn("agent-ntfy confirm-sub slot4", err)

    def test_non_tty_run_failure_after_split_exits_4(self):
        h = Harness(self, subscribed=())
        fake = FakeHerdr()
        fake.run_result = HerdrResult(rc=1, stdout="", stderr=herdr_error("pane:run", "pane_not_found"))
        with mock.patch("agent_ntfy.herdr_run", fake):
            code, out, err = run(["--home", str(h.home), "confirm-sub", "slot4"], env=HERDR)
        self.assertEqual((code, out), (4, ""))
        self.assertIn("agent-ntfy confirm-sub slot4", err)
        self.assertEqual([c[1:3] for c in fake.calls], [["pane", "list"], ["pane", "split"], ["pane", "run"]])

    def test_show_topic_and_subscribed_paths_do_not_open_panes(self):
        h = Harness(self, subscribed=())
        fake = FakeHerdr()
        with mock.patch("agent_ntfy.herdr_run", fake):
            code, out, err = run(["--home", str(h.home), "confirm-sub", "slot4", "--show-topic"], env=HERDR)
            self.assertEqual(code, 0, err)
            self.assertIn(h.topic("slot4"), out)
            code, out, err = run(["--home", str(h.home), "confirm-sub", "slot4", "--subscribed", "--timeout", "0.3"], env=HERDR)
            self.assertEqual(code, 2, err)
        self.assertEqual(fake.calls, [])


class AwayOnTest(unittest.TestCase):
    """away on 一站式：daemon 保障 → 槽位保障 → 写状态文件；退 3 / 4 时什么都不写、也不建目录。herdr 一律替身，不真开窗格。"""

    def setUp(self):
        self.root = temp_root(self)
        self.fake = FakeHerdr()
        patcher = mock.patch("agent_ntfy.herdr_run", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def away_on(self, home, env=HERDR):
        return run(["--home", str(home), "away", "on"], env=env, root=self.root)

    def state(self):
        return json.loads(projstate.state_path(self.root).read_text(encoding="utf-8"))

    def pane_commands(self):
        return [ti.split_pane_command(c[4]) for c in self.fake.calls if c[1:3] == ["pane", "run"]]

    # 本项目已租且已过闸：直接就绪，不碰 herdr
    def test_ready_when_own_lease_is_confirmed(self):
        h = Harness(self)
        h.state.acquire(owner(self.root), pane="wD:p9")
        code, out, err = self.away_on(h.home)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines()[0], Z("cli.away.ready", slot="slot1"))
        self.assertIn(str(projstate.state_path(self.root)), out)
        self.assertEqual((self.state()["away"], self.state()["target"]), (True, owner(self.root)))
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(h.state.slots()["slot1"]["pane"], "wD:p1")  # 顺手把租约的窗格刷新成当前窗格

    # 未租但池里有空闲已过闸槽位：就绪（惰性，首次提问才租），不开 pane
    def test_ready_lazily_when_a_confirmed_slot_is_free(self):
        h = Harness(self)
        code, out, err = self.away_on(h.home)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines()[0], Z("cli.away.ready.lazy"))
        self.assertEqual((self.state()["away"], self.state()["slot"]), (True, None))
        self.assertEqual(self.fake.calls, [])
        self.assertIsNone(h.state.slots()["slot1"]["leased_by"])

    # 本项目已租但未过闸：对它开确认窗格；状态文件照写
    def test_own_unconfirmed_lease_opens_confirm_pane(self):
        h = Harness(self, subscribed=())
        h.state.acquire(owner(self.root), pane="wD:p1")
        code, out, err = self.away_on(h.home)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines()[0], Z("cli.away.confirm_pane", slot="slot1", pane="wD:p7").splitlines()[0])
        self.assertIn("wD:p7", out)
        self.assertEqual([c[1:3] for c in self.fake.calls], [["pane", "list"], ["pane", "split"], ["pane", "run"]])
        self.assertEqual(self.pane_commands()[0][2:4], ["--lang", "zh"])  # 确认窗格里的文案与调用方同语言
        self.assertEqual(self.pane_commands()[0][4:], ["--home", str(h.home), "confirm-sub", "slot1", "--close-pane"])
        self.assertTrue(self.state()["away"])

    # 同样情形但不在 herdr 里：退 4 指路 confirm-sub，什么都不写、不建目录
    def test_own_unconfirmed_lease_outside_herdr_exits_4_without_writing(self):
        h = Harness(self, subscribed=())
        h.state.acquire(owner(self.root))
        code, out, err = self.away_on(h.home, env={})
        self.assertEqual((code, out), (4, ""))
        self.assertIn("agent-ntfy confirm-sub slot1", err)
        self.assertFalse((self.root / projstate.DIR_NAME).exists())
        self.assertEqual(self.fake.calls, [])

    # 未租且没有空闲已过闸槽位：挑最小号空闲槽位开确认窗格
    def test_no_confirmed_slot_opens_pane_on_smallest_free_slot(self):
        h = Harness(self, subscribed=())
        h.state.acquire("proj:/w/other")  # slot1 被别人租走：最小号空闲的是 slot2
        code, out, err = self.away_on(h.home)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("slot2", out.splitlines()[0])
        self.assertIn("wD:p7", out)
        self.assertEqual(self.pane_commands()[0][-3:], ["confirm-sub", "slot2", "--close-pane"])
        self.assertTrue(self.state()["away"])
        self.assertIsNone(h.state.slots()["slot2"]["leased_by"])  # 不替用户租：确认过之后首次提问才租

    # 挑到的槽位正在确认中（上一次 away on 开的窗格还没走完）：不再开第二个窗格，指去已开的那个；照写状态文件
    def test_slot_already_confirming_does_not_open_a_second_pane(self):
        h = Harness(self, subscribed=())
        sock, ev, _ = h.confirm("slot1")  # 有人正在对 slot1 走确认（停在 topic 段）
        self.addCleanup(sock.close)
        self.assertEqual(ev["event"], "topic")
        code, out, err = self.away_on(h.home)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines()[0], Z("cli.away.confirming", slot="slot1").splitlines()[0])
        self.assertNotIn("pane split", " ".join(" ".join(c[1:3]) for c in self.fake.calls))
        self.assertTrue(self.state()["away"])

    # 一个空闲槽位都没有：退 4 + 候选清单（可替换的空闲已租槽位），不写文件
    def test_no_free_slot_exits_4_with_candidates(self):
        h = Harness(self, pool_size=2, subscribed=("slot1",))
        h.state.acquire("proj:/w/a")
        h.state.acquire("proj:/w/b")
        code, out, err = self.away_on(h.home)
        self.assertEqual((code, out), (4, ""))
        listed = ", ".join([Z("daemon.candidate.confirmed", slot="slot1"), Z("daemon.candidate.unconfirmed", slot="slot2")])
        self.assertEqual(err.splitlines()[0], "agent-ntfy: " + Z("daemon.no_free_slot", candidates=listed))  # 抬头与 ask 撞满时同款
        self.assertIn(Z("cli.ask.candidate", slot="slot1", gate=Z("cli.ask.candidate.confirmed")), err)
        self.assertIn(Z("cli.ask.candidate", slot="slot2", gate=Z("cli.ask.candidate.unconfirmed")), err)
        self.assertFalse((self.root / projstate.DIR_NAME).exists())
        self.assertEqual([c[1:3] for c in self.fake.calls], [])

    # daemon 没跑、在 herdr 里：开窗格在里面起 daemon；探活等到超时仍没起来 ⇒ 退 3，不写文件、不建目录
    def test_daemon_not_started_in_herdr_exits_3_without_writing(self):
        home = Path(tempfile.mkdtemp(prefix="home-")) / "home"
        self.addCleanup(shutil.rmtree, home.parent, ignore_errors=True)
        with mock.patch("agent_ntfy.DAEMON_START_TIMEOUT", 0.3):
            code, out, err = self.away_on(home)
        self.assertEqual((code, out), (3, ""))
        self.assertIn(Z("cli.away.daemon_failed", seconds="0.3", log=home / "daemon.log"), err)
        self.assertEqual([c[1:3] for c in self.fake.calls], [["pane", "list"], ["pane", "split"], ["pane", "run"]])
        self.assertEqual(self.pane_commands()[0], [sys.executable, os.path.abspath(agent_ntfy.__file__), "--lang", "zh", "--home", str(home), "daemon"])
        self.assertFalse((self.root / projstate.DIR_NAME).exists())

    # 在 herdr 里但窗格开不出来：立刻退 3，不傻等探活超时
    def test_split_failure_exits_3_without_waiting(self):
        home = Path(tempfile.mkdtemp(prefix="home-")) / "home"
        self.addCleanup(shutil.rmtree, home.parent, ignore_errors=True)
        self.fake.split_result = HerdrResult(rc=1, stdout="", stderr=herdr_error("pane:split", "pane_not_found"))
        started = time.monotonic()
        with mock.patch("agent_ntfy.DAEMON_START_TIMEOUT", 3.0):
            code, out, err = self.away_on(home)
        self.assertEqual((code, out), (3, ""))
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertIn(Z("cli.away.pane_failed"), err)  # 不是「探不到」：根本没起，指路 daemon --detach
        self.assertIn("daemon --detach", err)
        self.assertEqual([c[1:3] for c in self.fake.calls], [["pane", "list"], ["pane", "split"]])
        self.assertFalse((self.root / projstate.DIR_NAME).exists())

    # 窗格开出来了但命令敲不进去：同样立刻退 3
    def test_run_failure_after_split_exits_3_without_waiting(self):
        home = Path(tempfile.mkdtemp(prefix="home-")) / "home"
        self.addCleanup(shutil.rmtree, home.parent, ignore_errors=True)
        self.fake.run_result = HerdrResult(rc=1, stdout="", stderr=herdr_error("pane:run", "pane_not_found"))
        started = time.monotonic()
        with mock.patch("agent_ntfy.DAEMON_START_TIMEOUT", 3.0):
            code, out, err = self.away_on(home)
        self.assertEqual((code, out), (3, ""))
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertIn(Z("cli.away.pane_failed"), err)
        self.assertEqual([c[1:3] for c in self.fake.calls], [["pane", "list"], ["pane", "split"], ["pane", "run"]])

    # daemon 已 bind 但不应答（卡在初始化）：总耗时仍以 DAEMON_START_TIMEOUT 为准，单次探活超时不能把它撑长
    def test_silent_listener_respects_the_overall_budget(self):
        home = Path(tempfile.mkdtemp(prefix="home-"))
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        srv, cleanup = ipc.listen(home)  # 按当前传输起监听（unix 的 sock 文件 / tcp 的 port 文件都到位），只 bind 不 accept
        self.addCleanup(cleanup)
        self.addCleanup(srv.close)
        started = time.monotonic()
        with mock.patch("agent_ntfy.DAEMON_START_TIMEOUT", 0.5), mock.patch("agent_ntfy.PROBE_TIMEOUT", 2.0):
            code, out, err = self.away_on(home)
        self.assertEqual((code, out), (3, ""))
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertIn(Z("cli.away.daemon_failed", seconds="0.5", log=home / "daemon.log"), err)

    # 不在 herdr 里、子进程起来就退了：报它的退出码，不等满探活预算
    def test_spawned_daemon_dying_exits_3_with_its_rc(self):
        home = Path(tempfile.mkdtemp(prefix="home-")) / "home"
        self.addCleanup(shutil.rmtree, home.parent, ignore_errors=True)
        started = time.monotonic()
        with mock.patch("agent_ntfy.DAEMON_START_TIMEOUT", 3.0), mock.patch("agent_ntfy._spawn_daemon", return_value=mock.Mock(poll=lambda: 7, pid=4242)):
            code, out, err = self.away_on(home, env={})
        self.assertEqual((code, out), (3, ""))
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertIn(Z("cli.detach.died", rc=7, log=home / "daemon.log"), err)
        self.assertFalse((self.root / projstate.DIR_NAME).exists())

    # daemon 没跑、不在 herdr 里：脱离会话起（Popen），起来后照常走槽位保障
    def test_daemon_spawned_outside_herdr_then_ready(self):
        h = Harness(self)
        real_probe, probes = agent_ntfy.probe, []

        def probe_none_first(home, **kw):
            probes.append(home)
            return None if len(probes) == 1 else real_probe(home, **kw)

        spawned = []
        with mock.patch("agent_ntfy.probe", probe_none_first), mock.patch("agent_ntfy._spawn_daemon", lambda home, lang: spawned.append((home, lang)) or mock.Mock(poll=lambda: None)):
            code, out, err = self.away_on(h.home, env={})
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(spawned, [(h.home, "zh")])  # 脱离会话起的 daemon 也显式带语言
        self.assertEqual(out.splitlines()[0], Z("cli.away.ready.lazy"))
        self.assertTrue(self.state()["away"])
        self.assertEqual(self.fake.calls, [])

    # 状态目录不可写：第一步就退 3，daemon 不起、herdr 不碰
    def test_unwritable_state_dir_exits_3_before_anything_starts(self):
        (self.root / projstate.DIR_NAME).write_text("not a dir", encoding="utf-8")
        with mock.patch("agent_ntfy._spawn_daemon") as spawn:
            code, out, err = self.away_on(Path("/nonexistent/agent-ntfy-home"))
        self.assertEqual((code, out), (3, ""))
        self.assertIn(Z("cli.away.io_failed", error="").rstrip(), err)
        self.assertEqual(self.fake.calls, [])
        spawn.assert_not_called()

    # off / status 不受影响（off 不需要 daemon）
    def test_off_still_works_without_daemon(self):
        projstate.save(self.root, away=True, target=owner(self.root))
        code, out, err = run(["--home", "/nonexistent/agent-ntfy-home", "away", "off"], root=self.root)
        self.assertEqual((code, err), (0, ""))
        self.assertFalse(self.state()["away"])


if __name__ == "__main__":
    unittest.main()
