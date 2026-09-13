"""注入层：把用户从手机发来的话送进目标 agent 的窗格；送不到时说清为什么。

    手机 → topic → daemon（无 pending）→ deliver() ──herdr agent prompt <pane_id> '[agent-ntfy remote] <原文>'──▶ 目标 agent
                                              └─ 送不到 → 回执（[<slot>] 消息未送达 + 控制按钮）→ 手机

只做通路：只加来源前缀（REMOTE_PREFIX，让 agent 知道这条来自远程通道），原文本身一个字不改、不判断目标忙不忙
（AI CLI 自己会排队）。要判断的只有「目标在不在」——租约是懒释放的，租约看着有效而它指向的 pane 早已消失是常态，
不是边缘情况。前缀是协议（与控制标记、[<tag>] 同类）：不翻译、不进文案表；刻意不用 `from:` 开头——那是多 agent 组队里
成员消息的约定，用它会让统筹的那个 agent 把用户的话当成陌生成员的。

能力判定按 herdr CLI 的调用结果，不按环境变量：daemon 可能是 --detach 起的、环境里没有 HERDR_*，
但 herdr 本身在跑。实测（herdr 0.9.0）：`env -i PATH=… HOME=… herdr pane list` 照样 rc=0——
CLI 不靠环境变量找 server，默认落在 ~/.config/herdr/herdr.sock。

herdr 成功返回一行 JSON {"id":"cli:…","result":{…,"type":"…"}}（type 在 result 里）；失败形态有两种（实测 herdr 0.9.0）：
    rc=1 + stderr 一行 JSON {"error":{"code":"…","message":"…"}}   —— server_not_running / agent_not_found / agent_blocked / empty_agent_prompt
    rc=2 + stderr 纯文本                                          —— 用法错误（unknown option / requires text）
两种 stdout 都是空。回执里只带 error.code 或退出码，不带 stderr 原文：stderr 可能回显正文。
herdr 不认 `--` 分隔符（会把它当 TARGET）；TEXT 位置上以 `-` 开头的串被当正文，不当选项（实测，目标不存在的探针）。

按接收方 CLI 分档（herdr 0.9.0 上对 claude / kimi 的实测，各条证据强度标在行尾）：
    claude：prompt 并进当前那一轮，直接发即可                       —— 实测
    kimi  ：prompt 只排队，目标忙时一条都读不到；紧跟 send-keys ctrl+s 把它插进去，
            顺序不能反（先 ctrl+s 时队列是空的，什么也不会发生）       —— 排队不读到 = 实测；顺序前置 = 实测；ctrl+s 为 kimi 特有 = 未实测
    其他 / 没检测到 agent：只 prompt
分档依据是 pane 对象的 agent 字段（静态，查一次即可），不是运行状态。
"""

import json
import logging
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass

import texts
from ntfyclient import http_action
from render import SEPARATOR, Rendered, bold_first_line

HERDR = "herdr"
REMOTE_PREFIX = "[agent-ntfy remote] "  # 注入正文前的来源标记（协议）
# 三条命令都是本机 unix socket IPC，实测毫秒级返回；prompt 不带 --wait，提交即返回、不追踪回合。
# 15 秒够熬过机器卡顿，又不至于让工作线程被一条投递挂死。
HERDR_TIMEOUT = 15.0
RC_NOT_FOUND = 127
RC_TIMEOUT = 124
KIMI_WAKE_KEY = "ctrl+s"

# 回执的 Title / 按钮 / 正文都从 texts 表取（receipt.* / prefix.*）。通知栏只看得到 Title：其实可能已送达的情形
# （超时 / 唤醒失败 / 出错）用「可能未送达」那个 Title，不能写死「未送达」
UNCERTAIN_REASONS = ("prompt_timeout", "wake_failed", "error")

MARK_ACTIONS = ("release", "ignore", "confirmed")  # 回执的两个按钮 + 可达性确认的「我收到了」
# 只认这一个精确形态（fullmatch），且槽位名要与消息所在的槽位相同——由调用方核；不匹配就是普通消息。
# 这是「通路不解释内容」的唯一例外：认自己生成的固定标记可以，解析用户自由输入的文本不行。
MARK_RE = re.compile(r"__agent-ntfy:(release|ignore|confirmed):(slot[1-9][0-9]*)__")


LOG = logging.getLogger("agent-ntfy.inject")


# ---------------------------------------------------------------- herdr 子进程

@dataclass(frozen=True)
class HerdrResult:
    rc: int
    stdout: str
    stderr: str
    timed_out: bool = False
    timeout: float = HERDR_TIMEOUT  # 这次调用用的上限，summary() 报的是它

    @property
    def ok(self) -> bool:
        return self.rc == 0 and not self.timed_out

    def error_code(self) -> str | None:
        """stderr 那行 JSON 里的 error.code；不是 JSON（用法错误）或没有 code 就是 None。"""
        try:
            obj = json.loads(self.stderr)
        except ValueError:
            return None
        if not isinstance(obj, dict):
            return None
        err = obj.get("error")
        code = err.get("code") if isinstance(err, dict) else None
        return code if isinstance(code, str) and code else None

    def summary(self, lang: str) -> str:
        """给回执与日志用的一句话。只有 code / 退出码 / 超时，不带 stderr 原文（它可能回显正文）。"""
        if self.timed_out:
            return texts.t("herdr.timeout", lang, seconds=f"{self.timeout:g}")
        if self.rc == RC_NOT_FOUND:
            return texts.t("herdr.missing", lang)
        code = self.error_code()
        return code if code else texts.t("herdr.rc", lang, rc=self.rc)


Runner = Callable[[list[str]], HerdrResult]


def run_herdr(argv: list[str], *, timeout: float = HERDR_TIMEOUT) -> HerdrResult:
    """跑一条 herdr 命令，不抛：命令不存在 → rc 127；超时 → rc 124 且 timed_out。正文只进 argv，不经 shell。

    stdin 接 /dev/null：daemon 前台跑时 stdin 是终端，子进程若继承它、又碰巧等输入，就会挂满整个超时。
    win32 上先用 shutil.which 定位可执行名（herdr.exe / herdr.cmd），CreateProcess 不会自己补扩展名。
    """
    if sys.platform == "win32" and argv and argv[0] == HERDR:
        resolved = shutil.which(HERDR)
        if resolved is None:
            return HerdrResult(rc=RC_NOT_FOUND, stdout="", stderr=f"{HERDR}: command not found", timeout=timeout)
        argv = [resolved, *argv[1:]]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, errors="replace", timeout=timeout,
                           stdin=subprocess.DEVNULL)  # stderr 不是合法 UTF-8 也不抛
    except FileNotFoundError:
        return HerdrResult(rc=RC_NOT_FOUND, stdout="", stderr=f"{argv[0]}: command not found", timeout=timeout)
    except subprocess.TimeoutExpired:
        return HerdrResult(rc=RC_TIMEOUT, stdout="", stderr="", timed_out=True, timeout=timeout)
    return HerdrResult(rc=r.returncode, stdout=r.stdout, stderr=r.stderr, timeout=timeout)


def parse_panes(stdout: str) -> list[dict] | None:
    """`herdr pane list` 的 stdout → pane 对象列表；不是那个形态就 None（调用方当 herdr 不通）。"""
    try:
        obj = json.loads(stdout)
    except ValueError:
        return None
    result = obj.get("result") if isinstance(obj, dict) else None
    panes = result.get("panes") if isinstance(result, dict) else None
    if not isinstance(panes, list):
        return None
    return [p for p in panes if isinstance(p, dict)]


# ---------------------------------------------------------------- 窗格（CLI 侧用：开确认窗格 / 起 daemon；daemon 不用）

def split_pane(cwd: str, pane_id: str, *, run: Runner = run_herdr) -> str | None:
    """在 pane_id 下方开一个新窗格（cwd 指定、不抢焦点），返回新窗格的 pane_id；开不出来（rc 非 0 / 输出不是预期形态）就 None。"""
    r = run([HERDR, "pane", "split", "--pane", pane_id, "--direction", "down", "--cwd", cwd, "--no-focus"])
    if not r.ok:
        return None
    try:
        obj = json.loads(r.stdout)
    except ValueError:
        return None
    result = obj.get("result") if isinstance(obj, dict) else None
    pane = result.get("pane") if isinstance(result, dict) else None
    new_id = pane.get("pane_id") if isinstance(pane, dict) else None
    return new_id if isinstance(new_id, str) and new_id else None


def run_in_pane(pane_id: str, argv: list[str], *, run: Runner = run_herdr) -> bool:
    """往窗格里敲一条命令并回车。`pane run` 把各参数按空格拼接后原样敲进窗格 shell、不做任何引用（实测 herdr 0.9.0：
    `x'y` 会挂在 quote>、`$HOME` 会被展开），所以 argv 逐个按该平台 shell 的规则引用、合成一个参数传过去。"""
    def quote(a: str) -> str:
        # 以 = 开头的词 shlex.quote 不加引号，而 zsh 会对它做等值展开（=ls → /bin/ls）：强制单引号包住
        return "'" + a.replace("'", "'\\''") + "'" if a.startswith("=") else shlex.quote(a)
    command = subprocess.list2cmdline(argv) if sys.platform == "win32" else " ".join(quote(a) for a in argv)
    return run([HERDR, "pane", "run", pane_id, command]).ok


# ---------------------------------------------------------------- 投递

@dataclass(frozen=True)
class Outcome:
    """一次投递的结局。detail 是给人看的原因（已按 lang 取好），不含正文（正文已经在手机上，不进服务端第二次）。

    reason：delivered · no_lease · no_herdr · pane_missing · prompt_failed · prompt_timeout · wake_failed · error（注入层自己出错）
    """

    delivered: bool
    reason: str
    target: str | None
    cli: str | None
    detail: str
    lang: str  # 回执用的语言；必填，漏传在构造点就炸


def deliver(slot: str, pane: str | None, text: str, *, run: Runner, lang: str) -> Outcome:
    """把 text 注进 slot 的租约记的窗格 pane。同步、会阻塞在子进程上——调用方放工作线程里跑。lang 只管回执文案。"""
    if not pane:
        return Outcome(False, "no_lease", None, None, texts.t("receipt.no_lease", lang, slot=slot), lang)
    listing = run([HERDR, "pane", "list"])
    panes = parse_panes(listing.stdout) if listing.ok else None
    if panes is None:
        why = listing.summary(lang) if not listing.ok else texts.t("receipt.no_herdr.bad_output", lang)
        return Outcome(False, "no_herdr", pane, None, texts.t("receipt.no_herdr", lang, target=pane, why=why), lang)
    found = next((p for p in panes if p.get("pane_id") == pane), None)
    if found is None:  # 租约指向的窗格已经关掉了
        return Outcome(False, "pane_missing", pane, None, texts.t("receipt.pane_missing", lang, target=pane), lang)
    agent = found.get("agent")
    cli = agent if isinstance(agent, str) and agent else None
    if cli is None:
        LOG.info("目标 %s 没有检测到 agent 种类，按只 prompt 处理", pane)
    # TARGET 用 pane_id；正文只加来源前缀，原文本身一个字不改、不加 from:（这条真的就是用户发的）
    prompted = run([HERDR, "agent", "prompt", pane, REMOTE_PREFIX + text])
    if prompted.timed_out:
        return Outcome(False, "prompt_timeout", pane, cli, texts.t("receipt.prompt_timeout", lang, target=pane, why=prompted.summary(lang)), lang)
    if not prompted.ok:
        return Outcome(False, "prompt_failed", pane, cli, texts.t("receipt.prompt_failed", lang, target=pane, why=prompted.summary(lang)), lang)
    if cli == "kimi":
        woken = run([HERDR, "agent", "send-keys", pane, KIMI_WAKE_KEY])
        if not woken.ok:
            return Outcome(False, "wake_failed", pane, cli, texts.t("receipt.wake_failed", lang, target=pane, why=woken.summary(lang)), lang)
    return Outcome(True, "delivered", pane, cli, "", lang)


# ---------------------------------------------------------------- 控制标记

def control_mark(action: str, slot: str) -> str:
    if action not in MARK_ACTIONS:
        raise ValueError(f"没有这种控制动作：{action!r}")
    return f"__agent-ntfy:{action}:{slot}__"


def parse_control_mark(text: str) -> tuple[str, str] | None:
    """整段正文恰好是一个标记才算；多一个字符（含换行）都是普通消息。返回 (动作, 槽位)。"""
    m = MARK_RE.fullmatch(text)
    return (m.group(1), m.group(2)) if m else None


# ---------------------------------------------------------------- 可达性确认的测试消息

def render_confirm_request(slot: str, *, reply_url: str, lang: str) -> Rendered:
    """可达性确认闸的测试消息：验的是「通知弹出 → 点按钮 → 回传」整条链路，所以只有按钮点击算数。

    正文写给一个没看过任何文档的人：这条是什么、点按钮意味着什么、没弹通知怎么办。排查清单在 README 里，这里只指过去。
    """
    button = texts.t("confirm.button", lang)
    body = bold_first_line(texts.t("confirm.body", lang, slot=slot, button=button))
    return Rendered(title=f"[{slot}] {texts.t('confirm.title', lang)}", message=body,
                    actions=[http_action(button, reply_url, control_mark("confirmed", slot))], body=body, lang=lang)


# ---------------------------------------------------------------- 回执

def render_receipt(slot: str, outcome: Outcome, *, reply_url: str) -> Rendered:
    """投递失败回执：Title「[<slot>] 消息未送达」，正文说清为什么，按钮回同一 topic、body 是控制标记。语言随 outcome。

    没有租约时「释放」无意义，只带「忽略」；kimi 唤醒失败时消息其实已在队列里，同样只带「忽略」。
    「忽略」不能省：只给「释放」的话用户不想释放时这条回执永远悬着、按钮一直亮着。
    """
    if outcome.delivered:
        raise ValueError("已送达的投递没有回执")
    lang = outcome.lang
    if outcome.reason in ("prompt_timeout", "error"):
        tail = texts.t("receipt.uncertain", lang)
    elif outcome.reason == "wake_failed":
        tail = ""
    else:
        tail = texts.t("receipt.not_delivered", lang)
    head = bold_first_line(outcome.detail)
    body = f"{head}\n\n{tail}" if tail else head  # 尾句另起一段（Markdown 把单个换行折成空格）
    actions = []
    if outcome.reason not in ("no_lease", "wake_failed"):
        actions.append(http_action(texts.t("receipt.button.release", lang), reply_url, control_mark("release", slot)))
    actions.append(http_action(texts.t("receipt.button.ignore", lang), reply_url, control_mark("ignore", slot)))
    return Rendered(title=receipt_title(slot, lang, uncertain=outcome.reason in UNCERTAIN_REASONS), message=body, actions=actions, body=body, lang=lang)


def receipt_title(slot: str, lang: str, *, uncertain: bool = False) -> str:
    return f"[{slot}] {texts.t('receipt.title_uncertain' if uncertain else 'receipt.title', lang)}"


def render_stopping_receipt(slot: str, *, reply_url: str, lang: str, uncertain: bool = False) -> Rendered:
    """daemon 关停时还没投出去的消息：静默丢掉正是回执要防的形态，所以尽力发一张。只带「忽略」。

    uncertain：那条正卡在 herdr 子进程上、关停等不到结果——可能已送达，不能说死。
    """
    body = bold_first_line(texts.t("receipt.stopping_uncertain" if uncertain else "receipt.stopping", lang))
    return Rendered(title=receipt_title(slot, lang, uncertain=uncertain), message=body,
                    actions=[http_action(texts.t("receipt.button.ignore", lang), reply_url, control_mark("ignore", slot))], body=body, lang=lang)


def render_receipt_closed(receipt: Rendered, *, prefix: str, result: str) -> Rendered:
    """按钮被点过之后的回执：Title 加前缀、正文 = 加粗的结果句 + 分隔线 + 原回执正文、不带按钮。同 seq 更新后它就不再是活的。
    prefix / result 由调用方按 receipt.lang 取好传入。"""
    return Rendered(title=prefix + receipt.title, message=f"{bold_first_line(result)}\n\n{SEPARATOR}\n\n{receipt.body}",
                    actions=[], body=receipt.body, lang=receipt.lang)
