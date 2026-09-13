"""项目级状态文件：`<项目根>/.agent-ntfy/state.json`。

给 agent 在任何一个会话里读的：本项目远程模式开没开、当前租着哪个槽位、过没过闸。
它是**状态**不是配置——slot 随租约来去而变；开关由 `away on|off` 改；ask / confirm-sub / release 跑完顺手回写；
`away status` 经 daemon 的租约校对（`reconcile()`），文件与 daemon 不一致时以 daemon 为准。

三条边界：
- 只在目录已存在时回写（`note()`）：没启用过远程模式的项目不会被建目录。
- 目录自带 `.gitignore`（内容 `*`），git 看不到它，用户仓的 .gitignore 一个字不动。
- ⚠️ 文件里没有 topic 名，也不许写进去：topic 就是密码，agent 不需要它。
"""

import json
import os
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

DIR_NAME = ".agent-ntfy"
FILE_NAME = "state.json"
FIELDS = ("away", "slot", "confirmed", "target", "updated")


def project_root(cwd: Path | None = None) -> Path:
    """git 仓根；不在 git 仓里就是 cwd 本身。worktree / submodule 各是自己的根（各有各的状态文件）。

    cwd 已被删时 `Path.cwd()` 抛 OSError，原样抛给调用方：`note()` 会吞掉，`away` 要报给人。
    """
    cwd = (cwd or Path.cwd()).resolve()
    try:
        r = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return cwd
    if r.returncode == 0 and r.stdout.strip():
        return Path(r.stdout.strip()).resolve()
    return cwd


def state_dir(root: Path) -> Path:
    return root / DIR_NAME


def state_path(root: Path) -> Path:
    return state_dir(root) / FILE_NAME


def exists(root: Path) -> bool:
    return state_dir(root).is_dir()


def ensure(root: Path) -> Path:
    """建目录 + 自忽略的 .gitignore；幂等。"""
    d = state_dir(root)
    d.mkdir(mode=0o700, exist_ok=True)
    gi = d / ".gitignore"
    if not gi.exists():
        gi.write_text("*\n", encoding="utf-8")
    return d


def load(root: Path) -> dict:
    """读不到 / 不是合法 JSON 都当空：状态文件坏了不该让 ask 跑不起来。"""
    try:
        with open(state_path(root), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):  # ValueError 兜住 JSONDecodeError 与非法 UTF-8 的 UnicodeDecodeError
        return {}
    return data if isinstance(data, dict) else {}


def save(root: Path, **fields) -> dict:
    """合并写：只改给了的字段，其余保留；`updated` 每次刷新。临时文件 + rename，读的一方永远看不到半个文件。

    无锁：两个进程同时写，读-改-写窗口里后写的那个赢（字段可能被对方的旧值盖回）。
    一个项目同一时刻只有一个 ask 在回写，这是有意的取舍，不是要修的竞态。
    """
    ensure(root)
    data: dict[str, object] = {k: None for k in FIELDS if k != "updated"}
    data.update(load(root))
    data.update(fields)
    data["updated"] = datetime.now().astimezone().isoformat(timespec="seconds")
    d = state_dir(root)
    fd, tmp = tempfile.mkstemp(prefix=".state-", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.write("\n")
        os.replace(tmp, state_path(root))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return data


def reconcile(root: Path, leased_by: str, slots_view: dict) -> tuple[dict, bool]:
    """以 daemon 为准校对文件里的 slot / confirmed：在 slots 视图里按 leased_by 反查本项目真实租着哪个槽位。

    有 ⇒ 期望 slot = 那个、confirmed = 其 subscribed；没有 ⇒ 期望两者都是 None。与文件不一致才改写，
    返回 (校对后的状态, 是否改写过)。`away` 是人的开关，校对永远不碰它；两边都空时连文件都不建。
    """
    current = load(root)
    mine = next((slot for slot, rec in slots_view.items() if rec.get("leased_by") == leased_by), None)
    expected = {"slot": mine, "confirmed": bool(slots_view[mine].get("subscribed")) if mine else None}
    if all(current.get(k) == v for k, v in expected.items()):
        return current, False
    return save(root, **expected), True


def note(**fields) -> None:
    """命令跑完顺手回写——目录不在就什么都不做；任何异常都吞掉：这不是命令的主事，
    状态文件坏了、cwd 没了、目录不可写，都不能让一次已经发出去的 ask 变成失败。"""
    try:
        root = project_root()
        if not exists(root):
            return
        save(root, **fields)
    except Exception:
        pass


def note_confirmed(slot: str) -> None:
    """confirm-sub 过闸后回写——只在状态文件记的正是这个槽位时才写，别把一个本项目没租的槽位写成「当前租约」。"""
    try:
        root = project_root()
        if exists(root) and load(root).get("slot") == slot:
            save(root, confirmed=True)
    except Exception:
        pass


def note_released(slot: str, explicit: bool) -> None:
    """release 成功后回写——不带参数释放的是本目标自己的槽位，一定清；显式释放别的槽位时只在它就是本项目记的那个才清。"""
    try:
        root = project_root()
        if not exists(root):
            return
        if explicit and load(root).get("slot") != slot:
            return
        save(root, slot=None, confirmed=None)
    except Exception:
        pass
