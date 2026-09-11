"""注入层：把用户从手机发来的话原样送进目标 agent 的窗格；送不到时说清为什么。

    手机 → topic → daemon（无 pending）→ deliver() ──herdr agent prompt <pane_id> '<原文>'──▶ 目标 agent
                                              └─ 送不到 → 回执（[<slot>] 消息未送达 + 控制按钮）→ 手机

只做通路：正文一个字不改、不加前缀、不判断目标忙不忙（AI CLI 自己会排队）。要判断的只有「目标在不在」——
租约是懒释放的，租约看着有效而它指向的 pane 早已消失是常态，不是边缘情况。

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
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

from ntfyclient import http_action
from render import SEPARATOR, Rendered

HERDR = "herdr"
# 三条命令都是本机 unix socket IPC，实测毫秒级返回；prompt 不带 --wait，提交即返回、不追踪回合。
# 15 秒够熬过机器卡顿，又不至于让工作线程被一条投递挂死。
HERDR_TIMEOUT = 15.0
RC_NOT_FOUND = 127
RC_TIMEOUT = 124
KIMI_WAKE_KEY = "ctrl+s"

RECEIPT_TITLE = "消息未送达"
RECEIPT_TITLE_UNCERTAIN = "消息可能未送达"  # 通知栏只看得到 Title：其实可能已送达的情形（超时 / 唤醒失败 / 出错）不能写死「未送达」
UNCERTAIN_REASONS = ("prompt_timeout", "wake_failed", "error")
STOPPING_LINE = "daemon 正在停止，你刚才的消息未送达，请稍后再发。"
STOPPING_UNCERTAIN_LINE = "daemon 正在停止，无法确认你刚才的消息是否送达，请稍后再发。"
RELEASE_LABEL = "释放这个槽位"
IGNORE_LABEL = "忽略"
NOT_DELIVERED_LINE = "你刚才发的内容没有送达任何 agent。"
UNCERTAIN_LINE = "无法确认你刚才发的内容有没有送达。"
RELEASED_PREFIX = "✅ 已释放 · "
NOT_RELEASED_PREFIX = "未释放 · "
IGNORED_PREFIX = "已忽略 · "
SUPERSEDED_PREFIX = "已被新回执取代 · "  # 同一槽位又来一张回执时，旧的那张按此关闭，按钮不悬着

MARK_ACTIONS = ("release", "ignore", "confirmed")  # 回执的两个按钮 + 可达性确认的「我收到了」
# 只认这一个精确形态（fullmatch），且槽位名要与消息所在的槽位相同——由调用方核；不匹配就是普通消息。
# 这是「通路不解释内容」的唯一例外：认自己生成的固定标记可以，解析用户自由输入的文本不行。
MARK_RE = re.compile(r"__agent-ntfy:(release|ignore|confirmed):(slot[1-9][0-9]*)__")

CONFIRM_TITLE = "确认你能收到通知"  # Title = f"[{slot}] {CONFIRM_TITLE}"
CONFIRM_LABEL = "我收到了"
CONFIRMED_PREFIX = "✅ 已确认 · "

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

    def summary(self) -> str:
        """给回执与日志用的一句话。只有 code / 退出码 / 超时，不带 stderr 原文（它可能回显正文）。"""
        if self.timed_out:
            return f"herdr {self.timeout:g} 秒无响应"
        if self.rc == RC_NOT_FOUND:
            return "herdr 没装或不在 PATH 上"
        code = self.error_code()
        return code if code else f"退出码 {self.rc}"


Runner = Callable[[list[str]], HerdrResult]


def run_herdr(argv: list[str], *, timeout: float = HERDR_TIMEOUT) -> HerdrResult:
    """跑一条 herdr 命令，不抛：命令不存在 → rc 127；超时 → rc 124 且 timed_out。正文只进 argv，不经 shell。

    stdin 接 /dev/null：daemon 前台跑时 stdin 是终端，子进程若继承它、又碰巧等输入，就会挂满整个超时。
    """
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


# ---------------------------------------------------------------- 投递

@dataclass(frozen=True)
class Outcome:
    """一次投递的结局。detail 是给人看的原因，不含正文（正文已经在手机上，不进服务端第二次）。

    reason：delivered · no_lease · no_herdr · pane_missing · prompt_failed · prompt_timeout · wake_failed · error（注入层自己出错）
    """

    delivered: bool
    reason: str
    target: str | None
    cli: str | None
    detail: str


def deliver(slot: str, leased_by: str | None, text: str, *, run: Runner = run_herdr) -> Outcome:
    """把 text 注进 slot 的租约指向的 pane。同步、会阻塞在子进程上——调用方放工作线程里跑。"""
    if not leased_by:
        return Outcome(False, "no_lease", None, None, f"槽位 {slot} 目前没有绑定任何 agent（没有租约）。")
    listing = run([HERDR, "pane", "list"])
    panes = parse_panes(listing.stdout) if listing.ok else None
    if panes is None:
        why = listing.summary() if not listing.ok else "pane list 的输出不是预期形态"
        return Outcome(False, "no_herdr", leased_by, None, f"这个槽位绑定的目标 {leased_by} 不在 herdr 里（{why}），无法注入。")
    pane = next((p for p in panes if p.get("pane_id") == leased_by), None)
    if pane is None:  # 租约指向的窗格关掉了，或这个目标本来就不是 herdr 窗格（非 herdr 身份 host:…|sid:…）——两种都送不到
        return Outcome(False, "pane_missing", leased_by, None, f"这个槽位绑定的目标 {leased_by} 不在 herdr 的窗格列表里。")
    agent = pane.get("agent")
    cli = agent if isinstance(agent, str) and agent else None
    if cli is None:
        LOG.info("目标 %s 没有检测到 agent 种类，按只 prompt 处理", leased_by)
    prompted = run([HERDR, "agent", "prompt", leased_by, text])  # TARGET 用 pane_id；正文原样、不加 from:（这条真的就是用户发的）
    if prompted.timed_out:
        return Outcome(False, "prompt_timeout", leased_by, cli, f"向目标 {leased_by} 注入时 {prompted.summary()}。")
    if not prompted.ok:
        return Outcome(False, "prompt_failed", leased_by, cli, f"向目标 {leased_by} 注入失败：herdr 返回 {prompted.summary()}。")
    if cli == "kimi":
        woken = run([HERDR, "agent", "send-keys", leased_by, KIMI_WAKE_KEY])
        if not woken.ok:
            return Outcome(False, "wake_failed", leased_by, cli,
                           f"消息已进入目标 {leased_by}（kimi）的队列，但唤醒它失败（herdr 返回 {woken.summary()}）；"
                           "它忙着的时候可能不会读到这条，请再发一次。")
    return Outcome(True, "delivered", leased_by, cli, "")


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

def render_confirm_request(slot: str, *, reply_url: str) -> Rendered:
    """可达性确认闸的测试消息：验的是「通知弹出 → 点按钮 → 回传」整条链路，所以只有按钮点击算数。

    正文写给一个没看过任何文档的人：这条是什么、点按钮意味着什么、没弹通知怎么办。排查清单在 README 里，这里只指过去。
    """
    body = (
        f"这是 agent-ntfy 发来的测试消息，用来确认这台手机能收到槽位 {slot} 的通知。\n"
        "\n"
        f"请在通知栏里点下面的「{CONFIRM_LABEL}」按钮——点了就算确认完成，之后 agent 才会往这个槽位发提问。\n"
        "\n"
        "如果这条在 ntfy app 里看得见、但通知栏没有弹出来，说明手机的通知权限还没配好：\n"
        "先按 README 的排查清单逐项检查（通知权限、省电策略、自启动、锁屏通知、这个 topic 没被静音），\n"
        "让它弹出来之后再点按钮。只在 app 里点按钮证明不了通知会弹。"
    )
    return Rendered(title=f"[{slot}] {CONFIRM_TITLE}", message=body,
                    actions=[http_action(CONFIRM_LABEL, reply_url, control_mark("confirmed", slot))], body=body)


# ---------------------------------------------------------------- 回执

def render_receipt(slot: str, outcome: Outcome, *, reply_url: str) -> Rendered:
    """投递失败回执：Title「[<slot>] 消息未送达」，正文说清为什么，按钮回同一 topic、body 是控制标记。

    没有租约时「释放」无意义，只带「忽略」；kimi 唤醒失败时消息其实已在队列里，同样只带「忽略」。
    「忽略」不能省：只给「释放」的话用户不想释放时这条回执永远悬着、按钮一直亮着。
    """
    if outcome.delivered:
        raise ValueError("已送达的投递没有回执")
    if outcome.reason in ("prompt_timeout", "error"):
        tail = UNCERTAIN_LINE
    elif outcome.reason == "wake_failed":
        tail = ""
    else:
        tail = NOT_DELIVERED_LINE
    body = outcome.detail if not tail else f"{outcome.detail}\n{tail}"
    actions = []
    if outcome.reason not in ("no_lease", "wake_failed"):
        actions.append(http_action(RELEASE_LABEL, reply_url, control_mark("release", slot)))
    actions.append(http_action(IGNORE_LABEL, reply_url, control_mark("ignore", slot)))
    return Rendered(title=receipt_title(slot, uncertain=outcome.reason in UNCERTAIN_REASONS), message=body, actions=actions, body=body)


def receipt_title(slot: str, *, uncertain: bool = False) -> str:
    return f"[{slot}] {RECEIPT_TITLE_UNCERTAIN if uncertain else RECEIPT_TITLE}"


def render_stopping_receipt(slot: str, *, reply_url: str, uncertain: bool = False) -> Rendered:
    """daemon 关停时还没投出去的消息：静默丢掉正是回执要防的形态，所以尽力发一张。只带「忽略」。

    uncertain：那条正卡在 herdr 子进程上、关停等不到结果——可能已送达，不能说死。
    """
    body = STOPPING_UNCERTAIN_LINE if uncertain else STOPPING_LINE
    return Rendered(title=receipt_title(slot, uncertain=uncertain), message=body,
                    actions=[http_action(IGNORE_LABEL, reply_url, control_mark("ignore", slot))], body=body)


def render_receipt_closed(receipt: Rendered, *, prefix: str, result: str) -> Rendered:
    """按钮被点过之后的回执：Title 加前缀、正文 = 结果 + 分隔线 + 原回执正文、不带按钮。同 seq 更新后它就不再是活的。"""
    return Rendered(title=prefix + receipt.title, message=f"{result}\n{SEPARATOR}\n{receipt.body}", actions=[], body=receipt.body)
