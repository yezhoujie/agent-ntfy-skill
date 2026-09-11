"""消息渲染：JSON → 固定版式的通知文本 + 唯一的按钮；已回复态的更新文本。纯函数，不碰网络。"""

import email.header
import io
import json
import re
import unittest

import ntfyclient
import render
from ntfyclient import NtfyClient
from render import Rendered

# 一份合格提问的范例（同一件事的完整写法）
SAMPLE = {
    "title": "助手没检出代码时，临时目录留还是删",
    "doing": "让需求助手在用户还没把项目代码检出到本地时也能用",
    "description": "以前必须先有本地代码目录才让用。现在放开了这道限制，于是要决定助手临时起的那个子进程在哪个目录下跑。",
    "blocker": "既然没有代码目录，那个子进程没有天然的工作目录可用",
    "options": [
        {"id": "keep", "label": "留固定目录",
         "consequence": "在应用数据目录下给每个项目建一个固定空目录。每个项目留一份，出问题时能翻出现场看；代价是目录越堆越多没人清理"},
        {"id": "temp", "label": "用完即删",
         "consequence": "每次用完就删的临时目录。干净，但一旦跑挂了什么都不剩，排查只能靠日志"},
    ],
    "recommend": "keep",
    "reasoning": "留固定目录。这条路上的用户恰恰是配置最容易出错的那批人（连代码都还没检出），留现场值钱。最强的反对是磁盘垃圾会累积，但可以加个 30 天清理",
    "question": "留固定目录，还是用完即删？",
}
SECTIONS = ["【正在做】", "【背景】", "【卡点】", "【选项】", "【我的建议】", "【要你定】"]
HINT_LINES = ["⚠️ 按钮是快捷选项。有别的意见请在下方输入框直接回复。", "   回复发出即生效，不能撤回、也无法追加——请一次说完。"]
REPLY_URL = "https://ntfy.sh/some-topic"


class EchoClient(NtfyClient):
    """把请求原样回显成服务端消息对象——用来证明渲染结果能被 publish() / update() 直接消费，不碰网络。

    JSON 发布体按字段回显；纯文本更新则从 URL 取 sequence_id、从 Title 头解 RFC 2047 后回显。
    """

    def _open(self, req, timeout):
        data = req.data
        assert isinstance(data, bytes)
        if req.get_header("Content-type", "").startswith("application/json"):
            body = json.loads(data.decode("utf-8"))
            echo = {"id": "echo1", "time": 1, "event": "message", **body}
            if "actions" in body:
                echo["actions"] = [{"id": f"a{i}", **a} for i, a in enumerate(body["actions"])]
        else:
            topic, seq = req.full_url.rsplit("/", 2)[-2:]
            echo = {"id": "echo2", "time": 1, "event": "message", "topic": topic, "sequence_id": seq,
                    "message": data.decode("utf-8")}
            raw_title = req.get_header("Title")
            if raw_title:
                text, charset = email.header.decode_header(raw_title)[0]
                echo["title"] = text.decode(charset or "ascii") if isinstance(text, bytes) else text
        return io.BytesIO(json.dumps(echo).encode("utf-8"))


def with_options(n):
    """把范例改成 n 个选项（1~5），推荐仍是第 1 个。"""
    p = dict(SAMPLE)
    p["options"] = [{"id": f"o{i}", "label": f"选项{i}", "consequence": f"后果{i}"} for i in range(n)]
    p["recommend"] = "o0"
    return p


class RenderQuestionTest(unittest.TestCase):
    def setUp(self):
        self.r = render.render_question(SAMPLE, tag="wD", reply_url=REPLY_URL)

    # 六段齐全且顺序固定：正在做 / 背景 / 卡点 / 选项 / 我的建议 / 要你定
    def test_six_sections_in_fixed_order(self):
        positions = [self.r.message.find(s) for s in SECTIONS]
        self.assertNotIn(-1, positions)
        self.assertEqual(positions, sorted(positions))
        for s in SECTIONS:
            self.assertEqual(self.r.message.count(s), 1, s)
        # 每段带的是对应字段的内容
        self.assertIn("【正在做】" + SAMPLE["doing"], self.r.message)
        self.assertIn("【背景】" + SAMPLE["description"], self.r.message)
        self.assertIn("【卡点】" + SAMPLE["blocker"], self.r.message)
        self.assertIn("【我的建议】" + SAMPLE["reasoning"], self.r.message)
        self.assertIn("【要你定】" + SAMPLE["question"], self.r.message)

    # 推荐项在选项列表中标注「（推荐）」，全部选项编号列出
    def test_recommended_option_is_marked(self):
        self.assertIn("  1. 留固定目录（推荐）→ " + SAMPLE["options"][0]["consequence"], self.r.message)
        self.assertIn("  2. 用完即删 → " + SAMPLE["options"][1]["consequence"], self.r.message)
        self.assertEqual(self.r.message.count("（推荐）"), 1)
        five = render.render_question(with_options(5), tag="wD", reply_url=REPLY_URL)
        for i in range(5):
            self.assertIn(f"  {i + 1}. 选项{i}", five.message)
        # 推荐项在最后一个
        last = render.render_question({**with_options(5), "recommend": "o4"}, tag="wD", reply_url=REPLY_URL)
        self.assertIn("  5. 选项4（推荐）→ 后果4", last.message)
        self.assertEqual(last.message.count("（推荐）"), 1)
        self.assertEqual(last.actions[0]["body"], "选项4")
        # recommend 缺失 + 某项缺 id：谁都不该被标成推荐
        none = render.render_question({**with_options(2), "recommend": None, "options": [{"label": "无 id", "consequence": "x"}, with_options(2)["options"][1]]},
                                      tag="wD", reply_url=REPLY_URL)
        self.assertNotIn("（推荐）", none.message)

    # 固定提示出现在正文末尾，不在开头
    def test_hint_is_at_the_end_not_the_beginning(self):
        hint = "\n".join(HINT_LINES)
        self.assertTrue(self.r.message.endswith(hint))
        self.assertFalse(self.r.message.startswith("⚠️"))
        self.assertGreater(self.r.message.find(hint), self.r.message.find("【要你定】"))
        # 提示与正文之间是那条分隔线
        self.assertIn("【要你定】" + SAMPLE["question"] + "\n──────────\n" + hint, self.r.message)

    # 提示文案完整，逐字
    def test_hint_text_is_verbatim(self):
        self.assertIn("也无法追加——请一次说完", self.r.message)
        for line in HINT_LINES:
            self.assertIn(line, self.r.message)

    # 按钮 label 固定为「采纳推荐」
    def test_button_label_is_fixed(self):
        self.assertEqual(self.r.actions[0]["label"], "采纳推荐")

    # 按钮 body 等于推荐项的 label，不是「采纳推荐」
    def test_button_body_is_recommended_label(self):
        self.assertEqual(self.r.actions[0]["body"], "留固定目录")
        self.assertNotEqual(self.r.actions[0]["body"], self.r.actions[0]["label"])
        other = render.render_question({**SAMPLE, "recommend": "temp"}, tag="wD", reply_url=REPLY_URL)
        self.assertEqual(other.actions[0]["body"], "用完即删")

    # 按钮有且只有 1 个，回传到调用方给的地址
    def test_exactly_one_button(self):
        self.assertEqual(len(self.r.actions), 1)
        self.assertEqual(self.r.actions[0]["action"], "http")
        self.assertEqual(self.r.actions[0]["url"], REPLY_URL)
        self.assertEqual(len(render.render_question(with_options(5), tag="wD", reply_url=REPLY_URL).actions), 1)

    # 输出为纯文本，不含 Markdown 标记
    def test_plain_text_without_markdown(self):
        for mark in ("**", "__", "`", "](", "\n# ", "\n- ", "\n* "):
            self.assertNotIn(mark, self.r.message, mark)
        self.assertIsNone(re.search(r"^\s*[#>]", self.r.message, flags=re.M))

    # 通知 Title = [<tag>] <title>，tag 由调用方给
    def test_title_carries_tag(self):
        self.assertEqual(self.r.title, "[wD] " + SAMPLE["title"])
        self.assertEqual(render.render_question(SAMPLE, tag="slot3", reply_url=REPLY_URL).title, "[slot3] " + SAMPLE["title"])

    # tag 的预算 = 1024 − 已回复前缀 − title 上限 − 「[] 」三字节 = 44；超出就是调用方的错，当场抛
    def test_tag_over_budget_raises(self):
        self.assertEqual(render.TAG_MAX_BYTES, 44)
        self.assertEqual(render.render_title(SAMPLE, "t" * 44), "[" + "t" * 44 + "] " + SAMPLE["title"])
        with self.assertRaises(ValueError):
            render.render_title(SAMPLE, "t" * 45)
        with self.assertRaises(ValueError):
            render.render_question(SAMPLE, tag="标" * 15, reply_url=REPLY_URL)  # 45 字节

    # 渲染结果能被 NtfyClient.publish() 直接消费
    def test_consumable_by_publish(self):
        resp = EchoClient().publish("some-topic", self.r.message, title=self.r.title, actions=self.r.actions)
        self.assertEqual(resp["title"], self.r.title)
        self.assertEqual(resp["actions"][0]["body"], "留固定目录")

    # 字节预算：提问上限 3584 = 4096 − 512，两个常量都有名字
    def test_byte_budget_constants(self):
        self.assertEqual(render.MAX_MESSAGE_BYTES, 4096)
        self.assertEqual(render.MAX_MESSAGE_BYTES, ntfyclient.MAX_MESSAGE_BYTES)
        self.assertEqual(render.REPLY_RESERVE_BYTES, 512)
        self.assertEqual(render.QUESTION_MAX_BYTES, 3584)
        self.assertEqual(render.TITLE_MAX_BYTES, 960)  # ntfy title 硬顶 1 KB，扣掉已回复前缀与 [tag] 的余量
        self.assertEqual(render.message_bytes(SAMPLE), len(self.r.message.encode("utf-8")))


class RenderAnsweredTest(unittest.TestCase):
    def setUp(self):
        self.q = render.render_question(SAMPLE, tag="wD", reply_url=REPLY_URL)

    # 已回复态：Title 加前缀、正文 = 回复 + 原提问、actions 为空、不含末尾提示
    def test_answered_rendering(self):
        a = render.render_answered(self.q, "留固定目录")
        self.assertIsInstance(a, Rendered)
        self.assertEqual(a.title, "✅ 已回复 · [wD] " + SAMPLE["title"])
        self.assertTrue(a.message.startswith("【你的回复】留固定目录\n──────────\n（以下为当时的提问）\n【正在做】"))
        self.assertIn(self.q.body, a.message)  # 原六段正文完整保留
        self.assertNotIn("⚠️ 按钮是快捷选项", a.message)
        self.assertEqual(a.actions, [])
        self.assertLessEqual(len(a.message.encode("utf-8")), render.MAX_MESSAGE_BYTES)
        # 回复首尾的空白与换行不进手机记录（agent 拿到的仍是原文，那是 daemon 的事）
        self.assertTrue(render.render_answered(self.q, "  留固定目录 \n").message.startswith("【你的回复】留固定目录\n──────────\n"))
        # 能被 update() 直接消费：中文 Title 经 HTTP 头往返后原样
        resp = EchoClient().update("some-topic", "seq1", a.message, title=a.title)
        self.assertEqual(resp["title"], a.title)

    # 原提问正文本身已超预算（没过校验就来）：不能静默产出一条「已截断」却一字回复都没有的消息
    def test_answered_refuses_oversized_question(self):
        huge = Rendered(title="[wD] x", message="", actions=[], body="字" * 1400)  # 4200 字节
        with self.assertRaises(ValueError):
            render.render_answered(huge, "ok")

    # 按字节截断不截坏多字节字符
    def test_cut_utf8(self):
        self.assertEqual(render._cut_utf8("中文abc", 4), "中")
        self.assertEqual(render._cut_utf8("中文abc", 6), "中文")
        self.assertEqual(render._cut_utf8("中文abc", 0), "")
        self.assertEqual(render._cut_utf8("中文abc", -3), "")

    # 回复超预算：只截回复并加标记，原提问完整，总长不超 4096
    def test_long_reply_is_truncated_but_question_kept(self):
        # 把提问撑到接近上限（校验放行的最大形态），再给一条很长的回复
        big = dict(SAMPLE)
        while render.message_bytes(big) <= render.QUESTION_MAX_BYTES - 300:
            big["description"] += "补充背景。"
        q = render.render_question(big, tag="wD", reply_url=REPLY_URL)
        self.assertLessEqual(len(q.message.encode("utf-8")), render.QUESTION_MAX_BYTES)
        reply = "这是一条很长的回复，" * 200
        a = render.render_answered(q, reply)
        self.assertLessEqual(len(a.message.encode("utf-8")), render.MAX_MESSAGE_BYTES)
        self.assertIn(q.body, a.message)
        head, _, _ = a.message.partition("\n──────────\n")
        self.assertTrue(head.startswith("【你的回复】"))
        self.assertTrue(head.endswith(render.TRUNCATED_MARK))
        kept = head[len("【你的回复】"):-len(render.TRUNCATED_MARK)]
        self.assertTrue(reply.startswith(kept))  # 截的是尾巴，没有截坏多字节字符
        self.assertGreater(len(kept), 100)
        # 不超预算就一个字不动
        short = render.render_answered(q, "留固定目录")
        self.assertIn("【你的回复】留固定目录\n", short.message)
        self.assertNotIn(render.TRUNCATED_MARK, short.message)


if __name__ == "__main__":
    unittest.main()
