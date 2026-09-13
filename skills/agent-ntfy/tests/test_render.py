"""消息渲染：JSON → 固定版式的通知文本 + 唯一的按钮；已回复态的更新文本。纯函数，不碰网络。"""

import email.header
import io
import json
import re
import unittest

import ntfyclient
import render
import texts
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
SECTIONS = ["**【正在做】**", "**【背景】**", "**【卡点】**", "**【选项】**", "**【我的建议】**", "**【要你定】**"]
HINT_LINES = ["⚠️ 按钮是快捷选项。", "有别的意见请在下方输入框直接回复。", "回复发出即生效，不能撤回、也无法追加——请一次说完。"]
HINT = "\n\n".join(HINT_LINES)  # 三句各占一段：Markdown 下单个换行会被折成空格
SEP = "\n\n---\n\n"  # 分隔线前后各一个空行：紧贴上一行的 --- 会把它变成 setext 标题
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
        self.r = render.render_question(SAMPLE, tag="wD", reply_url=REPLY_URL, lang="zh")

    # 六段齐全且顺序固定：正在做 / 背景 / 卡点 / 选项 / 我的建议 / 要你定
    def test_six_sections_in_fixed_order(self):
        positions = [self.r.message.find(s) for s in SECTIONS]
        self.assertNotIn(-1, positions)
        self.assertEqual(positions, sorted(positions))
        for s in SECTIONS:
            self.assertEqual(self.r.message.count(s), 1, s)
        # 每段带的是对应字段的内容：加粗的标记词 + 一个空格 + 内容；段与段之间空一行
        self.assertIn("**【正在做】** " + SAMPLE["doing"] + "\n\n**【背景】** " + SAMPLE["description"], self.r.message)
        self.assertIn("**【卡点】** " + SAMPLE["blocker"] + "\n\n**【选项】**\n\n1. ", self.r.message)
        self.assertIn("**【我的建议】** " + SAMPLE["reasoning"] + "\n\n**【要你定】** " + SAMPLE["question"], self.r.message)

    # 推荐项在选项列表中标注「（推荐）」，全部选项按 Markdown 有序列表列出（各项之间单个换行，label 加粗）
    def test_recommended_option_is_marked(self):
        self.assertIn("1. **留固定目录**（推荐）→ " + SAMPLE["options"][0]["consequence"] + "\n2. **用完即删** → " + SAMPLE["options"][1]["consequence"], self.r.message)
        self.assertEqual(self.r.message.count("（推荐）"), 1)
        five = render.render_question(with_options(5), tag="wD", reply_url=REPLY_URL, lang="zh")
        for i in range(5):
            self.assertIn(f"\n{i + 1}. **选项{i}**", five.message)
        # 推荐项在最后一个
        last = render.render_question({**with_options(5), "recommend": "o4"}, tag="wD", reply_url=REPLY_URL, lang="zh")
        self.assertIn("\n5. **选项4**（推荐）→ 后果4", last.message)
        self.assertEqual(last.message.count("（推荐）"), 1)
        self.assertEqual(last.actions[0]["body"], "选项4")
        # recommend 缺失 + 某项缺 id：谁都不该被标成推荐
        none = render.render_question({**with_options(2), "recommend": None, "options": [{"label": "无 id", "consequence": "x"}, with_options(2)["options"][1]]},
                                      tag="wD", reply_url=REPLY_URL, lang="zh")
        self.assertNotIn("（推荐）", none.message)

    # label 首尾带空白（校验只要求 strip 后非空）：加粗标记必须紧贴文字，否则 ** 在 Markdown 里裸露；按钮 body 同步去空白
    def test_option_label_whitespace_is_stripped_inside_bold(self):
        padded = {**SAMPLE, "options": [{**SAMPLE["options"][0], "label": " 留固定目录 "}, {**SAMPLE["options"][1], "label": "用完即删\t"}]}
        r = render.render_question(padded, tag="wD", reply_url=REPLY_URL, lang="zh")
        self.assertIn("1. **留固定目录**（推荐）→ ", r.message)
        self.assertIn("2. **用完即删** → ", r.message)
        self.assertEqual(r.actions[0]["body"], "留固定目录")

    # 固定提示出现在正文末尾，不在开头
    def test_hint_is_at_the_end_not_the_beginning(self):
        self.assertTrue(self.r.message.endswith(HINT))
        self.assertFalse(self.r.message.startswith("⚠️"))
        self.assertGreater(self.r.message.find(HINT), self.r.message.find("【要你定】"))
        # 提示与正文之间是那条分隔线，前后各空一行
        self.assertIn("**【要你定】** " + SAMPLE["question"] + SEP + HINT, self.r.message)
        self.assertEqual(self.r.message.count(SEP), 1)

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
        other = render.render_question({**SAMPLE, "recommend": "temp"}, tag="wD", reply_url=REPLY_URL, lang="zh")
        self.assertEqual(other.actions[0]["body"], "用完即删")

    # 按钮有且只有 1 个，回传到调用方给的地址
    def test_exactly_one_button(self):
        self.assertEqual(len(self.r.actions), 1)
        self.assertEqual(self.r.actions[0]["action"], "http")
        self.assertEqual(self.r.actions[0]["url"], REPLY_URL)
        self.assertEqual(len(render.render_question(with_options(5), tag="wD", reply_url=REPLY_URL, lang="zh").actions), 1)

    # Markdown 只用加粗 / 有序列表 / 分隔线：不用 # 标题（手机上太大）、表格、图片、链接（不渲染的客户端看到源码难读）
    def test_markdown_is_limited_to_bold_list_and_rule(self):
        self.assertEqual(render.SEPARATOR, "---")
        self.assertNotIn("──────────", self.r.message)
        for mark in ("__", "`", "](", "![", "|", "\n- ", "\n* "):
            self.assertNotIn(mark, self.r.message, mark)
        self.assertIsNone(re.search(r"^\s*[#>]", self.r.message, flags=re.M))
        self.assertEqual(self.r.message.count("**") % 2, 0)
        # --- 独占一行且前后都是空行：紧贴上一行会被当成 setext 二级标题，把上一行渲染成大标题。三种卡都查
        answered = render.render_answered(self.r, "留固定目录")
        notify = render.render_notify(NOTIFY, tag="wD", lang="zh")
        for message in (self.r.message, answered.message, notify.message):
            self.assertEqual(len(re.findall(r"^---$", message, flags=re.M)), 1)
            for m in re.finditer(r"^---$", message, flags=re.M):
                self.assertEqual(message[m.start() - 2:m.start()], "\n\n")
                self.assertEqual(message[m.end():m.end() + 2], "\n\n")
            self.assertIsNone(re.search(r"\S\n---", message))
            # 每行的加粗自身闭合：不会出现跨行的 **
            for line in message.splitlines():
                self.assertEqual(line.count("**") % 2, 0, line)

    # 通知 Title = [<tag>] <title>，tag 由调用方给
    def test_title_carries_tag(self):
        self.assertEqual(self.r.title, "[wD] " + SAMPLE["title"])
        self.assertEqual(render.render_question(SAMPLE, tag="slot3", reply_url=REPLY_URL, lang="zh").title, "[slot3] " + SAMPLE["title"])

    # tag 的预算 = 1024 − 会拼到提问 Title 前的最长前缀（只取已回复 / 超时 / 取消三个，两种语言里最长是「⚠️ 已取消 · 」20；
    # 「已被新回执取代 · 」25 字节刻意不算：它只拼在回执的固定短 Title 前）− title 上限 − 「[] 」三字节 = 41；超出就是调用方的错，当场抛
    def test_tag_over_budget_raises(self):
        self.assertEqual(render.TAG_MAX_BYTES, 41)
        self.assertEqual(render.render_title(SAMPLE, "t" * 41), "[" + "t" * 41 + "] " + SAMPLE["title"])
        with self.assertRaises(ValueError):
            render.render_title(SAMPLE, "t" * 42)
        with self.assertRaises(ValueError):
            render.render_question(SAMPLE, tag="标" * 14, reply_url=REPLY_URL, lang="zh")  # 42 字节

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
        self.assertEqual(render.message_bytes(SAMPLE, "zh"), len(self.r.message.encode("utf-8")))


class RenderAnsweredTest(unittest.TestCase):
    def setUp(self):
        self.q = render.render_question(SAMPLE, tag="wD", reply_url=REPLY_URL, lang="zh")

    # 已回复态：Title 加前缀、正文 = 加粗的回复标记 + 回复 + 分隔线 + 原提问、actions 为空、不含末尾提示
    def test_answered_rendering(self):
        a = render.render_answered(self.q, "留固定目录")
        self.assertIsInstance(a, Rendered)
        self.assertEqual(a.title, "✅ 已回复 · [wD] " + SAMPLE["title"])
        self.assertTrue(a.message.startswith("**【你的回复】** 留固定目录\n\n---\n\n（以下为当时的提问）\n\n**【正在做】** "))
        self.assertIn(self.q.body, a.message)  # 原六段正文完整保留
        self.assertNotIn("⚠️ 按钮是快捷选项", a.message)
        self.assertEqual(a.actions, [])
        self.assertLessEqual(len(a.message.encode("utf-8")), render.MAX_MESSAGE_BYTES)
        # 回复首尾的空白与换行不进手机记录（agent 拿到的仍是原文，那是 daemon 的事）
        self.assertTrue(render.render_answered(self.q, "  留固定目录 \n").message.startswith("**【你的回复】** 留固定目录\n\n---\n\n"))
        # 能被 update() 直接消费：中文 Title 经 HTTP 头往返后原样
        resp = EchoClient().update("some-topic", "seq1", a.message, title=a.title)
        self.assertEqual(resp["title"], a.title)

    # 回复里的换行要看得见：Markdown 把单个换行折成空格，所以 strip 后把每处换行（含 \r\n、连续空行）统一成一个空行
    def test_reply_line_breaks_become_blank_lines(self):
        a = render.render_answered(self.q, "  第一行\r\n第二行\n\n\n第三行\n")
        self.assertTrue(a.message.startswith("**【你的回复】** 第一行\n\n第二行\n\n第三行\n\n---\n\n"))
        self.assertNotIn("\r", a.message)
        # 归一在预算 / 截断之前做：撑到上限的回复，截完总长仍 ≤ 4096
        big = dict(SAMPLE)
        while render.message_bytes(big, "zh") <= render.QUESTION_MAX_BYTES - 300:
            big["description"] += "补充背景。"
        q = render.render_question(big, tag="wD", reply_url=REPLY_URL, lang="zh")
        long = render.render_answered(q, "一行回复\n" * 300)
        self.assertLessEqual(len(long.message.encode("utf-8")), render.MAX_MESSAGE_BYTES)
        self.assertTrue(long.message.partition(SEP)[0].endswith(texts.t("reply.truncated", "zh")))

    # 原提问正文本身已超预算（没过校验就来）：不能静默产出一条「已截断」却一字回复都没有的消息
    def test_answered_refuses_oversized_question(self):
        huge = Rendered(title="[wD] x", message="", actions=[], body="字" * 1400, lang="zh")  # 4200 字节
        with self.assertRaises(ValueError):
            render.render_answered(huge, "ok")

    # 按字节截断不截坏多字节字符
    def test_cut_utf8(self):
        self.assertEqual(render._cut_utf8("中文abc", 4), "中")
        self.assertEqual(render._cut_utf8("中文abc", 6), "中文")
        self.assertEqual(render._cut_utf8("中文abc", 0), "")
        self.assertEqual(render._cut_utf8("中文abc", -3), "")

    # 截断不切在 ** 中间，也不留下奇数个 **：剩一个没闭合的加粗标记会把截断标记连同后面的原提问全部渲染成粗体
    def test_cut_utf8_keeps_bold_marks_balanced(self):
        self.assertEqual(render._cut_utf8("a**b", 2), "a")  # 正好切在两个星号之间
        self.assertEqual(render._cut_utf8("a**b", 3), "a")  # 切完只剩一个 **
        self.assertEqual(render._cut_utf8("**加粗**后", 5), "加")  # "**加" 剩一个没闭合的 **：只摘掉标记，文字保留
        self.assertEqual(render._cut_utf8("**加粗**后", 8), "加粗")
        self.assertEqual(render._cut_utf8("**加粗**后", 10), "**加粗**")  # 成对就保留
        self.assertEqual(render._cut_utf8("**加粗**后", 13), "**加粗**后")
        self.assertEqual(render._cut_utf8("**a** **b", 9), "**a** b")  # 摘的是最后那个没闭合的，前面成对的不动
        self.assertEqual(render._cut_utf8("a*b", 2), "a*")  # 单个星号不是加粗标记，不动
        # 回复以 ** 开头、闭合标记在预算之外：截断后正文仍在，不能只剩一个截断标记
        self.assertEqual(render._cut_utf8("**" + "x" * 50 + "** end", 20), "x" * 18)

    # 回复超预算：只截回复并加标记，原提问完整，总长不超 4096
    def test_long_reply_is_truncated_but_question_kept(self):
        # 把提问撑到接近上限（校验放行的最大形态），再给一条很长的回复
        big = dict(SAMPLE)
        while render.message_bytes(big, "zh") <= render.QUESTION_MAX_BYTES - 300:
            big["description"] += "补充背景。"
        q = render.render_question(big, tag="wD", reply_url=REPLY_URL, lang="zh")
        self.assertLessEqual(len(q.message.encode("utf-8")), render.QUESTION_MAX_BYTES)
        reply = "这是一条很长的回复，" * 200
        a = render.render_answered(q, reply)
        self.assertLessEqual(len(a.message.encode("utf-8")), render.MAX_MESSAGE_BYTES)
        self.assertIn(q.body, a.message)
        head, _, _ = a.message.partition(SEP)
        self.assertTrue(head.startswith("**【你的回复】** "))
        self.assertTrue(head.endswith(texts.t("reply.truncated", "zh")))
        kept = head[len("**【你的回复】** "):-len(texts.t("reply.truncated", "zh"))]
        self.assertTrue(reply.startswith(kept))  # 截的是尾巴，没有截坏多字节字符
        self.assertGreater(len(kept), 100)
        # 不超预算就一个字不动
        short = render.render_answered(q, "留固定目录")
        self.assertIn("**【你的回复】** 留固定目录\n", short.message)
        self.assertNotIn(texts.t("reply.truncated", "zh"), short.message)
        # 回复本身带加粗时，截断后的正文里 ** 仍成对：截断标记与原提问不会被卷进一段没闭合的粗体
        bold = render.render_answered(q, "**要点**：留固定目录。" * 200)
        self.assertLessEqual(len(bold.message.encode("utf-8")), render.MAX_MESSAGE_BYTES)
        bold_head, _, _ = bold.message.partition(SEP)
        self.assertEqual(bold_head.count("**") % 2, 0)
        self.assertTrue(bold_head.endswith(texts.t("reply.truncated", "zh")))


NOTIFY = {"title": "单测全绿，进入 code review", "body": "**进度**：42 条单测全过。\n\n接下来派 reviewer，预计 20 分钟。"}
NOTIFY_HINT = {"zh": "想回话，直接在这个 topic 里发消息。", "en": "To reply, just send a message in this topic."}


class RenderNotifyTest(unittest.TestCase):
    """通知卡：Title 同提问、正文 = agent 给的 Markdown 正文 + 分隔线 + 一句「想回话直接发消息」、没有按钮。"""

    def test_notify_layout(self):
        r = render.render_notify(NOTIFY, tag="wD", lang="zh")
        self.assertIsInstance(r, Rendered)
        self.assertEqual(r.title, "[wD] " + NOTIFY["title"])
        self.assertEqual(r.message, NOTIFY["body"] + SEP + NOTIFY_HINT["zh"])
        self.assertEqual(r.actions, [])
        self.assertEqual(r.body, NOTIFY["body"])  # 正文原样，不加分段标记
        self.assertEqual(r.lang, "zh")
        # 不带提问卡那段按钮提示
        self.assertNotIn("按钮", r.message)
        self.assertEqual(r.message.count(SEP), 1)

    def test_notify_english(self):
        r = render.render_notify({**NOTIFY, "title": "Tests green", "body": "done"}, tag="proj", lang="en")
        self.assertEqual(r.title, "[proj] Tests green")
        self.assertEqual(r.message, "done" + SEP + NOTIFY_HINT["en"])
        self.assertEqual(r.actions, [])

    def test_notify_tag_budget_is_the_same_as_question(self):
        with self.assertRaises(ValueError):
            render.render_notify(NOTIFY, tag="t" * 42, lang="zh")
        self.assertEqual(render.render_notify(NOTIFY, tag="t" * 41, lang="zh").title, "[" + "t" * 41 + "] " + NOTIFY["title"])

    def test_notify_tolerates_missing_fields(self):
        # 渲染不做校验：缺字段当空串，校验层要能对不完整的输入量字节数
        r = render.render_notify({}, tag="wD", lang="zh")
        self.assertEqual(r.title, "[wD] ")
        self.assertEqual(r.message, SEP + NOTIFY_HINT["zh"])
        self.assertEqual(render.render_notify({"title": 3, "body": None}, tag="wD", lang="zh").message, SEP + NOTIFY_HINT["zh"])

    # body 首尾空白去掉再发：首行缩进四个空格在 Markdown 里是代码块，尾部空行会把分隔线前的空行数撑成三行
    def test_notify_body_is_stripped_so_indent_is_not_a_code_block(self):
        r = render.render_notify({**NOTIFY, "body": "    不是代码块\n\n第二段  \n\n"}, tag="wD", lang="zh")
        self.assertEqual(r.message, "不是代码块\n\n第二段" + SEP + NOTIFY_HINT["zh"])
        self.assertEqual(render.notify_bytes({**NOTIFY, "body": "  x  "}, "zh"), len(("x" + SEP + NOTIFY_HINT["zh"]).encode("utf-8")))

    def test_notify_budget_is_the_full_message_limit(self):
        # 通知卡没有后续更新，不留余量：上限就是 ntfy 的 4096
        self.assertEqual(render.NOTIFY_MAX_BYTES, 4096)
        self.assertEqual(render.NOTIFY_MAX_BYTES, render.MAX_MESSAGE_BYTES)
        self.assertEqual(render.notify_bytes(NOTIFY, "zh"), len(render.render_notify(NOTIFY, tag="wD", lang="zh").message.encode("utf-8")))
        self.assertEqual(render.notify_message(NOTIFY, "en"), render.render_notify(NOTIFY, tag="x", lang="en").message)


EN_SAMPLE = {
    "title": "Keep or drop the temp dir",
    "doing": "Let the assistant work before the code is checked out",
    "description": "It used to require a local checkout. That gate is gone, so the child process needs a working directory.",
    "blocker": "With no checkout there is no natural working directory",
    "options": [
        {"id": "keep", "label": "Keep a fixed dir", "consequence": "one empty dir per project under the app data dir; easy to inspect, piles up"},
        {"id": "temp", "label": "Delete after use", "consequence": "a temp dir removed on exit; clean, but nothing left to inspect after a crash"},
    ],
    "recommend": "keep",
    "reasoning": "Keep it: these users are the ones most likely to misconfigure. Strongest objection: disk clutter, fixable with a 30-day sweep",
    "question": "Keep a fixed dir, or delete after use?",
}

# 黄金样本按版式规格逐字手写，不从渲染结果抄：提问卡 / 已回复卡 / 通知卡，zh / en 各一张
ZH_QUESTION = (
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
ZH_ANSWERED = (
    "**【你的回复】** 用完即删，30 天清理太麻烦\n\n"
    "---\n\n"
    "（以下为当时的提问）\n\n"
    + ZH_QUESTION.partition("\n\n---\n\n")[0]
)
ZH_NOTIFY = NOTIFY["body"] + "\n\n---\n\n想回话，直接在这个 topic 里发消息。"
EN_QUESTION = (
    "**[Doing]** " + EN_SAMPLE["doing"] + "\n\n"
    "**[Background]** " + EN_SAMPLE["description"] + "\n\n"
    "**[Blocker]** " + EN_SAMPLE["blocker"] + "\n\n"
    "**[Options]**\n\n"
    "1. **Keep a fixed dir** (recommended) → " + EN_SAMPLE["options"][0]["consequence"] + "\n"
    "2. **Delete after use** → " + EN_SAMPLE["options"][1]["consequence"] + "\n\n"
    "**[My recommendation]** " + EN_SAMPLE["reasoning"] + "\n\n"
    "**[Your call]** " + EN_SAMPLE["question"] + "\n\n"
    "---\n\n"
    "⚠️ The button is a shortcut.\n\n"
    "Disagree? Type your reply in the box below.\n\n"
    "A reply takes effect the moment you send it — it can't be withdrawn or amended, so say it all at once."
)
EN_ANSWERED = (
    "**[Your reply]** Delete after use\n\n"
    "---\n\n"
    "(the question as asked)\n\n"
    + EN_QUESTION.partition("\n\n---\n\n")[0]
)
EN_NOTIFY = "All tests pass.\n\n---\n\nTo reply, just send a message in this topic."


class BoldFirstLineTest(unittest.TestCase):
    def test_bolds_only_the_first_line(self):
        self.assertEqual(render.bold_first_line("一句话"), "**一句话**")
        self.assertEqual(render.bold_first_line("首行\n\n第二段\n第三行"), "**首行**\n\n第二段\n第三行")
        self.assertEqual(render.bold_first_line(""), "")  # 空首行不产出 ****（那是四个字面星号）
        self.assertEqual(render.bold_first_line("\n第二行"), "\n第二行")


class GoldenTest(unittest.TestCase):
    """整张卡片逐字比对：版式一个字都不许变，改版式必须显式改这里。"""

    def test_zh_question_answered_notify(self):
        q = render.render_question(SAMPLE, tag="wD", reply_url=REPLY_URL, lang="zh")
        self.assertEqual(q.message, ZH_QUESTION)
        self.assertEqual(q.title, "[wD] " + SAMPLE["title"])
        a = render.render_answered(q, "用完即删，30 天清理太麻烦")
        self.assertEqual(a.message, ZH_ANSWERED)
        self.assertEqual(a.title, "✅ 已回复 · [wD] " + SAMPLE["title"])
        n = render.render_notify(NOTIFY, tag="wD", lang="zh")
        self.assertEqual(n.message, ZH_NOTIFY)
        self.assertEqual(n.title, "[wD] " + NOTIFY["title"])

    def test_en_question_answered_notify(self):
        q = render.render_question(EN_SAMPLE, tag="proj", reply_url=REPLY_URL, lang="en")
        self.assertEqual(q.message, EN_QUESTION)
        self.assertEqual(q.title, "[proj] " + EN_SAMPLE["title"])
        self.assertEqual(q.actions[0]["label"], "Accept recommended")
        a = render.render_answered(q, "Delete after use")
        self.assertEqual(a.message, EN_ANSWERED)
        self.assertEqual(a.title, "✅ Answered · [proj] " + EN_SAMPLE["title"])
        n = render.render_notify({"title": "Tests green", "body": "All tests pass."}, tag="proj", lang="en")
        self.assertEqual(n.message, EN_NOTIFY)
        self.assertEqual(n.title, "[proj] Tests green")


if __name__ == "__main__":
    unittest.main()
