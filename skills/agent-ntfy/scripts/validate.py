"""输入契约校验：agent 交来的提问 / 通知 JSON 不合规就一次报全，文案让它能照着改。

校验不过 ⇒ 不发送任何消息 ⇒ 非零退出 + stderr 写清楚。这里只交三件事：
    check() / check_json()                 提问：返回全部问题的列表（不是报第一个就停：逐条报会让 agent 来回好几轮，
                                           每轮都烧一次它的上下文，而校验是纯本地的，一次跑完几乎不花钱）
    check_notify() / check_notify_json()   通知：同一对形态，契约只有 title / body（/ lang）
    format_problems()                      把问题列表排成 stderr 文案，「消息未发送」那句由它保证——
                                           少了它 agent 分不清「校验失败所以没发」与「发了但用户没回」
退出码与 stdout 由 CLI 入口负责。

长度那两条从渲染层取数（正文量的是渲染后的字节数），上限常量也在渲染层，这里不再写一遍数字。
报错文案按 lang 从 texts 表取：lang 由调用方解析好传入（JSON 里的 lang 合法就用它，否则环境变量，否则 en）；
lang 字段本身也在校验之列——给了但不是 zh / en 就与其它错误一起报，不静默回退。
"""

import json
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass

import render
import texts

OPTIONS_MIN = 2  # 只有一个选项不叫选择
OPTIONS_MAX = 5  # 多于 5 项说明问题没收敛，该先想清楚再问

# 必填字段；「写什么」的提示在 texts（validate.hint.<字段>）——报缺失时给出来，让 agent 不用回头翻契约
REQUIRED_FIELDS = ("title", "doing", "description", "blocker", "options", "recommend", "reasoning", "question")
OPTION_KEYS = ("id", "label", "consequence")
BODY_FIELD = "body"  # 提问卡「正文」（渲染后整体）那条问题挂的字段名；显示时按语言取（validate.field.body）
NOTIFY_REQUIRED_FIELDS = ("title", "body")  # 通知卡：body 就是 JSON 里的字段，显示时不译
COMMANDS = ("ask", "notify")  # format_problems 的抬头按命令取（validate.header / validate.notify.header）
FIELD_WIDTH = 11  # 字段名列宽（最长的 description 正好 11）


@dataclass(frozen=True)
class Problem:
    field: str  # 出问题的字段：title / options / recommend / lang / body / JSON
    message: str  # 可照着改的说明（已按语言取好）：说清要求 + 现有值


def _missing(payload: dict, name: str, lang: str) -> str | None:
    """缺失 / 类型不对 / 空（含只有空白）——返回原因，合格返回 None。"""
    if name not in payload:
        return texts.t("validate.missing", lang)
    v = payload[name]
    if name == "options":
        if not isinstance(v, list):
            return texts.t("validate.not_array", lang, type=type(v).__name__)
        return texts.t("validate.empty_array", lang) if not v else None
    if not isinstance(v, str):
        return texts.t("validate.not_string", lang, type=type(v).__name__)
    return texts.t("validate.empty_string", lang) if not v.strip() else None


def _check_options(options: list, lang: str) -> list[Problem]:
    problems = []
    n = len(options)
    if n < OPTIONS_MIN:
        problems.append(Problem("options", texts.t("validate.options.too_few", lang, n=n, min=OPTIONS_MIN, max=OPTIONS_MAX)))
    elif n > OPTIONS_MAX:
        problems.append(Problem("options", texts.t("validate.options.too_many", lang, n=n, min=OPTIONS_MIN, max=OPTIONS_MAX)))
    seen: dict[str, list[int]] = {}
    for i, o in enumerate(options, start=1):
        # 单项问题也挂在 options 名下，文案里点「第 N 项」（从 1 数，与 id 重复那条同款），不用下标形态
        if not isinstance(o, dict):
            problems.append(Problem("options", texts.t("validate.options.not_object", lang, i=i, type=type(o).__name__)))
            continue
        lacking = [k for k in OPTION_KEYS if not isinstance(o.get(k), str) or not o[k].strip()]
        if lacking:
            problems.append(Problem("options", texts.t("validate.options.lacking", lang, i=i, keys=" / ".join(lacking))))
        if isinstance(o.get("id"), str) and o["id"].strip():
            seen.setdefault(o["id"], []).append(i)
    for oid, where in seen.items():
        if len(where) > 1:
            sep = texts.t("validate.options.dup_sep", lang)
            problems.append(Problem("options", texts.t("validate.options.dup_id", lang, id=oid, where=sep.join(map(str, where)))))
    return problems


def check(payload: object, lang: str) -> list[Problem]:
    """对已解析的输入跑全部检查，返回全部问题；合格返回空列表。任何形态的脏输入都只产生 Problem，不抛异常。

    lang 是报错文案与字节预算所用的语言——调用方按「JSON 里的 lang 合法就用它，否则环境变量，否则 en」解析好再传。
    """
    if not isinstance(payload, dict):
        return [Problem("JSON", texts.t("validate.top_not_object", lang, type=type(payload).__name__))]
    problems = []
    for name in REQUIRED_FIELDS:
        why = _missing(payload, name, lang)
        if why:
            problems.append(Problem(name, texts.t("validate.required", lang, why=why, hint=texts.t(f"validate.hint.{name}", lang))))
    options = payload.get("options")
    if isinstance(options, list) and options:
        problems.extend(_check_options(options, lang))
        ids = [o["id"] for o in options if isinstance(o, dict) and isinstance(o.get("id"), str) and o["id"].strip()]
        rec = payload.get("recommend")
        if isinstance(rec, str) and rec.strip() and rec not in ids:
            problems.append(Problem("recommend", texts.t("validate.recommend", lang, rec=rec, ids=", ".join(ids) or texts.t("validate.recommend.none", lang))))
    problems.extend(_check_lang(payload, lang))
    # 长度两条：渲染对缺字段容错（当空串），所以能与上面的错误一次报全；字节数按这张卡片将要用的语言量
    size = render.message_bytes(payload, lang)
    if size > render.QUESTION_MAX_BYTES:
        problems.append(Problem(BODY_FIELD, texts.t("validate.body_too_long", lang, size=size, limit=render.QUESTION_MAX_BYTES, over=size - render.QUESTION_MAX_BYTES)))
    problems.extend(_check_title(payload, lang))
    return sorted(problems, key=_field_order(REQUIRED_FIELDS))


def _check_lang(payload: dict, lang: str) -> list[Problem]:
    if payload.get("lang") is not None and not texts.is_lang(payload["lang"]):  # null 当没给
        # 不静默回退：给错了语言码就明说可选值；报错本身用的语言由调用方按环境变量兜底
        return [Problem("lang", texts.t("validate.lang", lang, value=payload["lang"], choices=" / ".join(texts.LANGS)))]
    return []


def _check_title(payload: dict, lang: str) -> list[Problem]:
    """title 的长度与换行：提问与通知共用（缺失 / 空另由 _missing 报）。"""
    problems = []
    title = payload.get("title")
    if isinstance(title, str):
        tsize = len(title.encode("utf-8"))
        if tsize > render.TITLE_MAX_BYTES:
            problems.append(Problem("title", texts.t("validate.title_too_long", lang, size=tsize, limit=render.TITLE_MAX_BYTES, over=tsize - render.TITLE_MAX_BYTES)))
        if "\n" in title or "\r" in title:
            # 提问放过它的话，回复到达后的更新（title 走 HTTP 头）才会失败，卡片带着按钮悬在手机上；通知的 Title 同样是通知栏那一行
            problems.append(Problem("title", texts.t("validate.title_newline", lang)))
    return problems


def check_notify(payload: object, lang: str) -> list[Problem]:
    """通知卡的全部检查：title / body 必填非空、title ≤ TITLE_MAX_BYTES 且单行、渲染后 ≤ NOTIFY_MAX_BYTES、lang 合法。
    形态同 check()：任何脏输入都只产生 Problem，不抛异常；lang 由调用方解析好传入。"""
    if not isinstance(payload, dict):
        return [Problem("JSON", texts.t("validate.top_not_object", lang, type=type(payload).__name__))]
    problems = []
    for name in NOTIFY_REQUIRED_FIELDS:
        why = _missing(payload, name, lang)
        if why:
            hint = texts.t("validate.hint.title" if name == "title" else "validate.notify.hint.body", lang)
            problems.append(Problem(name, texts.t("validate.required", lang, why=why, hint=hint)))
    problems.extend(_check_lang(payload, lang))
    size = render.notify_bytes(payload, lang)
    if size > render.NOTIFY_MAX_BYTES:
        problems.append(Problem("body", texts.t("validate.notify.body_too_long", lang, size=size, limit=render.NOTIFY_MAX_BYTES, over=size - render.NOTIFY_MAX_BYTES)))
    problems.extend(_check_title(payload, lang))
    return sorted(problems, key=_field_order(NOTIFY_REQUIRED_FIELDS))


def _field_order(required: tuple[str, ...]) -> Callable[[Problem], int]:
    """按契约里的字段顺序排（同字段保持产生顺序），lang 与提问卡的「正文」最后——与 agent 手里那份 JSON 的顺序一致，改起来顺手。"""
    names = list(required) + ["lang"]
    return lambda p: names.index(p.field) if p.field in names else len(names)


def lang_of(payload: object, fallback: str) -> str:
    """这份输入将要用的语言：JSON 里的 lang 合法就是它，否则用调用方给的兜底（环境变量解析结果）。"""
    explicit = payload.get("lang") if isinstance(payload, dict) else None
    return texts.resolve(explicit, fallback)


def check_json(text: str, fallback_lang: str = texts.DEFAULT_LANG) -> tuple[dict | None, list[Problem], str]:
    """从原始文本（stdin 全文）开始：语法错只报这一条（其余无从检查），否则同 check()。返回的第三项是报错所用的语言。

    不变式：problems 为空 ⇔ payload 是那个合格的 dict；有任何问题 payload 都是 None（能解析但不合格也是）。
    """
    return _check_text(text, fallback_lang, check)


def check_notify_json(text: str, fallback_lang: str = texts.DEFAULT_LANG) -> tuple[dict | None, list[Problem], str]:
    """通知卡的 check_json：从 stdin 全文开始，不变式与返回形态同 check_json()。"""
    return _check_text(text, fallback_lang, check_notify)


def _check_text(text: str, fallback_lang: str, checker: Callable[[object, str], list[Problem]]) -> tuple[dict | None, list[Problem], str]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as e:
        return None, [Problem("JSON", texts.t("validate.bad_json", fallback_lang, msg=e.msg, line=e.lineno, col=e.colno))], fallback_lang
    lang = lang_of(payload, fallback_lang)
    problems = checker(payload, lang)
    return (payload if isinstance(payload, dict) and not problems else None), problems, lang


def field_label(field: str, lang: str, *, command: str = "ask") -> str:
    """问题挂的字段名的显示形态：提问卡的「正文」（渲染后整体，不是 JSON 字段）按语言取，其余就是 JSON 字段名——
    通知卡的 body 是 JSON 字段，照原名显示。"""
    return texts.t("validate.field.body", lang) if command == "ask" and field == BODY_FIELD else field


def _pad(text: str, width: int) -> str:
    """按显示宽度补空格：全角字算 2 列，不然「正文」那行会比别的行宽。"""
    cols = sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)
    return text + " " * max(width - cols, 0)


def format_problems(problems: list[Problem], lang: str, *, command: str = "ask") -> str:
    """排成 stderr 文案。首行点明是哪个命令、「消息未发送」，之后一行一个问题，字段名对齐。"""
    if command not in COMMANDS:
        raise ValueError(f"没有这个命令的报错抬头：{command!r}")
    header = "validate.header" if command == "ask" else "validate.notify.header"
    lines = [texts.t(header, lang, n=len(problems)), ""]
    lines += [f"  {_pad(field_label(p.field, lang, command=command), FIELD_WIDTH)}: {p.message}" for p in problems]
    return "\n".join(lines) + "\n"
