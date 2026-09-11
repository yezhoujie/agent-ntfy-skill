"""输入契约校验：agent 交来的提问 JSON 不合规就一次报全，文案让它能照着改。

校验不过 ⇒ 不发送任何消息 ⇒ 非零退出 + stderr 写清楚。这里只交两件事：
    check() / check_json()   返回全部问题的列表（不是报第一个就停：逐条报会让 agent 来回好几轮，
                             每轮都烧一次它的上下文，而校验是纯本地的，一次跑完几乎不花钱）
    format_problems()        把问题列表排成 stderr 文案，「消息未发送」那句由它保证——
                             少了它 agent 分不清「校验失败所以没发」与「发了但用户没回」
退出码与 stdout 由 CLI 入口负责。

长度那两条从渲染层取数（正文量的是渲染后的字节数），上限常量也在渲染层，这里不再写一遍数字。
"""

import json
import unicodedata
from dataclasses import dataclass

import render

OPTIONS_MIN = 2  # 只有一个选项不叫选择
OPTIONS_MAX = 5  # 多于 5 项说明问题没收敛，该先想清楚再问

# 必填字段与「写什么」——报缺失时给出来，让 agent 不用回头翻契约
FIELD_HINTS = {
    "title": "通知栏那一行钩子，预览只看得到它",
    "doing": "一句话说这是哪件事",
    "description": "背景展开，写给一个完全没看过执行过程的人",
    "blocker": "具体卡在哪",
    "options": "2~5 个 {id, label, consequence}，consequence 写实际后果",
    "recommend": "推荐项的 id",
    "reasoning": "要写倾向的理由 + 最强的反对意见",
    "question": "一句能被一句话回答的问题",
}
OPTION_KEYS = ("id", "label", "consequence")
HEADER = "agent-ntfy ask: 输入校验未通过（{n} 处），全部修正后重试，消息未发送。"
FIELD_WIDTH = 11  # 字段名列宽（最长的 description 正好 11）


@dataclass(frozen=True)
class Problem:
    field: str  # 出问题的字段：title / options / recommend / 正文 / JSON
    message: str  # 可照着改的说明：说清要求 + 现有值


def _missing(payload: dict, name: str) -> str | None:
    """缺失 / 类型不对 / 空（含只有空白）——返回原因，合格返回 None。"""
    if name not in payload:
        return "缺失"
    v = payload[name]
    if name == "options":
        if not isinstance(v, list):
            return f"不是数组（是 {type(v).__name__}）"
        return "是空数组" if not v else None
    if not isinstance(v, str):
        return f"不是字符串（是 {type(v).__name__}）"
    return "是空串" if not v.strip() else None


def _check_options(options: list) -> list[Problem]:
    problems = []
    n = len(options)
    if n < OPTIONS_MIN:
        problems.append(Problem("options", f"只有 {n} 项，要求 {OPTIONS_MIN}~{OPTIONS_MAX} 项（只有一个选项不叫选择）"))
    elif n > OPTIONS_MAX:
        problems.append(Problem("options", f"有 {n} 项，要求 {OPTIONS_MIN}~{OPTIONS_MAX} 项（多于 {OPTIONS_MAX} 项说明问题没收敛，先想清楚再问）"))
    seen: dict[str, list[int]] = {}
    for i, o in enumerate(options, start=1):
        # 单项问题也挂在 options 名下，文案里点「第 N 项」（从 1 数，与 id 重复那条同款），不用下标形态
        if not isinstance(o, dict):
            problems.append(Problem("options", f"第 {i} 项不是对象（是 {type(o).__name__}）。每项要有 id / label / consequence"))
            continue
        lacking = [k for k in OPTION_KEYS if not isinstance(o.get(k), str) or not o[k].strip()]
        if lacking:
            problems.append(Problem("options", f"第 {i} 项缺 {' / '.join(lacking)}。每项要有 id / label / consequence，且都非空"))
        if isinstance(o.get("id"), str) and o["id"].strip():
            seen.setdefault(o["id"], []).append(i)
    for oid, where in seen.items():
        if len(where) > 1:
            problems.append(Problem("options", f"id 重复：{oid}（第 {'、'.join(map(str, where))} 项）"))
    return problems


def check(payload: object) -> list[Problem]:
    """对已解析的输入跑全部检查，返回全部问题；合格返回空列表。任何形态的脏输入都只产生 Problem，不抛异常。"""
    if not isinstance(payload, dict):
        return [Problem("JSON", f"顶层必须是对象，现在是 {type(payload).__name__}")]
    problems = []
    for name, hint in FIELD_HINTS.items():
        why = _missing(payload, name)
        if why:
            problems.append(Problem(name, f"{why}。必填，{hint}"))
    options = payload.get("options")
    if isinstance(options, list) and options:
        problems.extend(_check_options(options))
        ids = [o["id"] for o in options if isinstance(o, dict) and isinstance(o.get("id"), str) and o["id"].strip()]
        rec = payload.get("recommend")
        if isinstance(rec, str) and rec.strip() and rec not in ids:
            problems.append(Problem("recommend", f'"{rec}" 不在 options 的 id 里（现有 id: {", ".join(ids) or "无"}）'))
    # 长度两条：渲染对缺字段容错（当空串），所以能与上面的错误一次报全
    size = render.message_bytes(payload)
    if size > render.QUESTION_MAX_BYTES:
        problems.append(Problem("正文", f"渲染后 {size} 字节，上限 {render.QUESTION_MAX_BYTES} 字节，超出 {size - render.QUESTION_MAX_BYTES} 字节。"
                                      "精简 description / consequence / reasoning（不会替你截断）"))
    title = payload.get("title")
    if isinstance(title, str):
        tsize = len(title.encode("utf-8"))
        if tsize > render.TITLE_MAX_BYTES:
            problems.append(Problem("title", f"{tsize} 字节，上限 {render.TITLE_MAX_BYTES} 字节，超出 {tsize - render.TITLE_MAX_BYTES} 字节。"
                                             "title 是通知栏那一行钩子，写短"))
        if "\n" in title or "\r" in title:
            # 放过它的话，回复到达后的更新（title 走 HTTP 头）才会失败，卡片带着按钮悬在手机上
            problems.append(Problem("title", "含换行。title 是通知栏那一行，只能一行"))
    return sorted(problems, key=_field_order)


def _field_order(p: Problem) -> int:
    """按契约里的字段顺序排（同字段保持产生顺序），正文最后——与 agent 手里那份 JSON 的顺序一致，改起来顺手。"""
    names = list(FIELD_HINTS)
    return names.index(p.field) if p.field in names else len(names)


def check_json(text: str) -> tuple[dict | None, list[Problem]]:
    """从原始文本（stdin 全文）开始：语法错只报这一条（其余无从检查），否则同 check()。

    不变式：problems 为空 ⇔ payload 是那个合格的 dict；有任何问题 payload 都是 None（能解析但不合格也是）。
    """
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as e:
        return None, [Problem("JSON", f"不是合法 JSON：{e.msg}（第 {e.lineno} 行第 {e.colno} 列）")]
    problems = check(payload)
    return (payload if isinstance(payload, dict) and not problems else None), problems


def _pad(text: str, width: int) -> str:
    """按显示宽度补空格：全角字算 2 列，不然「正文」那行会比别的行宽。"""
    cols = sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)
    return text + " " * max(width - cols, 0)


def format_problems(problems: list[Problem]) -> str:
    """排成 stderr 文案。首行点明「消息未发送」，之后一行一个问题，字段名对齐。"""
    lines = [HEADER.format(n=len(problems)), ""]
    lines += [f"  {_pad(p.field, FIELD_WIDTH)}: {p.message}" for p in problems]
    return "\n".join(lines) + "\n"
