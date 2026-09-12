"""消息渲染：把 agent 交来的 JSON 变成固定版式的通知文本与唯一的按钮；回复到达后拼「已回复」态的更新文本。

纯函数，不知道 topic 名：按钮回传地址与通知标题里的 [<tag>] 都由调用方以字符串传入。
版式由这里定死，agent 只给数据——格式统一、末尾那句提示永远不会漏、按钮永远只有一个。
纯文本层次（【正在做】/ [Doing] 这类方括号做分段标记），不依赖 Markdown 渲染：控制不了用户用哪个客户端。
固定文案全部从 texts 表按 lang 取；lang 由调用方显式传（这里不读环境变量），渲染结果自带 lang，
之后同一张卡片的更新（已回复 / 超时 / 取消）沿用它——同一张卡片前后两种语言是不可接受的。

正文字节预算：ntfy 正文上限 4096 字节，超过会被转成附件而不是当消息推送。这个上限被两条消息共用——
提问（六段 + 末尾提示）与已回复更新（回复 + 包装 + 原六段）。所以提问放行上限是 4096 − 512，
给回复留 512 字节；回复再长也只截回复并加标记，原提问保持完整（这条记录的价值就是事后能翻）。
"""

from dataclasses import dataclass

import texts
from ntfyclient import MAX_MESSAGE_BYTES, http_action

REPLY_RESERVE_BYTES = 512
QUESTION_MAX_BYTES = MAX_MESSAGE_BYTES - REPLY_RESERVE_BYTES  # 3584：提问渲染后正文的放行上限
# ntfy 对 title 的硬顶是 1 KB（超过 HTTP 400，量的是 RFC 2047 解码后的字节数，实测）；
# 扣掉「✅ 已回复 · 」前缀与 [<tag>] 的余量
TITLE_MAX_BYTES = 960

# 按钮文案固定（texts button.accept）：位置与文案稳定形成肌肉记忆，短到手机上不会被截断
# 同 seq 更新时拼在原 Title 前的三个前缀（texts prefix.answered / timeout / cancelled）放 Title 而不是正文：
# 消息列表里一眼扫过去就知道哪些答过 / 过期了；其余 prefix.*（已释放 / 已忽略 / 被取代 / 已确认）只用于回执与确认卡
SEPARATOR = "──────────"  # 不分语言
# 末尾提示（texts hint）三句各有分工：按钮是什么 · 不同意怎么办 · 为什么要一次说完。放正文末尾：通知栏预览只有 Title + 1~2 行，
# 放开头会把真正的问题挤出预览；放末尾紧挨按钮，要看到按钮就必然已经点进来了
NTFY_TITLE_LIMIT = 1024
# 会拼到提问 Title 前的三个前缀（已回复 / 超时 / 取消）；其余 prefix.*（已释放 / 已忽略 / 被取代 / 已确认）只拼在回执与确认卡的
# 固定短 Title 前，不参与 tag 预算
QUESTION_PREFIX_KEYS = ("prefix.answered", "prefix.timeout", "prefix.cancelled")
# tag 的预算：1 KB 减去这三个前缀在两种语言里的最大值（「⚠️ 已取消 · 」/「⚠️ Cancelled · 」20 字节）、title 上限、
# 「[」「]」「 」三个字节 = 41。tag 是调用方给的（pane_id / 槽位名），超了是调用方的错
PREFIX_MAX_BYTES = max(len(texts.t(key, lang).encode("utf-8")) for lang in texts.LANGS for key in QUESTION_PREFIX_KEYS)
TAG_MAX_BYTES = NTFY_TITLE_LIMIT - PREFIX_MAX_BYTES - TITLE_MAX_BYTES - 3


@dataclass(frozen=True)
class Rendered:
    """一条可直接交给 NtfyClient.publish(topic, message, title=…, actions=…) 的消息。

    body 是六段正文本身（不含分隔线与提示）——回复到达后拼「已回复」态要用它，
    发布方必须把这个对象留到那时候，发完就丢就拼不出来了。
    """

    title: str
    message: str
    actions: list[dict]
    body: str
    lang: str  # 这张卡片的固定文案语言；之后同 seq 的更新沿用它


def _text(payload: dict, key: str) -> str:
    """缺字段 / 非字符串一律当空串：渲染不做校验，校验层要能对不完整的输入量字节数、一次报全。"""
    v = payload.get(key)
    return v if isinstance(v, str) else ""


def _options(payload: dict) -> list[dict]:
    opts = payload.get("options")
    return [o for o in opts if isinstance(o, dict)] if isinstance(opts, list) else []


def _is_recommended(option: dict, payload: dict) -> bool:
    rec = _text(payload, "recommend")
    return bool(rec) and _text(option, "id") == rec  # 两边都缺时 "" == "" 不能算命中


def recommended_label(payload: dict) -> str:
    """推荐项的 label——按钮回传的就是它（回传「采纳推荐」四个字的话，agent 重置过就不知道采纳了什么）。"""
    for o in _options(payload):
        if _is_recommended(o, payload):
            return _text(o, "label")
    return ""


def render_body(payload: dict, lang: str) -> str:
    """六段正文，标记与顺序固定：正在做 / 背景 / 卡点 / 选项 / 我的建议 / 要你定。不硬换行，由手机自行折行。"""
    lines = []
    for i, o in enumerate(_options(payload), start=1):
        key = "option.line_recommended" if _is_recommended(o, payload) else "option.line"
        lines.append(texts.t(key, lang, i=i, label=_text(o, "label"), consequence=_text(o, "consequence")))
    return "\n\n".join([
        texts.t("section.doing", lang) + _text(payload, "doing"),
        texts.t("section.background", lang) + _text(payload, "description"),
        texts.t("section.blocker", lang) + _text(payload, "blocker"),
        texts.t("section.options", lang) + "\n" + "\n".join(lines),
        texts.t("section.reasoning", lang) + _text(payload, "reasoning"),
        texts.t("section.question", lang) + _text(payload, "question"),
    ])


def _compose(body: str, lang: str) -> str:
    """六段 + 分隔线 + 末尾提示。校验量的与实际发出的必须是同一条模板，所以只写这一处。"""
    return f"{body}\n{SEPARATOR}\n{texts.t('hint', lang)}"


def render_message(payload: dict, lang: str) -> str:
    """提问的完整正文。校验层量的就是它的字节数。"""
    return _compose(render_body(payload, lang), lang)


def message_bytes(payload: dict, lang: str) -> int:
    return len(render_message(payload, lang).encode("utf-8"))


def fixed_overhead_bytes(lang: str) -> int:
    """该语言的固定开销：分隔线 + 末尾提示（`_compose("")` 的字节数，与正文预算文档里的口径一致；六段标签另算）。"""
    return len(_compose("", lang).encode("utf-8"))


def render_title(payload: dict, tag: str) -> str:
    """通知 Title = [<tag>] <title>。tag 超预算抛 ValueError：加上已回复前缀后会撞 ntfy 的 1 KB 硬顶。"""
    size = len(tag.encode("utf-8"))
    if size > TAG_MAX_BYTES:
        raise ValueError(f"tag {size} 字节，超过 {TAG_MAX_BYTES} 字节的预算——已回复态的 Title 会超过 ntfy 的 {NTFY_TITLE_LIMIT} 字节上限")
    return f"[{tag}] {_text(payload, 'title')}"


def render_question(payload: dict, *, tag: str, reply_url: str, lang: str) -> Rendered:
    """提问消息。tag 进 Title（herdr 内是 pane_id，否则是槽位名，取什么值是调用方的事）；
    reply_url 是按钮回传地址（通常就是这个 topic 自己的地址）；lang 由调用方解析好传入。"""
    body = render_body(payload, lang)
    return Rendered(
        title=render_title(payload, tag),
        message=_compose(body, lang),
        actions=[http_action(texts.t("button.accept", lang), reply_url, recommended_label(payload))],
        body=body,
        lang=lang,
    )


def _cut_utf8(text: str, limit: int) -> str:
    """按字节截断但不截坏多字节字符。"""
    return text.encode("utf-8")[:max(limit, 0)].decode("utf-8", errors="ignore")


def render_answered(question: Rendered, reply: str) -> Rendered:
    """已回复态：Title 加前缀，正文 = 回复 + 分隔线 + 原六段正文，不带按钮、不带末尾提示。

    回复的预算 = 4096 − 包装 − 原正文；超出只截回复并加标记，原提问一个字不动。
    交给 agent 的永远是回复全文，截断只发生在手机上这条记录里。
    前置条件：question 是过了校验的提问（正文 ≤ 3584 字节）；原正文本身就装不下时抛 ValueError，
    不产出一条标着「已截断」却一个字回复都没有的消息。
    """
    lang = question.lang  # 更新沿用首发语言，不看处理那一刻的环境
    reply = reply.strip()  # 手机记录里不带首尾的空白与换行；交给 agent 的原文不经这里
    head, quote, truncated = texts.t("reply.head", lang), texts.t("reply.quote_head", lang), texts.t("reply.truncated", lang)
    wrapper = f"{head}\n{SEPARATOR}\n{quote}\n"
    budget = MAX_MESSAGE_BYTES - len(wrapper.encode("utf-8")) - len(question.body.encode("utf-8"))
    if budget < len(truncated.encode("utf-8")):
        raise ValueError(f"原提问正文 {len(question.body.encode('utf-8'))} 字节，连回复的截断标记都放不下——它没过校验")
    if len(reply.encode("utf-8")) > budget:
        reply = _cut_utf8(reply, budget - len(truncated.encode("utf-8"))) + truncated
    return Rendered(
        title=texts.t("prefix.answered", lang) + question.title,
        message=f"{head}{reply}\n{SEPARATOR}\n{quote}\n{question.body}",
        actions=[],
        body=question.body,
        lang=lang,
    )
