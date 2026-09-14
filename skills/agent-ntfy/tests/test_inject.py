"""注入层：herdr 子进程调用、能力判定与分档、投递失败回执、控制标记。

herdr 用可替换的调用器替身：记录 argv、按预置返回 rc / stdout / stderr。真 herdr 注入不在这里做——
往任何活着的 pane 注都会打扰一个正在工作的 agent；只有 run_herdr() 本身用无害的本地命令验证子进程包装。
"""

import json
import os
import re
import shlex
import subprocess
import sys
import unittest

import inject
import texts
from inject import HerdrResult, Outcome


def Z(key, **fmt):
    return texts.t(key, "zh", **fmt)

TEXT = "把 B 方案也列进去，不要只给 A"
PANES_STDOUT = json.dumps({"id": "cli:pane:list", "result": {"panes": [
    {"pane_id": "wD:p1", "agent": "claude", "agent_status": "working", "workspace_id": "wD"},
    {"pane_id": "wD:p2", "agent": "kimi", "agent_status": "idle", "workspace_id": "wD"},
    {"pane_id": "wD:p3", "agent_status": "unknown", "workspace_id": "wD"},
], "type": "pane_list"}})  # herdr 0.9.0 实物：type 在 result 里


def herdr_error(cmd: str, code: str, message: str = "") -> str:
    """herdr 0.9.0 的失败形态：stdout 空、stderr 一行 JSON。"""
    return json.dumps({"id": f"cli:{cmd}", "error": {"code": code, "message": message or code}})


SPLIT_STDOUT = json.dumps({"id": "cli:pane:split", "result": {"pane": {"pane_id": "wD:p7", "workspace_id": "wD"}, "type": "pane_info"}})  # herdr 0.9.0 实物


def split_pane_command(command: str) -> list[str]:
    """把 run_in_pane 拼出来的整条命令拆回 argv，用的是它拼装时针对的那套规则：POSIX 是 sh（shlex），
    Windows 是 list2cmdline 的逆 CommandLineToArgvW（MS C 运行时的 argv 解析）。"""
    if sys.platform != "win32":
        return shlex.split(command)
    import ctypes
    from ctypes import wintypes
    shell32, kernel32 = ctypes.windll.shell32, ctypes.windll.kernel32
    shell32.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    shell32.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    argc = ctypes.c_int()
    argv = shell32.CommandLineToArgvW(command, ctypes.byref(argc))
    if not argv:
        raise ctypes.WinError()
    try:
        return [argv[i] for i in range(argc.value)]
    finally:
        kernel32.LocalFree(argv)


class FakeHerdr:
    """按子命令预置结果的调用器。calls 记录每次 argv，顺序就是调用顺序。"""

    def __init__(self, *, panes_stdout=PANES_STDOUT):
        self.calls: list[list[str]] = []
        self.list_result = HerdrResult(rc=0, stdout=panes_stdout, stderr="")
        self.prompt_result = HerdrResult(rc=0, stdout='{"id":"cli:agent:prompt","result":{"agent":{},"type":"agent_prompted"}}', stderr="")
        self.keys_result = HerdrResult(rc=0, stdout='{"id":"cli:agent:send-keys","result":{"agent":{},"type":"agent_keys_sent"}}', stderr="")
        self.split_result = HerdrResult(rc=0, stdout=SPLIT_STDOUT, stderr="")
        self.run_result = HerdrResult(rc=0, stdout="", stderr="")  # pane run 成功时 stdout 为空（实测）
        self.close_result = HerdrResult(rc=0, stdout='{"id":"cli:pane:close","result":{"type":"ok"}}', stderr="")

    def __call__(self, argv: list[str]) -> HerdrResult:
        self.calls.append(list(argv))
        sub = tuple(argv[1:3])
        if sub == ("pane", "list"):
            return self.list_result
        if sub == ("agent", "prompt"):
            return self.prompt_result
        if sub == ("agent", "send-keys"):
            return self.keys_result
        if sub == ("pane", "split"):
            return self.split_result
        if sub == ("pane", "run"):
            return self.run_result
        if sub == ("pane", "close"):
            return self.close_result
        raise AssertionError(f"没预置的 herdr 调用：{argv}")

    def subcommands(self) -> list[str]:
        return [" ".join(c[1:3]) for c in self.calls]


class DeliverTest(unittest.TestCase):
    def test_no_lease_is_receipt_without_touching_herdr(self):
        fake = FakeHerdr()
        out = inject.deliver("slot3", None, TEXT, run=fake, lang="zh")
        self.assertFalse(out.delivered)
        self.assertEqual(out.reason, "no_lease")
        self.assertEqual(fake.calls, [])
        self.assertIn("slot3", out.detail)
        self.assertNotIn(TEXT, out.detail)

    def test_herdr_server_not_running_is_no_herdr(self):
        fake = FakeHerdr()
        fake.list_result = HerdrResult(rc=1, stdout="", stderr=herdr_error("pane:list", "server_not_running", "no herdr server is running at /x"))
        out = inject.deliver("slot1", "wD:p1", TEXT, run=fake, lang="zh")
        self.assertFalse(out.delivered)
        self.assertEqual(out.reason, "no_herdr")
        self.assertEqual(fake.subcommands(), ["pane list"])  # 没去 prompt
        self.assertIn("server_not_running", out.detail)
        self.assertIn("wD:p1", out.detail)

    def test_herdr_binary_missing_is_no_herdr(self):
        fake = FakeHerdr()
        fake.list_result = HerdrResult(rc=127, stdout="", stderr="herdr: command not found")
        out = inject.deliver("slot1", "wD:p1", TEXT, run=fake, lang="zh")
        self.assertEqual(out.reason, "no_herdr")
        self.assertIn("没装", out.detail)

    def test_unparseable_pane_list_is_no_herdr(self):
        fake = FakeHerdr(panes_stdout="not json")
        out = inject.deliver("slot1", "wD:p1", TEXT, run=fake, lang="zh")
        self.assertEqual(out.reason, "no_herdr")
        self.assertEqual(fake.subcommands(), ["pane list"])

    def test_pane_list_timeout_is_no_herdr(self):
        fake = FakeHerdr()
        fake.list_result = HerdrResult(rc=124, stdout="", stderr="", timed_out=True, timeout=15)
        out = inject.deliver("slot1", "wD:p1", TEXT, run=fake, lang="zh")
        self.assertEqual(out.reason, "no_herdr")
        self.assertIn("15 秒无响应", out.detail)
        self.assertEqual(fake.subcommands(), ["pane list"])

    def test_pane_missing_is_receipt_without_prompt(self):
        fake = FakeHerdr()
        out = inject.deliver("slot1", "wX:p9", TEXT, run=fake, lang="zh")
        self.assertFalse(out.delivered)
        self.assertEqual(out.reason, "pane_missing")
        self.assertEqual(fake.subcommands(), ["pane list"])
        self.assertIn("wX:p9", out.detail)
        self.assertIn("不在 herdr 的窗格列表里", out.detail)  # 对非 herdr 身份（host:…|sid:…）这句也为真，「已经不存在了」不是

    def test_pane_missing_wording_holds_for_non_herdr_identity(self):
        fake = FakeHerdr()
        out = inject.deliver("slot1", "host:mac|sid:123", TEXT, run=fake, lang="zh")
        self.assertEqual(out.reason, "pane_missing")
        self.assertIn("host:mac|sid:123 不在 herdr 的窗格列表里", out.detail)

    def test_claude_gets_prompt_with_pane_id_and_prefixed_text(self):
        fake = FakeHerdr()
        out = inject.deliver("slot1", "wD:p1", TEXT, run=fake, lang="zh")
        self.assertTrue(out.delivered)
        self.assertEqual(out.reason, "delivered")
        self.assertEqual(out.cli, "claude")
        self.assertEqual(fake.subcommands(), ["pane list", "agent prompt"])
        # TARGET 是 pane_id；正文只加来源前缀（协议，不翻译），原文一个字不改、不加 from:
        self.assertEqual(inject.REMOTE_PREFIX, "[agent-ntfy remote] ")
        self.assertEqual(fake.calls[1], ["herdr", "agent", "prompt", "wD:p1", "[agent-ntfy remote] " + TEXT])
        self.assertEqual(fake.calls[1][4], inject.REMOTE_PREFIX + TEXT)

    def test_kimi_gets_prompt_then_ctrl_s_in_that_order(self):
        fake = FakeHerdr()
        out = inject.deliver("slot2", "wD:p2", TEXT, run=fake, lang="zh")
        self.assertTrue(out.delivered)
        self.assertEqual(out.cli, "kimi")
        self.assertEqual(fake.subcommands(), ["pane list", "agent prompt", "agent send-keys"])
        self.assertEqual(fake.calls[2], ["herdr", "agent", "send-keys", "wD:p2", "ctrl+s"])

    def test_unknown_agent_field_is_prompt_only(self):
        fake = FakeHerdr()
        out = inject.deliver("slot3", "wD:p3", TEXT, run=fake, lang="zh")
        self.assertTrue(out.delivered)
        self.assertIsNone(out.cli)
        self.assertEqual(fake.subcommands(), ["pane list", "agent prompt"])

    def test_prompt_failure_carries_error_code_not_text(self):
        fake = FakeHerdr()
        fake.prompt_result = HerdrResult(rc=1, stdout="", stderr=herdr_error("agent:prompt", "agent_blocked", "agent is blocked"))
        out = inject.deliver("slot1", "wD:p1", TEXT, run=fake, lang="zh")
        self.assertFalse(out.delivered)
        self.assertEqual(out.reason, "prompt_failed")
        self.assertIn("agent_blocked", out.detail)
        self.assertNotIn(TEXT, out.detail)
        self.assertEqual(fake.subcommands(), ["pane list", "agent prompt"])

    def test_prompt_usage_error_without_json_reports_rc_only(self):
        fake = FakeHerdr()
        fake.prompt_result = HerdrResult(rc=2, stdout="", stderr=f"unknown option: {TEXT}")
        out = inject.deliver("slot1", "wD:p1", TEXT, run=fake, lang="zh")
        self.assertEqual(out.reason, "prompt_failed")
        self.assertIn("退出码 2", out.detail)
        self.assertNotIn(TEXT, out.detail)  # stderr 里可能带正文，回执只取 code 或退出码

    def test_prompt_timeout_is_uncertain_not_undelivered(self):
        fake = FakeHerdr()
        fake.prompt_result = HerdrResult(rc=124, stdout="", stderr="", timed_out=True)
        out = inject.deliver("slot1", "wD:p1", TEXT, run=fake, lang="zh")
        self.assertFalse(out.delivered)
        self.assertEqual(out.reason, "prompt_timeout")
        self.assertIn("15 秒无响应", out.detail)
        self.assertNotIn("无法确认", out.detail)  # 那句由 render_receipt 统一追加，detail 里不再说一遍

    def test_kimi_wake_failure_is_receipt_with_code(self):
        fake = FakeHerdr()
        fake.keys_result = HerdrResult(rc=1, stdout="", stderr=herdr_error("agent:send-keys", "agent_not_found"))
        out = inject.deliver("slot2", "wD:p2", TEXT, run=fake, lang="zh")
        self.assertFalse(out.delivered)
        self.assertEqual(out.reason, "wake_failed")
        self.assertIn("agent_not_found", out.detail)
        self.assertIn("再发一次", out.detail)
        self.assertEqual(fake.subcommands(), ["pane list", "agent prompt", "agent send-keys"])


class PaneHelpersTest(unittest.TestCase):
    """CLI 侧的两个 herdr helper：开一个新窗格、往窗格里敲一条命令。daemon 不用它们。"""

    def test_split_pane_returns_new_pane_id(self):
        fake = FakeHerdr()
        self.assertEqual(inject.split_pane("/w/proj", "wD:p1", run=fake), "wD:p7")
        self.assertEqual(fake.calls, [["herdr", "pane", "split", "--pane", "wD:p1", "--direction", "down", "--cwd", "/w/proj", "--no-focus"]])

    def test_split_pane_failure_is_none(self):
        fake = FakeHerdr()
        fake.split_result = HerdrResult(rc=1, stdout="", stderr=herdr_error("pane:split", "pane_not_found"))
        self.assertIsNone(inject.split_pane("/w/proj", "wX:p9", run=fake))
        fake.split_result = HerdrResult(rc=0, stdout="not json", stderr="")
        self.assertIsNone(inject.split_pane("/w/proj", "wD:p1", run=fake))
        fake.split_result = HerdrResult(rc=0, stdout='{"id":"cli:pane:split","result":{"type":"pane_info"}}', stderr="")
        self.assertIsNone(inject.split_pane("/w/proj", "wD:p1", run=fake))
        fake.split_result = HerdrResult(rc=124, stdout="", stderr="", timed_out=True)
        self.assertIsNone(inject.split_pane("/w/proj", "wD:p1", run=fake))

    # pane run 把各参数按空格拼起来原样敲进窗格 shell、不做引用（实测）：argv 逐个 quote 后合成一个参数传过去
    def test_run_in_pane_quotes_argv_into_one_shell_word_list(self):
        fake = FakeHerdr()
        argv = ["/usr/bin/python3", "/w/my proj/agent_ntfy.py", "--home", "/Users/x/.agent-ntfy", "confirm-sub", "slot2", "it's"]
        self.assertTrue(inject.run_in_pane("wD:p7", argv, run=fake))
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fake.calls[0][:4], ["herdr", "pane", "run", "wD:p7"])
        self.assertEqual(len(fake.calls[0]), 5)  # 整条命令是一个参数
        self.assertEqual(split_pane_command(fake.calls[0][4]), argv)  # 按该平台的规则拆回来仍是原 argv：空格 / 单引号都没走样
        self.assertNotIn("$", fake.calls[0][4])

    # 以 = 开头的词 shlex.quote 不会加引号，而 zsh 会对它做等值展开（=ls → /bin/ls）：这类词强制用单引号包住
    def test_run_in_pane_quotes_words_starting_with_equals(self):
        fake = FakeHerdr()
        self.assertTrue(inject.run_in_pane("wD:p7", ["echo", "=ls", "a=b", "=it's"], run=fake))
        command = fake.calls[0][4]
        if sys.platform != "win32":  # 等值展开是 zsh 的事，Windows 那边按 list2cmdline 拼、没有这条规则
            self.assertIn("'=ls'", command)
        self.assertIn(" a=b ", " " + command + " ")  # 不以 = 开头的照旧
        self.assertEqual(split_pane_command(command), ["echo", "=ls", "a=b", "=it's"])

    def test_run_in_pane_failure_is_false(self):
        fake = FakeHerdr()
        fake.run_result = HerdrResult(rc=1, stdout="", stderr=herdr_error("pane:run", "pane_not_found"))
        self.assertFalse(inject.run_in_pane("wX:p9", ["true"], run=fake))
        fake.run_result = HerdrResult(rc=124, stdout="", stderr="", timed_out=True)
        self.assertFalse(inject.run_in_pane("wD:p7", ["true"], run=fake))


class ControlMarkTest(unittest.TestCase):
    def test_mark_roundtrip(self):
        self.assertEqual(inject.control_mark("release", "slot3"), "__agent-ntfy:release:slot3__")
        self.assertEqual(inject.parse_control_mark("__agent-ntfy:release:slot3__"), ("release", "slot3"))
        self.assertEqual(inject.parse_control_mark("__agent-ntfy:ignore:slot12__"), ("ignore", "slot12"))

    def test_only_exact_form_is_a_mark(self):
        for text in ("__agent-ntfy:release:slot3__\n", " __agent-ntfy:release:slot3__", "请 __agent-ntfy:release:slot3__",
                     "__agent-ntfy:delete:slot3__", "__agent-ntfy:release:slot__", "__agent-ntfy:release:slot0__",
                     "__agent-ntfy:release:__", "agent-ntfy:release:slot3", "", "释放这个槽位"):
            self.assertIsNone(inject.parse_control_mark(text), text)

    def test_unknown_action_cannot_be_built(self):
        with self.assertRaises(ValueError):
            inject.control_mark("delete", "slot3")


class ReceiptTest(unittest.TestCase):
    URL = "https://ntfy.example/t"

    def test_pane_missing_receipt_has_release_and_ignore_buttons(self):
        out = Outcome(delivered=False, reason="pane_missing", target="wD:p1", cli=None, detail="这个槽位绑定的目标 wD:p1 已经不存在了。", lang="zh")
        r = inject.render_receipt("slot3", out, reply_url=self.URL)
        self.assertEqual(r.title, "[slot3] 消息未送达")
        self.assertEqual([a["label"] for a in r.actions], [Z("receipt.button.release"), Z("receipt.button.ignore")])
        self.assertEqual([a["body"] for a in r.actions], ["__agent-ntfy:release:slot3__", "__agent-ntfy:ignore:slot3__"])
        self.assertTrue(all(a["url"] == self.URL and a["action"] == "http" for a in r.actions))
        # 正文首行（原因）加粗，尾句另起一段（Markdown 下单个换行会折成空格）
        self.assertEqual(r.message, f"**{out.detail}**\n\n" + Z("receipt.not_delivered"))
        self.assertEqual(r.body, r.message)

    def test_no_lease_receipt_has_only_ignore(self):
        out = Outcome(delivered=False, reason="no_lease", target=None, cli=None, detail="槽位 slot4 目前没有绑定任何 agent（没有租约）。", lang="zh")
        r = inject.render_receipt("slot4", out, reply_url=self.URL)
        self.assertEqual([a["label"] for a in r.actions], [Z("receipt.button.ignore")])

    def test_wake_failed_receipt_has_only_ignore_and_no_undelivered_line(self):
        out = Outcome(delivered=False, reason="wake_failed", target="wD:p2", cli="kimi", detail="唤醒失败", lang="zh")
        r = inject.render_receipt("slot2", out, reply_url=self.URL)
        self.assertEqual([a["label"] for a in r.actions], [Z("receipt.button.ignore")])
        self.assertNotIn(Z("receipt.not_delivered"), r.message)

    def test_uncertain_reasons_use_maybe_undelivered_title(self):
        # 通知栏只看得到 Title：消息其实可能已送达的三种情形不能写死「未送达」
        for reason in ("prompt_timeout", "wake_failed", "error"):
            out = Outcome(delivered=False, reason=reason, target="wD:p1", cli=None, detail="x", lang="zh")
            self.assertEqual(inject.render_receipt("slot1", out, reply_url=self.URL).title, "[slot1] 消息可能未送达", reason)
        for reason in ("no_lease", "no_herdr", "pane_missing", "prompt_failed"):
            out = Outcome(delivered=False, reason=reason, target="wD:p1", cli=None, detail="x", lang="zh")
            self.assertEqual(inject.render_receipt("slot1", out, reply_url=self.URL).title, "[slot1] 消息未送达", reason)

    def test_stopping_receipt_has_only_ignore(self):
        r = inject.render_stopping_receipt("slot3", reply_url=self.URL, lang="zh")
        self.assertEqual(r.title, "[slot3] 消息未送达")
        self.assertEqual(r.message, "**daemon 正在停止，你刚才的消息未送达，请稍后再发。**")
        self.assertEqual([a["label"] for a in r.actions], [Z("receipt.button.ignore")])
        self.assertEqual(r.actions[0]["body"], "__agent-ntfy:ignore:slot3__")
        r2 = inject.render_stopping_receipt("slot3", reply_url=self.URL, lang="zh", uncertain=True)
        self.assertEqual(r2.title, "[slot3] 消息可能未送达")
        self.assertIn("无法确认", r2.message)
        self.assertEqual([a["label"] for a in r2.actions], [Z("receipt.button.ignore")])

    def test_prompt_timeout_receipt_says_uncertain(self):
        out = Outcome(delivered=False, reason="prompt_timeout", target="wD:p1", cli="claude", detail="herdr 15 秒无响应", lang="zh")
        r = inject.render_receipt("slot1", out, reply_url=self.URL)
        self.assertEqual([a["label"] for a in r.actions], [Z("receipt.button.release"), Z("receipt.button.ignore")])
        self.assertEqual(r.message, "**herdr 15 秒无响应**\n\n" + Z("receipt.uncertain"))
        self.assertNotIn(Z("receipt.not_delivered"), r.message)

    def test_delivered_outcome_has_no_receipt(self):
        out = Outcome(delivered=True, reason="delivered", target="wD:p1", cli="claude", detail="", lang="zh")
        with self.assertRaises(ValueError):
            inject.render_receipt("slot1", out, reply_url=self.URL)

    def test_closed_receipt_drops_buttons_and_prefixes_title(self):
        out = Outcome(delivered=False, reason="pane_missing", target="wD:p1", cli=None, detail="目标不在了。", lang="zh")
        r = inject.render_receipt("slot3", out, reply_url=self.URL)
        closed = inject.render_receipt_closed(r, prefix=Z("prefix.released"), result="槽位 slot3 已释放。")
        self.assertEqual(closed.title, Z("prefix.released") + "[slot3] 消息未送达")
        self.assertEqual(closed.actions, [])
        # 结果句加粗打头，分隔线前后空行，原回执正文保留，事后能翻
        self.assertEqual(closed.message, "**槽位 slot3 已释放。**\n\n---\n\n" + r.body)
        self.assertEqual(closed.body, r.body)


class ConfirmRequestTest(unittest.TestCase):
    URL = "https://ntfy.example/t"

    def test_confirmed_is_a_control_mark(self):
        self.assertEqual(inject.control_mark("confirmed", "slot3"), "__agent-ntfy:confirmed:slot3__")
        self.assertEqual(inject.parse_control_mark("__agent-ntfy:confirmed:slot3__"), ("confirmed", "slot3"))
        self.assertIsNone(inject.parse_control_mark("__agent-ntfy:confirmed:slot3__ 收到"))

    def test_confirm_request_has_one_button_and_explains_itself(self):
        r = inject.render_confirm_request("slot3", reply_url=self.URL, lang="zh")
        self.assertEqual(r.title, "[slot3] 确认你能收到通知")
        self.assertEqual([a["label"] for a in r.actions], [Z("confirm.button")])
        self.assertEqual(r.actions[0]["body"], "__agent-ntfy:confirmed:slot3__")
        self.assertEqual(r.actions[0]["url"], self.URL)
        # 写给没看过任何文档的人：这条是什么、点按钮意味着什么、没弹通知怎么办
        self.assertIn("slot3", r.message)
        self.assertIn("测试消息", r.message)
        self.assertIn(Z("confirm.button"), r.message)
        self.assertIn("通知栏", r.message)
        self.assertIn("README", r.message)
        self.assertEqual(r.body, r.message)
        # 首行（这条是什么）加粗，其余照 texts 原样
        first, _, rest = Z("confirm.body", slot="slot3", button=Z("confirm.button")).partition("\n")
        self.assertEqual(r.message, f"**{first}**\n{rest}")
        self.assertEqual(r.message.count("**"), 2)

    # 三段之间空一行、段内没有单个换行：Markdown 把单个换行折成空格，中文标点后会多出一个空格
    def test_confirm_request_paragraphs_have_no_soft_line_breaks(self):
        for lang in ("zh", "en"):
            with self.subTest(lang=lang):
                r = inject.render_confirm_request("slot3", reply_url=self.URL, lang=lang)
                self.assertEqual(r.message.count("\n\n"), 2)
                self.assertIsNone(re.search(r"[^\n]\n[^\n]", r.message), r.message)

    def test_confirm_request_fits_the_question_budget(self):
        from render import QUESTION_MAX_BYTES
        r = inject.render_confirm_request("slot12345", reply_url=self.URL, lang="zh")
        self.assertLessEqual(len(r.message.encode("utf-8")), QUESTION_MAX_BYTES)


class RunHerdrTest(unittest.TestCase):
    """只验子进程包装本身，用无害的本地命令（一律 sys.executable -c，不依赖 sh / sleep 这类 POSIX 工具），不碰 herdr。"""

    # 子进程看自己的 stdin：是不是字符设备（/dev/null 与 NUL 都是；继承来的管道是 FIFO），以及 POSIX 上是否与 os.devnull 同一 inode。
    # Windows 上 st_ino 可能为 0、os.stat(os.devnull) 是否可用未核，same 只记不判。
    DEVNULL_PROBE = (
        "import os, stat\n"
        "st = os.fstat(0)\n"
        "try:\n"
        "    same = os.path.samestat(st, os.stat(os.devnull))\n"
        "except OSError as e:\n"
        "    same = type(e).__name__\n"
        "print('chr=%s same=%s' % (stat.S_ISCHR(st.st_mode), same))"
    )

    def test_missing_binary_is_rc_127(self):
        r = inject.run_herdr(["definitely-no-such-binary-agent-ntfy", "pane", "list"])
        self.assertEqual(r.rc, 127)
        self.assertFalse(r.ok)
        self.assertFalse(r.timed_out)
        self.assertIn("没装", r.summary("zh"))

    def test_timeout_is_flagged_with_the_actual_limit(self):
        r = inject.run_herdr([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.2)
        self.assertTrue(r.timed_out)
        self.assertFalse(r.ok)
        self.assertEqual(r.summary("zh"), "herdr 0.2 秒无响应")

    def test_child_stdin_is_devnull_not_inherited(self):
        # 外层给一个管道当 stdin；里面 run_herdr 起的子进程若继承了它，看到的就是 FIFO 而不是字符设备
        probe = (f"import sys, inject; r = inject.run_herdr([sys.executable, '-c', {self.DEVNULL_PROBE!r}], timeout=5); "
                 "print(r.stdout.strip(), r.timed_out); sys.stderr.write(r.stderr)")  # 探针自己炸了时把 traceback 带出来
        outer = subprocess.run([sys.executable, "-c", probe], cwd=os.path.dirname(inject.__file__), stdin=subprocess.PIPE,
                               capture_output=True, text=True, timeout=20)
        out = outer.stdout.strip()
        self.assertRegex(out, r"^chr=True same=\S+ False$", outer.stderr)  # 三平台：字符设备、没超时
        if sys.platform != "win32":
            self.assertEqual(out, "chr=True same=True False", outer.stderr)  # POSIX：与 `test -ef` 同强度
        else:
            print("devnull probe:", out)  # Windows 上 same 的实际值只进日志

    def test_undecodable_stderr_does_not_raise(self):
        r = inject.run_herdr([sys.executable, "-c", "import sys; sys.stderr.buffer.write(b'\\x81\\xff'); sys.exit(1)"])  # UTF-8 与 cp1252 下都非法
        self.assertEqual((r.rc, r.ok), (1, False))
        self.assertEqual(r.summary("zh"), "退出码 1")

    def test_stdout_and_rc_are_captured(self):
        r = inject.run_herdr([sys.executable, "-c", "import sys; sys.stdout.write('{\"a\":1}'); sys.stderr.write('e'); sys.exit(3)"])
        self.assertEqual((r.rc, r.stdout, r.stderr, r.ok), (3, '{"a":1}', "e", False))
        self.assertIsNone(r.error_code())
        self.assertEqual(r.summary("zh"), "退出码 3")

    def test_error_code_is_read_from_stderr_json(self):
        r = HerdrResult(rc=1, stdout="", stderr=herdr_error("agent:prompt", "agent_blocked"))
        self.assertEqual(r.error_code(), "agent_blocked")
        self.assertEqual(r.summary("zh"), "agent_blocked")


if __name__ == "__main__":
    unittest.main()
