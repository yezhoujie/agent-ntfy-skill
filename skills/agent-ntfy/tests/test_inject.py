"""注入层：herdr 子进程调用、能力判定与分档、投递失败回执、控制标记。

herdr 用可替换的调用器替身：记录 argv、按预置返回 rc / stdout / stderr。真 herdr 注入不在这里做——
往任何活着的 pane 注都会打扰一个正在工作的 agent；只有 run_herdr() 本身用无害的本地命令验证子进程包装。
"""

import json
import unittest

import inject
from inject import HerdrResult, Outcome

TEXT = "把 B 方案也列进去，不要只给 A"
PANES_STDOUT = json.dumps({"id": "cli:pane:list", "result": {"panes": [
    {"pane_id": "wD:p1", "agent": "claude", "agent_status": "working", "workspace_id": "wD"},
    {"pane_id": "wD:p2", "agent": "kimi", "agent_status": "idle", "workspace_id": "wD"},
    {"pane_id": "wD:p3", "agent_status": "unknown", "workspace_id": "wD"},
], "type": "pane_list"}})  # herdr 0.9.0 实物：type 在 result 里


def herdr_error(cmd: str, code: str, message: str = "") -> str:
    """herdr 0.9.0 的失败形态：stdout 空、stderr 一行 JSON。"""
    return json.dumps({"id": f"cli:{cmd}", "error": {"code": code, "message": message or code}})


class FakeHerdr:
    """按子命令预置结果的调用器。calls 记录每次 argv，顺序就是调用顺序。"""

    def __init__(self, *, panes_stdout=PANES_STDOUT):
        self.calls: list[list[str]] = []
        self.list_result = HerdrResult(rc=0, stdout=panes_stdout, stderr="")
        self.prompt_result = HerdrResult(rc=0, stdout='{"id":"cli:agent:prompt","result":{"agent":{},"type":"agent_prompted"}}', stderr="")
        self.keys_result = HerdrResult(rc=0, stdout='{"id":"cli:agent:send-keys","result":{"agent":{},"type":"agent_keys_sent"}}', stderr="")

    def __call__(self, argv: list[str]) -> HerdrResult:
        self.calls.append(list(argv))
        sub = tuple(argv[1:3])
        if sub == ("pane", "list"):
            return self.list_result
        if sub == ("agent", "prompt"):
            return self.prompt_result
        if sub == ("agent", "send-keys"):
            return self.keys_result
        raise AssertionError(f"没预置的 herdr 调用：{argv}")

    def subcommands(self) -> list[str]:
        return [" ".join(c[1:3]) for c in self.calls]


class DeliverTest(unittest.TestCase):
    def test_no_lease_is_receipt_without_touching_herdr(self):
        fake = FakeHerdr()
        out = inject.deliver("slot3", None, TEXT, run=fake)
        self.assertFalse(out.delivered)
        self.assertEqual(out.reason, "no_lease")
        self.assertEqual(fake.calls, [])
        self.assertIn("slot3", out.detail)
        self.assertNotIn(TEXT, out.detail)

    def test_herdr_server_not_running_is_no_herdr(self):
        fake = FakeHerdr()
        fake.list_result = HerdrResult(rc=1, stdout="", stderr=herdr_error("pane:list", "server_not_running", "no herdr server is running at /x"))
        out = inject.deliver("slot1", "wD:p1", TEXT, run=fake)
        self.assertFalse(out.delivered)
        self.assertEqual(out.reason, "no_herdr")
        self.assertEqual(fake.subcommands(), ["pane list"])  # 没去 prompt
        self.assertIn("server_not_running", out.detail)
        self.assertIn("wD:p1", out.detail)

    def test_herdr_binary_missing_is_no_herdr(self):
        fake = FakeHerdr()
        fake.list_result = HerdrResult(rc=127, stdout="", stderr="herdr: command not found")
        out = inject.deliver("slot1", "wD:p1", TEXT, run=fake)
        self.assertEqual(out.reason, "no_herdr")
        self.assertIn("没装", out.detail)

    def test_unparseable_pane_list_is_no_herdr(self):
        fake = FakeHerdr(panes_stdout="not json")
        out = inject.deliver("slot1", "wD:p1", TEXT, run=fake)
        self.assertEqual(out.reason, "no_herdr")
        self.assertEqual(fake.subcommands(), ["pane list"])

    def test_pane_list_timeout_is_no_herdr(self):
        fake = FakeHerdr()
        fake.list_result = HerdrResult(rc=124, stdout="", stderr="", timed_out=True, timeout=15)
        out = inject.deliver("slot1", "wD:p1", TEXT, run=fake)
        self.assertEqual(out.reason, "no_herdr")
        self.assertIn("15 秒无响应", out.detail)
        self.assertEqual(fake.subcommands(), ["pane list"])

    def test_pane_missing_is_receipt_without_prompt(self):
        fake = FakeHerdr()
        out = inject.deliver("slot1", "wX:p9", TEXT, run=fake)
        self.assertFalse(out.delivered)
        self.assertEqual(out.reason, "pane_missing")
        self.assertEqual(fake.subcommands(), ["pane list"])
        self.assertIn("wX:p9", out.detail)
        self.assertIn("不在 herdr 的窗格列表里", out.detail)  # 对非 herdr 身份（host:…|sid:…）这句也为真，「已经不存在了」不是

    def test_pane_missing_wording_holds_for_non_herdr_identity(self):
        fake = FakeHerdr()
        out = inject.deliver("slot1", "host:mac|sid:123", TEXT, run=fake)
        self.assertEqual(out.reason, "pane_missing")
        self.assertIn("host:mac|sid:123 不在 herdr 的窗格列表里", out.detail)

    def test_claude_gets_prompt_only_with_pane_id_and_raw_text(self):
        fake = FakeHerdr()
        out = inject.deliver("slot1", "wD:p1", TEXT, run=fake)
        self.assertTrue(out.delivered)
        self.assertEqual(out.reason, "delivered")
        self.assertEqual(out.cli, "claude")
        self.assertEqual(fake.subcommands(), ["pane list", "agent prompt"])
        self.assertEqual(fake.calls[1], ["herdr", "agent", "prompt", "wD:p1", TEXT])  # TARGET 是 pane_id，正文原样、不加 from:

    def test_kimi_gets_prompt_then_ctrl_s_in_that_order(self):
        fake = FakeHerdr()
        out = inject.deliver("slot2", "wD:p2", TEXT, run=fake)
        self.assertTrue(out.delivered)
        self.assertEqual(out.cli, "kimi")
        self.assertEqual(fake.subcommands(), ["pane list", "agent prompt", "agent send-keys"])
        self.assertEqual(fake.calls[2], ["herdr", "agent", "send-keys", "wD:p2", "ctrl+s"])

    def test_unknown_agent_field_is_prompt_only(self):
        fake = FakeHerdr()
        out = inject.deliver("slot3", "wD:p3", TEXT, run=fake)
        self.assertTrue(out.delivered)
        self.assertIsNone(out.cli)
        self.assertEqual(fake.subcommands(), ["pane list", "agent prompt"])

    def test_prompt_failure_carries_error_code_not_text(self):
        fake = FakeHerdr()
        fake.prompt_result = HerdrResult(rc=1, stdout="", stderr=herdr_error("agent:prompt", "agent_blocked", "agent is blocked"))
        out = inject.deliver("slot1", "wD:p1", TEXT, run=fake)
        self.assertFalse(out.delivered)
        self.assertEqual(out.reason, "prompt_failed")
        self.assertIn("agent_blocked", out.detail)
        self.assertNotIn(TEXT, out.detail)
        self.assertEqual(fake.subcommands(), ["pane list", "agent prompt"])

    def test_prompt_usage_error_without_json_reports_rc_only(self):
        fake = FakeHerdr()
        fake.prompt_result = HerdrResult(rc=2, stdout="", stderr=f"unknown option: {TEXT}")
        out = inject.deliver("slot1", "wD:p1", TEXT, run=fake)
        self.assertEqual(out.reason, "prompt_failed")
        self.assertIn("退出码 2", out.detail)
        self.assertNotIn(TEXT, out.detail)  # stderr 里可能带正文，回执只取 code 或退出码

    def test_prompt_timeout_is_uncertain_not_undelivered(self):
        fake = FakeHerdr()
        fake.prompt_result = HerdrResult(rc=124, stdout="", stderr="", timed_out=True)
        out = inject.deliver("slot1", "wD:p1", TEXT, run=fake)
        self.assertFalse(out.delivered)
        self.assertEqual(out.reason, "prompt_timeout")
        self.assertIn("15 秒无响应", out.detail)
        self.assertNotIn("无法确认", out.detail)  # 那句由 render_receipt 统一追加，detail 里不再说一遍

    def test_kimi_wake_failure_is_receipt_with_code(self):
        fake = FakeHerdr()
        fake.keys_result = HerdrResult(rc=1, stdout="", stderr=herdr_error("agent:send-keys", "agent_not_found"))
        out = inject.deliver("slot2", "wD:p2", TEXT, run=fake)
        self.assertFalse(out.delivered)
        self.assertEqual(out.reason, "wake_failed")
        self.assertIn("agent_not_found", out.detail)
        self.assertIn("再发一次", out.detail)
        self.assertEqual(fake.subcommands(), ["pane list", "agent prompt", "agent send-keys"])


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
        out = Outcome(delivered=False, reason="pane_missing", target="wD:p1", cli=None, detail="这个槽位绑定的目标 wD:p1 已经不存在了。")
        r = inject.render_receipt("slot3", out, reply_url=self.URL)
        self.assertEqual(r.title, "[slot3] 消息未送达")
        self.assertEqual([a["label"] for a in r.actions], [inject.RELEASE_LABEL, inject.IGNORE_LABEL])
        self.assertEqual([a["body"] for a in r.actions], ["__agent-ntfy:release:slot3__", "__agent-ntfy:ignore:slot3__"])
        self.assertTrue(all(a["url"] == self.URL and a["action"] == "http" for a in r.actions))
        self.assertIn(out.detail, r.message)
        self.assertIn(inject.NOT_DELIVERED_LINE, r.message)
        self.assertEqual(r.body, r.message)

    def test_no_lease_receipt_has_only_ignore(self):
        out = Outcome(delivered=False, reason="no_lease", target=None, cli=None, detail="槽位 slot4 目前没有绑定任何 agent（没有租约）。")
        r = inject.render_receipt("slot4", out, reply_url=self.URL)
        self.assertEqual([a["label"] for a in r.actions], [inject.IGNORE_LABEL])

    def test_wake_failed_receipt_has_only_ignore_and_no_undelivered_line(self):
        out = Outcome(delivered=False, reason="wake_failed", target="wD:p2", cli="kimi", detail="唤醒失败")
        r = inject.render_receipt("slot2", out, reply_url=self.URL)
        self.assertEqual([a["label"] for a in r.actions], [inject.IGNORE_LABEL])
        self.assertNotIn(inject.NOT_DELIVERED_LINE, r.message)

    def test_uncertain_reasons_use_maybe_undelivered_title(self):
        # 通知栏只看得到 Title：消息其实可能已送达的三种情形不能写死「未送达」
        for reason in ("prompt_timeout", "wake_failed", "error"):
            out = Outcome(delivered=False, reason=reason, target="wD:p1", cli=None, detail="x")
            self.assertEqual(inject.render_receipt("slot1", out, reply_url=self.URL).title, "[slot1] 消息可能未送达", reason)
        for reason in ("no_lease", "no_herdr", "pane_missing", "prompt_failed"):
            out = Outcome(delivered=False, reason=reason, target="wD:p1", cli=None, detail="x")
            self.assertEqual(inject.render_receipt("slot1", out, reply_url=self.URL).title, "[slot1] 消息未送达", reason)

    def test_stopping_receipt_has_only_ignore(self):
        r = inject.render_stopping_receipt("slot3", reply_url=self.URL)
        self.assertEqual(r.title, "[slot3] 消息未送达")
        self.assertEqual(r.message, "daemon 正在停止，你刚才的消息未送达，请稍后再发。")
        self.assertEqual([a["label"] for a in r.actions], [inject.IGNORE_LABEL])
        self.assertEqual(r.actions[0]["body"], "__agent-ntfy:ignore:slot3__")
        r2 = inject.render_stopping_receipt("slot3", reply_url=self.URL, uncertain=True)
        self.assertEqual(r2.title, "[slot3] 消息可能未送达")
        self.assertIn("无法确认", r2.message)
        self.assertEqual([a["label"] for a in r2.actions], [inject.IGNORE_LABEL])

    def test_prompt_timeout_receipt_says_uncertain(self):
        out = Outcome(delivered=False, reason="prompt_timeout", target="wD:p1", cli="claude", detail="herdr 15 秒无响应")
        r = inject.render_receipt("slot1", out, reply_url=self.URL)
        self.assertEqual([a["label"] for a in r.actions], [inject.RELEASE_LABEL, inject.IGNORE_LABEL])
        self.assertIn(inject.UNCERTAIN_LINE, r.message)
        self.assertNotIn(inject.NOT_DELIVERED_LINE, r.message)

    def test_delivered_outcome_has_no_receipt(self):
        out = Outcome(delivered=True, reason="delivered", target="wD:p1", cli="claude", detail="")
        with self.assertRaises(ValueError):
            inject.render_receipt("slot1", out, reply_url=self.URL)

    def test_closed_receipt_drops_buttons_and_prefixes_title(self):
        out = Outcome(delivered=False, reason="pane_missing", target="wD:p1", cli=None, detail="目标不在了。")
        r = inject.render_receipt("slot3", out, reply_url=self.URL)
        closed = inject.render_receipt_closed(r, prefix=inject.RELEASED_PREFIX, result="槽位 slot3 已释放。")
        self.assertEqual(closed.title, inject.RELEASED_PREFIX + "[slot3] 消息未送达")
        self.assertEqual(closed.actions, [])
        self.assertTrue(closed.message.startswith("槽位 slot3 已释放。\n"))
        self.assertIn(r.body, closed.message)  # 原回执正文保留，事后能翻


class RunHerdrTest(unittest.TestCase):
    """只验子进程包装本身，用无害的本地命令，不碰 herdr。"""

    def test_missing_binary_is_rc_127(self):
        r = inject.run_herdr(["definitely-no-such-binary-agent-ntfy", "pane", "list"])
        self.assertEqual(r.rc, 127)
        self.assertFalse(r.ok)
        self.assertFalse(r.timed_out)
        self.assertIn("没装", r.summary())

    def test_timeout_is_flagged_with_the_actual_limit(self):
        r = inject.run_herdr(["sleep", "5"], timeout=0.2)
        self.assertTrue(r.timed_out)
        self.assertFalse(r.ok)
        self.assertEqual(r.summary(), "herdr 0.2 秒无响应")

    def test_child_stdin_is_devnull_not_inherited(self):
        # 外层给一个管道当 stdin；里面 run_herdr 起的子进程若继承了它，等输入的命令会一直挂到超时
        import os
        import subprocess
        import sys
        probe = ("import inject; r = inject.run_herdr(['sh', '-c', 'test /dev/stdin -ef /dev/null && echo devnull || echo other'], timeout=5); "
                 "print(r.stdout.strip(), r.timed_out)")
        outer = subprocess.run([sys.executable, "-c", probe], cwd=os.path.dirname(inject.__file__), stdin=subprocess.PIPE,
                               capture_output=True, text=True, timeout=20)
        self.assertEqual(outer.stdout.strip(), "devnull False", outer.stderr)

    def test_undecodable_stderr_does_not_raise(self):
        r = inject.run_herdr(["sh", "-c", "printf '\\xff\\xfe' >&2; exit 1"])
        self.assertEqual((r.rc, r.ok), (1, False))
        self.assertEqual(r.summary(), "退出码 1")

    def test_stdout_and_rc_are_captured(self):
        r = inject.run_herdr(["sh", "-c", "printf '{\"a\":1}'; printf 'e' >&2; exit 3"])
        self.assertEqual((r.rc, r.stdout, r.stderr, r.ok), (3, '{"a":1}', "e", False))
        self.assertIsNone(r.error_code())
        self.assertEqual(r.summary(), "退出码 3")

    def test_error_code_is_read_from_stderr_json(self):
        r = HerdrResult(rc=1, stdout="", stderr=herdr_error("agent:prompt", "agent_blocked"))
        self.assertEqual(r.error_code(), "agent_blocked")
        self.assertEqual(r.summary(), "agent_blocked")


if __name__ == "__main__":
    unittest.main()
