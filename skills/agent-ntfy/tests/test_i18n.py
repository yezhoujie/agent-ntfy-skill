"""固定文案的语言切换：zh 字节级回归、en 不含汉字、三档优先级、同 seq 更新沿用首发语言、lang 字段校验、字节预算。"""

import json
import os
import re
import unittest

from pathlib import Path

import agent_ntfy
import daemon
import inject
import render
import texts
import validate
from inject import HerdrResult, Outcome
import tests.test_agent_ntfy as ta
from tests.test_agent_ntfy import HERDR, run
from tests.test_daemon import Harness, wait_until
from tests.test_inject import FakeHerdr, herdr_error
from tests.test_render import SAMPLE

from tests.test_texts import HAN

URL = "https://ntfy.example/t"

# zh 渲染结果的黄金样本：一个字都不许变（固定文案只是搬进了表里）。Markdown 版式：加粗分段标记、有序列表、--- 分隔线，段间空行
ZH_GOLDEN = (
    "**【正在做】** " + SAMPLE["doing"] + "\n\n"
    "**【背景】** " + SAMPLE["description"] + "\n\n"
    "**【卡点】** " + SAMPLE["blocker"] + "\n\n"
    "**【选项】**\n\n"
    "1. **留固定目录**（推荐）→ " + SAMPLE["options"][0]["consequence"] + "\n"
    "2. **用完即删** → " + SAMPLE["options"][1]["consequence"] + "\n\n"
    "**【我的建议】** " + SAMPLE["reasoning"] + "\n\n"
    "**【要你定】** " + SAMPLE["question"] + "\n\n"
    "---\n\n"
    "⚠️ 按钮是快捷选项。\n\n"
    "有别的意见请在下方输入框直接回复。\n\n"
    "回复发出即生效，不能撤回、也无法追加——请一次说完。"
)


def cjk(text: str) -> int:
    return len(HAN.findall(text))


class ModuleHygieneTest(unittest.TestCase):
    def test_no_foreign_testcases_are_re_exported(self):
        # 把别的模块的 TestCase 导进本模块的命名空间，unittest 会在这里再跑一遍它们（实测 12 条重复）
        import sys
        me = sys.modules[__name__]
        foreign = [n for n, v in vars(me).items() if isinstance(v, type) and issubclass(v, unittest.TestCase) and v.__module__ != __name__]
        self.assertEqual(foreign, [])


class ZhRegressionTest(unittest.TestCase):
    def test_question_renders_byte_identical(self):
        r = render.render_question(SAMPLE, tag="wD", reply_url=URL, lang="zh")
        self.assertEqual(r.message, ZH_GOLDEN)
        self.assertEqual(r.title, "[wD] " + SAMPLE["title"])
        self.assertEqual(r.actions[0]["label"], "采纳推荐")
        answered = render.render_answered(r, "留固定目录")
        self.assertEqual(answered.title, "✅ 已回复 · [wD] " + SAMPLE["title"])
        self.assertTrue(answered.message.startswith("**【你的回复】** 留固定目录\n\n---\n\n（以下为当时的提问）\n\n**【正在做】** "))

    def test_zh_column_matches_the_golden_snapshot(self):
        # 改任何一个中文字都必须显式改 tests/zh_golden.json（`python3 tests/zh_golden.py --write`），不能顺手润色
        from pathlib import Path
        golden = json.loads((Path(__file__).parent / "zh_golden.json").read_text(encoding="utf-8"))
        self.assertEqual(set(golden), set(texts.TEXTS["zh"]), "key 集合变了：新增 / 删除 key 也要更新快照")
        for key, value in texts.TEXTS["zh"].items():
            self.assertEqual(value, golden[key], key)

    def test_validation_wording_byte_identical(self):
        text = validate.format_problems(validate.check({"title": "x", "options": [{"id": "a", "label": "A", "consequence": "c"}], "recommend": "nope"}, "zh"), "zh")
        self.assertTrue(text.startswith("agent-ntfy ask: 输入校验未通过（7 处），全部修正后重试，消息未发送。\n\n"))
        self.assertIn("  options    : 只有 1 项，要求 2~5 项（只有一个选项不叫选择）\n", text)
        self.assertIn('  recommend  : "nope" 不在 options 的 id 里（现有 id: a）\n', text)
        self.assertIn("  doing      : 缺失。必填，一句话说这是哪件事\n", text)


class EnglishTest(unittest.TestCase):
    """en 下每一个固定串都不含汉字（agent 自己写的内容字段除外）。"""

    def test_question_and_answered(self):
        payload = {**SAMPLE, "title": "Keep or drop the temp dir", "doing": "d", "description": "e", "blocker": "b",
                   "options": [{"id": "keep", "label": "Keep", "consequence": "kept"}, {"id": "temp", "label": "Drop", "consequence": "gone"}],
                   "reasoning": "r", "question": "q?"}
        r = render.render_question(payload, tag="wD", reply_url=URL, lang="en")
        self.assertEqual(cjk(r.message + r.title + r.actions[0]["label"]), 0, r.message)
        self.assertIn("**[Doing]** d", r.message)
        self.assertIn("1. **Keep** (recommended) → kept\n2. **Drop** → gone", r.message)
        self.assertIn("**[Your call]** q?\n\n---\n\n⚠️ The button is a shortcut.", r.message)
        self.assertEqual(r.actions[0], {"action": "http", "label": "Accept recommended", "url": URL, "method": "POST", "body": "Keep"})
        self.assertIn("**[My recommendation]** r", r.message)
        a = render.render_answered(r, "Keep")
        self.assertEqual(a.title, "✅ Answered · [wD] Keep or drop the temp dir")
        self.assertTrue(a.message.startswith("**[Your reply]** Keep\n\n---\n\n(the question as asked)\n\n**[Doing]** d"))
        self.assertEqual(cjk(a.message + a.title), 0)

    def test_receipts_and_confirm_message(self):
        fake = FakeHerdr()
        fake.prompt_result = HerdrResult(rc=1, stdout="", stderr=herdr_error("agent:prompt", "agent_blocked"))
        outs = [inject.deliver("slot1", None, "x", run=fake, lang="en"),
                inject.deliver("slot1", "wX:p9", "x", run=fake, lang="en"),
                inject.deliver("slot1", "wD:p1", "x", run=fake, lang="en"),
                Outcome(False, "prompt_timeout", "wD:p1", None, HerdrResult(rc=124, stdout="", stderr="", timed_out=True, timeout=15).summary("en"), "en"),
                Outcome(False, "error", None, None, texts.t("receipt.error", "en", error="ValueError"), "en")]
        fake.list_result = HerdrResult(rc=1, stdout="", stderr=herdr_error("pane:list", "server_not_running"))
        outs.append(inject.deliver("slot1", "wD:p1", "x", run=fake, lang="en"))
        for out in outs:
            r = inject.render_receipt("slot1", out, reply_url=URL)
            blob = r.title + r.message + "".join(a["label"] for a in r.actions)
            self.assertEqual(cjk(blob), 0, (out.reason, blob))
        self.assertEqual(inject.render_receipt("slot1", outs[0], reply_url=URL).title, "[slot1] Message not delivered")
        self.assertEqual(inject.render_receipt("slot1", outs[3], reply_url=URL).title, "[slot1] Message may not have been delivered")
        for uncertain in (False, True):
            r = inject.render_stopping_receipt("slot2", reply_url=URL, lang="en", uncertain=uncertain)
            self.assertEqual(cjk(r.title + r.message + r.actions[0]["label"]), 0)
        c = inject.render_confirm_request("slot3", reply_url=URL, lang="en")
        self.assertEqual(cjk(c.title + c.message + c.actions[0]["label"]), 0, c.message)
        self.assertEqual((c.title, c.actions[0]["label"], c.actions[0]["body"]), ("[slot3] Confirm you get notifications", "Got it", "__agent-ntfy:confirmed:slot3__"))
        self.assertLessEqual(len(c.message.encode("utf-8")), render.QUESTION_MAX_BYTES)

    def test_validation_errors(self):
        payload, problems, lang = validate.check_json('{"title": "x", "options": [{"id": "a"}], "recommend": "nope", "lang": "en"}', "zh")
        self.assertEqual(lang, "en")
        text = validate.format_problems(problems, lang)
        self.assertEqual(cjk(text), 0, text)
        self.assertIn("Message NOT sent", text)
        self.assertIn("  body       :", validate.format_problems(validate.check({**SAMPLE, "description": "x" * 4000}, "en"), "en"))
        for bad in ('{"title":', "[1]"):
            _, problems, lang = validate.check_json(bad, "en")
            self.assertEqual(cjk(validate.format_problems(problems, lang)), 0)

    def test_cli_human_readable_outputs(self):
        h = Harness(self, lang="en")
        env = {"AGENT_NTFY_LANG": "en"}
        code, out, err = run(["--home", str(h.home), "slots"], env=env)
        self.assertEqual((code, cjk(out)), (0, 0), out)
        self.assertIn("unassigned", out)
        self.assertIn("confirmed", out)
        code, out, err = run(["--home", str(h.home), "daemon", "--status"], env=env)
        self.assertEqual((code, cjk(out)), (0, 0), out)
        self.assertIn("subscription: connected", out)
        code, out, err = run(["--home", str(h.home), "confirm-sub", "slot4"], env=env)  # 非 TTY 提示
        self.assertEqual((code, cjk(err)), (4, 0), err)
        code, out, err = run(["--home", str(h.home), "release", "slot9"], env=env)
        self.assertEqual((code, cjk(err)), (1, 0), err)
        code, out, err = run(["--home", str(h.home), "add-slot"], env=env)
        self.assertEqual((code, cjk(out)), (0, 0), out)
        code, out, err = run(["--home", "/nonexistent/agent-ntfy-home", "ask"], json.dumps({**SAMPLE, "lang": "en"}), env=env)
        self.assertEqual((code, cjk(err)), (3, 0), err)
        self.assertIn("Message NOT sent", err)

    def test_cli_protocol_error_wording_is_from_the_table(self):
        from unittest import mock
        h = Harness(self, lang="en")
        def broken(sock):
            raise agent_ntfy.ProtocolError("not_object")
            yield  # noqa: unreachable，只为让它是生成器

        with mock.patch("agent_ntfy.read_events", broken):  # daemon 回了个非对象
            code, out, err = run(["--home", str(h.home), "ask", "--timeout", "5"], json.dumps({**SAMPLE, "lang": "en"}), {**HERDR, "AGENT_NTFY_LANG": "en"})
        self.assertEqual((code, cjk(err)), (3, 0), err)

    def test_daemon_socket_messages(self):
        h = Harness(self, subscribed=(), lang="en")
        sock, first, events = h.ask(payload={**SAMPLE, "lang": "en"})
        self.assertEqual((first["kind"], cjk(first["message"])), ("unconfirmed", 0), first)
        sock.close()
        for req in ({"cmd": "release", "slot": "slot9"}, {"cmd": "release", "slot": "slot1"}, {"cmd": "nonsense"}, {"cmd": "confirm-sub", "slot": "slot9"}):
            ev = h.request(**req, lang="en")[0]
            self.assertEqual(cjk(str(ev.get("message"))), 0, ev)
        sock, first, _ = h.ask(payload={**SAMPLE, "lang": "en", "recommend": "nope", "reasoning": ""})
        self.assertEqual((first["kind"], cjk(first["message"])), ("invalid_input", 0), first)
        sock.close()
        with h.connect() as raw:  # 非对象的请求行：报错文案也不能混进中文
            raw.sendall(b"[1, 2]\n")
            ev = next(agent_ntfy.read_events(raw))
        self.assertEqual((ev["kind"], cjk(ev["message"])), ("bad_request", 0), ev)
        h2 = Harness(self, pool_size=1, subscribed=("slot1",), lang="en")
        h2.state.acquire("someone")
        sock, first, _ = h2.ask(leased_by="wD:p9", payload={**SAMPLE, "lang": "en"})
        self.assertEqual((first["kind"], cjk(first["message"])), ("no_free_slot", 0), first)
        sock.close()


class LangFieldTest(unittest.TestCase):
    def test_default_zh_en_pass_and_others_fail_together(self):
        for lang in ("zh", "en", None):  # null 当没给
            self.assertEqual(validate.check({**SAMPLE, "lang": lang}, "zh"), [])
        self.assertEqual(validate.check(SAMPLE, "zh"), [])
        problems = validate.check({**SAMPLE, "lang": "jp", "recommend": "nope"}, "zh")
        self.assertEqual([p.field for p in problems], ["recommend", "lang"])
        self.assertIn("zh / en", problems[1].message)
        self.assertIn('"jp"', problems[1].message)
        text = validate.format_problems(problems, "zh")
        self.assertTrue(text.startswith("agent-ntfy ask: 输入校验未通过（2 处）"))

    def test_error_language_follows_json_then_env_then_default(self):
        _, problems, lang = validate.check_json(json.dumps({**SAMPLE, "lang": "en", "title": ""}), "zh")
        self.assertEqual((lang, cjk(problems[0].message)), ("en", 0))
        _, problems, lang = validate.check_json(json.dumps({**SAMPLE, "lang": "jp", "title": ""}), "zh")
        self.assertEqual(lang, "zh")  # 非法的 JSON lang 不生效，报错用环境的语言
        self.assertIn("是空串", problems[0].message)
        _, problems, lang = validate.check_json(json.dumps({**SAMPLE, "title": ""}), texts.resolve(None, None))
        self.assertEqual(lang, "en")

    def test_cli_invalid_lang_is_reported_with_the_rest(self):
        code, out, err = run(["--home", "/nonexistent/agent-ntfy-home", "ask"], json.dumps({**SAMPLE, "lang": "jp", "reasoning": ""}))
        self.assertEqual((code, out), (1, ""))
        self.assertIn("输入校验未通过（2 处）", err)
        self.assertIn("lang       :", err)
        code, out, err = run(["--home", "/nonexistent/agent-ntfy-home", "ask"], json.dumps({**SAMPLE, "lang": "en", "reasoning": ""}), env={"AGENT_NTFY_LANG": "zh"})
        self.assertEqual(code, 1)
        self.assertEqual(cjk(err), 0, err)  # JSON 里的 en 压过环境的 zh


class PriorityTest(unittest.TestCase):
    reply_when_sent = ta.AskExitCodesTest.reply_when_sent

    def test_json_lang_beats_env(self):
        h = Harness(self, lang="en")
        self.reply_when_sent(h, "ok")
        code, out, err = run(["--home", str(h.home), "ask", "--timeout", "5"], json.dumps({**SAMPLE, "lang": "zh"}), {**HERDR, "AGENT_NTFY_LANG": "en"})
        self.assertEqual(code, 0, err)
        self.assertIn("【正在做】", h.client.published[0]["message"])

    def test_env_applies_when_json_has_no_lang(self):
        h = Harness(self, lang="en")
        self.reply_when_sent(h, "ok")
        code, out, err = run(["--home", str(h.home), "ask", "--timeout", "5"], json.dumps(SAMPLE), {**HERDR, "AGENT_NTFY_LANG": "zh"})
        self.assertEqual(code, 0, err)
        self.assertIn("【正在做】", h.client.published[0]["message"])

    def test_default_is_english(self):
        h = Harness(self, lang="en")
        self.reply_when_sent(h, "ok")
        env = {k: v for k, v in HERDR.items()}
        env["AGENT_NTFY_LANG"] = ""  # run() 缺省会塞 zh：这里显式清空，模拟两者都没有
        code, out, err = run(["--home", str(h.home), "ask", "--timeout", "5"], json.dumps(SAMPLE), env)
        self.assertEqual(code, 0, err)
        self.assertIn("**[Doing]** ", h.client.published[0]["message"])
        self.assertNotIn("【", h.client.published[0]["message"])

    def test_invalid_env_lang_fails_loudly_everywhere(self):
        # 只认恰好 zh / en：zh-CN 不是静默回退，是响亮失败——每个子命令都一样，daemon 也不起
        h = Harness(self, lang="en")
        for argv, stdin in ((["slots"], ""), (["ask"], json.dumps(SAMPLE)), (["daemon", "--status"], ""), (["confirm-sub", "slot1", "--subscribed"], ""), (["release"], "")):
            code, out, err = run(["--home", str(h.home), *argv], stdin, env={"AGENT_NTFY_LANG": "zh-CN"})
            self.assertEqual((code, out), (1, ""), (argv, err))
            self.assertIn("zh-CN", err)
            self.assertIn("zh / en", err)
        self.assertEqual(h.client.published, [])  # ask 没发
        code, out, err = run(["--home", str(h.home), "slots"], env={"AGENT_NTFY_LANG": ""})  # 空串 = 没给
        self.assertEqual(code, 0, err)

    def test_daemon_language_is_a_constructor_argument_not_the_environment(self):
        import tempfile
        from unittest import mock
        home = Path(tempfile.mkdtemp(prefix="an-")) / "home"
        with mock.patch.dict(os.environ, {"AGENT_NTFY_LANG": "en"}):
            self.assertEqual(daemon.Daemon(home, lang="zh").lang, "zh")
        with self.assertRaises(daemon.DaemonError):
            daemon.Daemon(home, lang="nope")  # 拒绝启动，不静默回退


class SameSequenceTest(unittest.TestCase):
    """同 seq 的更新沿用首发语言：daemon 自己是 en，一张 zh 的卡片从头到尾都是 zh；回执反之。"""

    def test_answered_and_timeout_updates_keep_zh_on_an_en_daemon(self):
        h = Harness(self, lang="en")
        sock, first, events = h.ask(payload={**SAMPLE, "lang": "zh"})
        h.client.message(h.topic("slot1"), "留固定目录")
        next(events)
        sock.close()
        wait_until(lambda: len(h.client.clears) == 1)
        upd = h.client.updates[-1]
        self.assertTrue(upd["title"].startswith("✅ 已回复 · "), upd["title"])
        self.assertTrue(upd["message"].startswith("**【你的回复】** "))
        sock2, first2, events2 = h.ask(payload={**SAMPLE, "lang": "zh"}, timeout=0.5)
        self.assertEqual(next(events2), {"event": "timeout"})
        sock2.close()
        wait_until(lambda: len(h.client.clears) == 2)
        self.assertTrue(h.client.updates[-1]["title"].startswith("⌛ 已超时 · "))
        sock3, first3, events3 = h.ask(payload={**SAMPLE, "lang": "en"})
        sock3.close()
        wait_until(lambda: len(h.client.clears) == 3)
        self.assertTrue(h.client.updates[-1]["title"].startswith("⚠️ Cancelled · "), h.client.updates[-1]["title"])

    def test_receipt_and_its_button_result_follow_daemon_language(self):
        h = Harness(self, lang="en")
        h.state.acquire("wX:p9")
        h.client.message(h.topic("slot1"), "anyone?")
        wait_until(lambda: len(h.client.published) == 1)
        pub = h.client.published[0]
        self.assertEqual(pub["title"], "[slot1] Message not delivered")
        self.assertEqual([a["label"] for a in pub["actions"]], ["Release slot", "Ignore"])
        h.client.message(h.topic("slot1"), inject.control_mark("release", "slot1"))
        wait_until(lambda: len(h.client.clears) == 1)
        upd = h.client.updates[-1]
        self.assertEqual(upd["title"], "✅ Released · [slot1] Message not delivered")
        self.assertTrue(upd["message"].startswith("**Slot slot1 released"))
        self.assertEqual(cjk(upd["title"] + upd["message"]), 0)

    def test_receipt_close_uses_the_receipt_language_not_the_daemon_language(self):
        # 白盒：回执对象自带 lang，按钮点击后的更新按它取——即便 daemon 自己是别的语言
        h = Harness(self, lang="en")
        h.state.acquire("wX:p9")
        h.client.message(h.topic("slot1"), "anyone?")
        wait_until(lambda: len(h.client.published) == 1)
        old = h.daemon._receipts["slot1"]
        zh = Outcome(False, "pane_missing", "wX:p9", None, texts.t("receipt.pane_missing", "zh", target="wX:p9"), "zh")
        h.daemon._receipts["slot1"] = daemon.Receipt(slot="slot1", msg_id=old.msg_id, rendered=inject.render_receipt("slot1", zh, reply_url=URL))
        h.client.message(h.topic("slot1"), inject.control_mark("ignore", "slot1"))
        wait_until(lambda: len(h.client.clears) == 1)
        self.assertTrue(h.client.updates[-1]["title"].startswith("已忽略 · [slot1] 消息未送达"), h.client.updates[-1]["title"])

    def test_confirm_card_follows_the_request_language(self):
        h = Harness(self, subscribed=(), lang="en")
        sock = h.connect()
        agent_ntfy.send_request(sock, {"cmd": "confirm-sub", "slot": "slot4", "subscribed": True, "timeout": 30, "lang": "zh"})
        events = agent_ntfy.read_events(sock)
        self.assertEqual(next(events)["event"], "sent")
        self.assertEqual(h.client.published[-1]["title"], "[slot4] 确认你能收到通知")
        h.client.message(h.topic("slot4"), inject.control_mark("confirmed", "slot4"))
        self.assertEqual(next(events)["event"], "confirmed")
        sock.close()
        wait_until(lambda: len(h.client.clears) == 1)
        self.assertTrue(h.client.updates[-1]["title"].startswith("✅ 已确认 · "))


class StateErrorTest(unittest.TestCase):
    def test_state_errors_reaching_the_cli_are_in_the_table(self):
        import state
        e = state.StateError("slot.missing", slot="slot9", n=5)
        self.assertEqual(str(e), "槽位 slot9 不存在（池子里只有 5 个）")  # 日志 / zh 一个字不变
        self.assertEqual(e.text("en"), "slot slot9 does not exist (the pool only has 5)")
        self.assertEqual(cjk(state.NeedsUserDecision(["slot2"]).text("en")), 0)
        self.assertEqual(str(state.NeedsUserDecision([])), "全部槽位已租用，可替换的空闲槽位：无")
        h = Harness(self, lang="en")
        (h.home / "leases.json").write_text("{not json", encoding="utf-8")
        ev = h.request(cmd="release", slot="slot1", lang="en")[0]
        self.assertEqual((ev["kind"], cjk(ev["message"])), ("state", 0), ev)
        ev = h.request(cmd="add-slot", lang="en")[0]
        self.assertEqual((ev["kind"], cjk(ev["message"])), ("state", 0), ev)
        ev = h.request(cmd="release", slot="slot1", lang="zh")[0]
        self.assertTrue(ev["message"].startswith("读租约文件 "), ev)  # zh 原样


class RemainingLiteralsTest(unittest.TestCase):
    """人读文案里最后几处写死的中文：DaemonError / ArgumentTypeError / NtfyError。"""

    def test_daemon_errors_are_in_the_table(self):
        import tempfile
        home = Path(tempfile.mkdtemp(prefix="an-")) / "home"
        with self.assertRaises(daemon.DaemonError) as cm:
            daemon.Daemon(home, lang="nope")
        self.assertEqual(cjk(cm.exception.text("en")), 0)
        self.assertIn("zh / en", cm.exception.text("zh"))
        e = daemon.DaemonError("socket", path="/x/daemon.sock", error="too long")
        self.assertTrue(str(e).startswith("起不了 socket /x/daemon.sock：too long。"))  # zh 原样
        self.assertEqual(cjk(e.text("en")), 0)
        code, out, err = run(["--home", "/tmp/definitely-not-a-dir-xyz/" + "x" * 120, "daemon"], env={"AGENT_NTFY_LANG": "en"})  # socket 路径超长：起不了
        cli_lines = [l for l in err.splitlines() if l.startswith("agent-ntfy:")]  # 前台 daemon 的日志也打在 stderr，日志按约定保持中文，只看 CLI 那行
        self.assertEqual(code, 3, err)
        self.assertEqual(len(cli_lines), 1, err)
        self.assertEqual(cjk(cli_lines[0]), 0, cli_lines[0])

    def test_timeout_argument_errors_follow_the_language(self):
        import contextlib
        import io
        from unittest import mock
        for lang, needle in (("en", "not a number"), ("zh", "不是数字")):
            errbuf = io.StringIO()
            with mock.patch.dict(os.environ, {"AGENT_NTFY_LANG": lang}), contextlib.redirect_stderr(errbuf), self.assertRaises(SystemExit):
                agent_ntfy.main(["ask", "--timeout", "abc"])
            self.assertIn(needle, errbuf.getvalue())
        errbuf = io.StringIO()
        with mock.patch.dict(os.environ, {"AGENT_NTFY_LANG": "en"}), contextlib.redirect_stderr(errbuf), self.assertRaises(SystemExit):
            agent_ntfy.main(["ask", "--timeout", "0"])
        self.assertEqual(cjk(errbuf.getvalue()), 0, errbuf.getvalue())

    def test_ntfy_errors_reach_the_phone_and_cli_in_the_card_language(self):
        import ntfyclient
        e = ntfyclient.NtfyError("message.too_long", n=5000, limit=4096)
        self.assertEqual(str(e), "正文 5000 字节，超过 4096 字节 ntfy 会把它转成附件而不是当消息推送")  # 日志 / zh 原样
        self.assertEqual(cjk(e.text("en")), 0)
        with self.assertRaises(ntfyclient.NtfyError) as cm:
            ntfyclient._check_name("topic", "bad topic")
        self.assertEqual(str(cm.exception), "topic不合法：含空白字符（长度 9）。只能用字母、数字、- 和 _，1~64 位")
        self.assertEqual(cjk(cm.exception.text("en")), 0)
        rl = ntfyclient.NtfyClient()._status_error(429, "Too Many Requests", '{"code": 42901, "error": "limit reached"}')
        self.assertIsInstance(rl, ntfyclient.NtfyRateLimited)
        self.assertEqual(str(rl), "HTTP 429 Too Many Requests：limit reached（ntfy code 42901）。这是限流，不是代码错")
        self.assertEqual(cjk(rl.text("en")), 0)
        # 经 daemon 到 CLI：发布失败的 {error} 按卡片语言
        h = Harness(self, lang="en")
        h.client.fail_publish = ntfyclient.NtfyError("connect.failed", url="https://ntfy.example", error="boom")
        sock, first, events = h.ask(payload={**SAMPLE, "lang": "en"})
        self.assertEqual((first["kind"], cjk(first["message"])), ("publish_failed", 0), first)
        sock.close()


class HelpTest(unittest.TestCase):
    def test_argparse_help_follows_env_language(self):
        import contextlib
        import io
        from unittest import mock
        for lang, needle, absent in (("en", "block and ask", "阻塞提问"), ("zh", "阻塞提问", "block and ask")):
            out = io.StringIO()
            with mock.patch.dict(os.environ, {"AGENT_NTFY_LANG": lang}), contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as cm:
                agent_ntfy.main(["--help"])
            self.assertEqual(cm.exception.code, 0)
            self.assertIn(needle, out.getvalue())
            self.assertNotIn(absent, out.getvalue())
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"AGENT_NTFY_LANG": "en"}), contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            agent_ntfy.main(["confirm-sub", "--help"])
        self.assertEqual(cjk(out.getvalue()), 0, out.getvalue())


class BudgetTest(unittest.TestCase):
    def test_fixed_overhead_and_tag_budget(self):
        # 固定开销 = 分隔线 "\n\n---\n\n"（7 B）+ 末尾提示：zh 提示 164 B ⇒ 171；en 提示 183 B ⇒ 190
        self.assertEqual(render.fixed_overhead_bytes("zh"), 171)
        self.assertEqual(render.fixed_overhead_bytes("en"), 190)
        self.assertEqual(render.PREFIX_MAX_BYTES, 20)  # 提问 Title 前缀里最长的是「⚠️ 已取消 · 」/「⚠️ Cancelled · 」
        self.assertEqual(render.TAG_MAX_BYTES, 41)
        longest_tag = "x" * render.TAG_MAX_BYTES
        for lang in texts.LANGS:
            for key in render.QUESTION_PREFIX_KEYS:  # 两种语言的每一个提问前缀都不能把极限 Title 顶过 1 KB
                title = texts.t(key, lang) + f"[{longest_tag}] " + "字" * 320
                self.assertLessEqual(len(title.encode("utf-8")), render.NTFY_TITLE_LIMIT, (lang, key))
            for key in (k for k in texts.TEXTS[lang] if k.startswith("prefix.") and k not in render.QUESTION_PREFIX_KEYS):
                # 其余前缀只拼在回执 / 确认卡的固定短 Title 前：最长的固定 Title + 极限槽位名也远在 1 KB 内
                title = texts.t(key, lang) + f"[slot{10**9}] " + max((texts.t(k, lang) for k in ("receipt.title", "receipt.title_uncertain", "confirm.title")), key=len)
                self.assertLess(len(title.encode("utf-8")), 200, (lang, key))


if __name__ == "__main__":
    unittest.main()
