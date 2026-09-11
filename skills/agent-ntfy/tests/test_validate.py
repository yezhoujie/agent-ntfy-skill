"""输入校验：不合规就一次报全，文案可照着改，且一定带「消息未发送」。纯函数，不碰网络。"""

import json
import unittest

import render
import validate
from tests.test_render import SAMPLE

REQUIRED = ["title", "doing", "description", "blocker", "options", "recommend", "reasoning", "question"]


def fields(problems):
    return [p.field for p in problems]


class CheckTest(unittest.TestCase):
    def test_sample_passes(self):
        self.assertEqual(validate.check(SAMPLE), [])

    # 8 个必填字段各自缺失时都被捕获；存在但为空串同样算缺失
    def test_each_required_field_missing_or_empty_is_caught(self):
        for name in REQUIRED:
            with self.subTest(field=name, how="缺失"):
                payload = {k: v for k, v in SAMPLE.items() if k != name}
                self.assertIn(name, fields(validate.check(payload)))
            with self.subTest(field=name, how="空"):
                payload = {**SAMPLE, name: [] if name == "options" else ""}
                self.assertIn(name, fields(validate.check(payload)))
        # 只有空白也算空
        self.assertIn("title", fields(validate.check({**SAMPLE, "title": "  \n"})))

    # options < 2 → 报错；> 5 → 报错
    def test_options_count_out_of_range(self):
        one = {**SAMPLE, "options": SAMPLE["options"][:1]}
        problems = validate.check(one)
        self.assertIn("options", fields(problems))
        self.assertIn("2~5", next(p.message for p in problems if p.field == "options"))
        six = {**SAMPLE, "options": [{"id": f"o{i}", "label": f"选项{i}", "consequence": f"后果{i}"} for i in range(6)], "recommend": "o0"}
        self.assertIn("options", fields(validate.check(six)))
        five = {**six, "options": six["options"][:5]}
        self.assertEqual(validate.check(five), [])

    # options[].id 重复 → 报错
    def test_duplicate_option_id(self):
        dup = {**SAMPLE, "options": [SAMPLE["options"][0], {**SAMPLE["options"][1], "id": "keep"}]}
        problems = validate.check(dup)
        self.assertTrue(any("重复" in p.message for p in problems), problems)

    # options[] 缺 label 或 consequence → 报错
    def test_option_missing_label_or_consequence(self):
        for key in ("label", "consequence", "id"):
            with self.subTest(missing=key):
                bad = {k: v for k, v in SAMPLE["options"][1].items() if k != key}
                problems = validate.check({**SAMPLE, "options": [SAMPLE["options"][0], bad]})
                # field 仍是 options，文案点名「第 N 项」（1-based，与 id 重复那条同款），不用下标形态
                self.assertTrue(any(p.field == "options" and "第 2 项" in p.message and key in p.message for p in problems), problems)
                self.assertFalse(any("[" in p.field for p in problems), problems)
        # 存在但为空串同样算缺
        empty = {**SAMPLE["options"][1], "consequence": ""}
        problems = validate.check({**SAMPLE, "options": [SAMPLE["options"][0], empty]})
        self.assertTrue(any(p.field == "options" and "第 2 项" in p.message and "consequence" in p.message for p in problems), problems)
        # 某项不是对象
        problems = validate.check({**SAMPLE, "options": [SAMPLE["options"][0], "不是对象"]})
        self.assertTrue(any(p.field == "options" and "第 2 项" in p.message for p in problems), problems)

    # recommend 不在 id 集合里 → 报错，且错误信息里列出现有 id
    def test_recommend_not_in_ids_lists_existing_ids(self):
        problems = validate.check({**SAMPLE, "recommend": "nope"})
        msg = next(p.message for p in problems if p.field == "recommend")
        self.assertIn("nope", msg)
        self.assertIn("keep, temp", msg)

    # JSON 语法错 → 报错，不抛 Python 原生异常给调用方
    def test_json_syntax_error_is_reported_not_raised(self):
        payload, problems = validate.check_json('{"title": "x",')
        self.assertIsNone(payload)
        self.assertEqual(len(problems), 1)
        self.assertIn("JSON", problems[0].message)
        # 能解析但不是对象也算错
        payload, problems = validate.check_json("[1, 2]")
        self.assertIsNone(payload)
        self.assertEqual(len(problems), 1)
        # 正常输入原样解析出来，没有错误
        payload, problems = validate.check_json(json.dumps(SAMPLE))
        self.assertEqual(payload, SAMPLE)
        self.assertEqual(problems, [])

    # 一次报全部错误：同时含 3 个错误的输入，3 条都在
    def test_reports_all_errors_at_once(self):
        payload = {k: v for k, v in SAMPLE.items() if k != "reasoning"}
        payload["options"] = [SAMPLE["options"][1]]  # 只剩 temp 一项
        payload["recommend"] = "keep"  # 不在 id 里
        problems = validate.check(payload)
        self.assertEqual(sorted(fields(problems)), ["options", "reasoning", "recommend"])

    # 报错文案含「消息未发送」，形态与规范一致、可照着改
    def test_format_mentions_message_not_sent(self):
        payload = {k: v for k, v in SAMPLE.items() if k != "reasoning"}
        payload["options"] = [SAMPLE["options"][1]]
        payload["recommend"] = "keep"
        text = validate.format_problems(validate.check(payload))
        self.assertIn("消息未发送", text)
        self.assertTrue(text.startswith("agent-ntfy ask: 输入校验未通过（3 处），全部修正后重试，消息未发送。\n"))
        self.assertIn("\n  options    : 只有 1 项，要求 2~5 项（只有一个选项不叫选择）\n", text)
        self.assertIn('\n  recommend  : "keep" 不在 options 的 id 里（现有 id: temp）\n', text)
        self.assertIn("\n  reasoning  : 缺失。必填，要写倾向的理由 + 最强的反对意见\n", text)
        # 顺序按契约里的字段顺序（options → recommend → reasoning），与规范样例一致
        self.assertEqual(text.splitlines()[2:], [
            "  options    : 只有 1 项，要求 2~5 项（只有一个选项不叫选择）",
            '  recommend  : "keep" 不在 options 的 id 里（现有 id: temp）',
            "  reasoning  : 缺失。必填，要写倾向的理由 + 最强的反对意见",
        ])


class LengthRuleTest(unittest.TestCase):
    """长度规则：渲染后正文 ≤ 3584 字节、title ≤ 960 字节，都从渲染层取数。"""

    def test_message_over_budget(self):
        big = dict(SAMPLE)
        # 用规范里的数字（4096 − 512 = 3584）而不是常量：常量被改宽时这条要能发现
        while render.message_bytes(big) <= 3584:
            big["description"] += "补充背景。"
        self.assertLess(render.message_bytes(big), 4096)
        problems = validate.check(big)
        self.assertEqual(fields(problems), ["正文"])
        actual = render.message_bytes(big)
        self.assertIn(f"{actual} 字节", problems[0].message)
        self.assertIn(f"超出 {actual - render.QUESTION_MAX_BYTES} 字节", problems[0].message)
        self.assertIn("description", problems[0].message)
        # 刚好卡线放行
        while render.message_bytes(big) > render.QUESTION_MAX_BYTES:
            big["description"] = big["description"][:-1]
        self.assertEqual(validate.check(big), [])

    def test_title_over_budget(self):
        long_title = "标" * 321  # 963 字节
        problems = validate.check({**SAMPLE, "title": long_title})
        self.assertEqual(fields(problems), ["title"])
        self.assertIn("963 字节", problems[0].message)
        self.assertIn(f"超出 {963 - render.TITLE_MAX_BYTES} 字节", problems[0].message)
        self.assertEqual(validate.check({**SAMPLE, "title": "标" * 320}), [])

    # title 含换行：通知栏那一行只能一行；放过它会在「已回复」更新（走 HTTP 头）那一步才炸，卡片带按钮悬着
    def test_title_with_newline_rejected(self):
        for bad in ("第一行\n第二行", "带回车\r", "尾换行\n"):
            with self.subTest(title=bad):
                problems = validate.check({**SAMPLE, "title": bad})
                self.assertEqual(fields(problems), ["title"])
                self.assertIn("换行", problems[0].message)

    # 长度错误与其它错误一次报全：正文超限的同时 recommend 不对
    def test_length_reported_together_with_other_errors(self):
        big = {**SAMPLE, "recommend": "nope"}
        while render.message_bytes(big) <= render.QUESTION_MAX_BYTES:
            big["reasoning"] += "再补一句理由。"
        self.assertEqual(sorted(fields(validate.check(big))), ["recommend", "正文"])


if __name__ == "__main__":
    unittest.main()
