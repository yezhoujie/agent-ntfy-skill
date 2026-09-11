"""状态层：topic 池（密钥存储）+ 槽位租约（~/.agent-ntfy/leases.json）。

两层分开存：
    密钥层  topic 名的池子（JSON 数组），存 macOS 钥匙串，几乎不变
    租约层  各槽位的订阅状态与当前占用者，存明文 JSON，频繁变

topic 名就是密码（公共 ntfy 实例知道名字即可读写、可对本机 agent 下指令），
所以租约文件里只出现槽位号（slot1、slot2……），绝不出现 topic 名。

槽位三态：
    未分配        没有租约
    已租用·空闲   有租约，当前没有提问挂着等回复
    已租用·活跃   有租约，且有提问正挂着等回复
「活跃」不落盘——它等价于 daemon 手上有没有那条等待中的连接，落盘反而会在进程崩溃后
留下假的活跃标记。调用方按需把活跃槽位集合传进来。

本模块预期只被 daemon 一个进程持有（其余子命令经 socket 向它查询），因此不做文件锁。

环境变量: AGENT_NTFY_KEYCHAIN / AGENT_NTFY_TOPIC_PREFIX / AGENT_NTFY_HOME
"""

import json
import os
import re
import secrets
import string
import subprocess
from abc import ABC, abstractmethod
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path

KEYCHAIN_SERVICE = os.environ.get("AGENT_NTFY_KEYCHAIN", "AGENT_NTFY_TOPICS")
KEYCHAIN_ACCOUNT = "agent-ntfy"  # 钥匙串条目的 -a，只是标识，不参与任何逻辑
DEFAULT_PREFIX = os.environ.get("AGENT_NTFY_TOPIC_PREFIX", "agent-ntfy")
HOME_DIR = Path(os.environ.get("AGENT_NTFY_HOME", "~/.agent-ntfy")).expanduser()
LEASES_PATH = HOME_DIR / "leases.json"
DEFAULT_POOL_SIZE = 5
# 小写字母 + 数字，20 位 ≈ 2^103 的熵；不用大写，免得用户在手机上抄 topic 名时分不清大小写
TOPIC_ALPHABET = string.ascii_lowercase + string.digits
TOPIC_RANDOM_LEN = 20
# ntfy 对 topic 名的限制：[-_A-Za-z0-9]{1,64}。前缀由用户自定义，写进钥匙串前先拦一次
TOPIC_PREFIX_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
SLOT_RE = re.compile(r"^slot([1-9][0-9]*)$")
# 服务名 / 账户名进 security 的参数，也进错误提示；只放行这些字符，免得提示里混进换行或引号
# 三个正则都用 fullmatch：match() 的 $ 会放过末尾换行
KEYCHAIN_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
# security 出错时会把它收到的参数回显到 stderr；密码载荷是一长串十六进制，进错误消息前先抹掉
HEX_RUN_RE = re.compile(r"[0-9a-fA-F]{32,}")


class StateError(Exception):
    """状态层的可预期错误：槽位不存在、状态不允许该操作、密钥存储读写失败。"""


class NeedsUserDecision(StateError):
    """全部槽位已租用，要用户在「替换某个空闲槽位」与「新建槽位」之间选。

    candidates 是可替换的槽位名（已租用·空闲），按槽位号排序；活跃槽位一律不在其中——
    替换它会让用户的回复落到别人的提问上。候选为空时只剩新建一条路。
    """

    def __init__(self, candidates):
        self.candidates = list(candidates)
        super().__init__(f"全部槽位已租用，可替换的空闲槽位：{self.candidates or '无'}")


class SlotState(Enum):
    UNASSIGNED = "未分配"
    IDLE = "已租用·空闲"
    ACTIVE = "已租用·活跃"


@dataclass(frozen=True)
class Lease:
    slot: str
    topic: str = field(repr=False)  # topic 名是密钥；repr 会被写进日志，不带它
    subscribed: bool  # 该槽位是否已过可达性确认闸；False 时调用方要先过闸再用


# ---------------------------------------------------------------- 密钥存储

class SecretStore(ABC):
    """topic 池的存取接口。钥匙串是 macOS 专有，其他平台补一个实现即可，其余代码不用动。"""

    @abstractmethod
    def load(self) -> list[str] | None:
        """返回池子；池子还不存在时返回 None。空列表与 None 同义，调用方都当作没有池子。"""

    @abstractmethod
    def save(self, topics: list[str]) -> None:
        """整体覆盖写入。"""


class KeychainStore(SecretStore):
    """macOS 钥匙串实现：一个 generic password 条目，密码值是 topic 名的 JSON 数组。

    读：security find-generic-password -a <账户> -s <服务名> -w
    写：security add-generic-password -U -a <账户> -s <服务名> -X <十六进制>，写完立刻回读比对
        · -U 让同一条目可以原地更新（加槽位时整个数组重写）
        · 载荷走 -X 十六进制，JSON 里的引号、逗号、空格都不用管转义
        · 载荷在 argv 里，执行的那几十毫秒内本机 ps 看得到（十六进制形态）；接受这一点，因为
          另一条路 `security -i` 从 stdin 读命令有 4096 字节的行缓冲：超长的一行会被切成多条
          命令执行，第一段以截断的载荷成功写入、把原有池子覆盖成垃圾，之后才报错——实测
          60 个 topic 就触发。与其在池子长到一定程度时静默毁掉密钥，不如让 ps 看几十毫秒
        · 回读比对是为了不依赖 security 的退出码语义：写入「成功」而内容不对，内存里的池子
          与钥匙串就分叉了，重启后凭空少槽位
    find-generic-password 找不到条目时退出码 44，据此区分「池子不存在」与「命令本身跑挂了」——
    后者绝不能当成不存在，否则会用 -U 把用户现有的池子整个换掉。
    """

    NOT_FOUND_RC = 44

    def __init__(self, service=KEYCHAIN_SERVICE, account=KEYCHAIN_ACCOUNT):
        for label, value in (("服务名", service), ("账户名", account)):
            if not KEYCHAIN_NAME_RE.fullmatch(value or ""):
                raise StateError(f"钥匙串{label}不合法：{value!r}（只能用字母、数字、. _ -）")
        self.service = service
        self.account = account

    def _run(self, argv, **kw):
        return subprocess.run(argv, capture_output=True, text=True, **kw)

    def _fail(self, what, r):
        detail = HEX_RUN_RE.sub("<hex>", r.stderr.strip().splitlines()[0] if r.stderr.strip() else "")
        raise StateError(f"{what}钥匙串条目 '{self.service}' 失败（rc={r.returncode}）：{detail}")

    def load(self):
        r = self._run(["security", "find-generic-password", "-a", self.account, "-s", self.service, "-w"])
        if r.returncode == self.NOT_FOUND_RC:
            return None
        if r.returncode != 0:
            self._fail("读", r)
        try:
            topics = json.loads(r.stdout.strip())
        except json.JSONDecodeError as e:
            raise StateError(f"钥匙串条目 '{self.service}' 里的内容不是 JSON 数组：{e}") from e
        if not isinstance(topics, list) or not all(isinstance(t, str) for t in topics):
            raise StateError(f"钥匙串条目 '{self.service}' 里的内容不是字符串数组")
        return topics

    def save(self, topics):
        topics = list(topics)
        payload = json.dumps(topics).encode("utf-8").hex()
        r = self._run(["security", "add-generic-password", "-U", "-a", self.account, "-s", self.service, "-X", payload])
        if r.returncode != 0:
            self._fail("写", r)
        if self.load() != topics:
            raise StateError(f"钥匙串条目 '{self.service}' 写入后回读与写入内容不一致，池子未更新")


# ---------------------------------------------------------------- 状态

def _slot_index(slot):
    """'slot3' -> 3；形态不对抛 StateError。"""
    m = SLOT_RE.fullmatch(slot or "")
    if not m:
        raise StateError(f"槽位名不合法：{slot!r}（应形如 slot1）")
    return int(m.group(1))


def _empty_record():
    return {"subscribed": False, "leased_by": None, "leased_at": None}


def _now():
    # 带本地时区偏移的 ISO 8601，秒级
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _require(leases, slot):
    if slot not in leases:
        _slot_index(slot)  # 形态不对先报形态
        raise StateError(f"槽位 {slot} 不存在（池子里只有 {len(leases)} 个）")


def _state_of(rec, is_active):
    if not rec["leased_by"]:
        return SlotState.UNASSIGNED
    return SlotState.ACTIVE if is_active else SlotState.IDLE


def _check_active(leases, active):
    """活跃 = 有提问挂着等回复，前提是有租约；活跃集合里出现未分配或不存在的槽位，说明调用方的状态和文件已经对不上。"""
    for slot in active:
        if slot not in leases or not leases[slot]["leased_by"]:
            raise StateError(f"活跃槽位 {slot} 在租约文件里不是已租用状态，状态不一致，拒绝操作")


class State:
    """topic 池 + 租约的唯一入口。

    池子惰性创建：第一次用到时密钥存储里没有就当场生成 pool_size 个 topic 写进去，
    使用者不需要跑 init。生成时不做订阅确认——那时用户还没订阅任何 topic。
    """

    def __init__(self, store: SecretStore, leases_path: Path = LEASES_PATH, *,
                 prefix: str = DEFAULT_PREFIX, pool_size: int = DEFAULT_POOL_SIZE):
        if not TOPIC_PREFIX_RE.fullmatch(prefix):
            raise StateError(f"topic 前缀不合法：{prefix!r}（只能用字母、数字、- 和 _，最长 40 位）")
        if pool_size < 1:
            raise StateError(f"池子至少要有 1 个槽位，给的是 {pool_size}")
        self.store = store
        self.leases_path = Path(leases_path)
        self.prefix = prefix
        self.pool_size = pool_size
        self._topics: list[str] | None = None

    # -------- topic 池

    def topics(self):
        if self._topics is None:
            topics = self.store.load()
            if not topics:
                topics = [self._new_topic() for _ in range(self.pool_size)]
                self.store.save(topics)
                self._topics = topics  # 先记下，下面 slot_names() 要读它
                # 新池子里的 topic 谁都没订阅过，旧租约文件（比如钥匙串条目被删掉重建后留下的）
                # 里的订阅位与占用者全部作废；沿用它会让「已订阅」贴到一个手机上没有的 topic 上
                self._save_leases({slot: _empty_record() for slot in self.slot_names()})
            self._topics = topics
        return list(self._topics)

    def _new_topic(self):
        rand = "".join(secrets.choice(TOPIC_ALPHABET) for _ in range(TOPIC_RANDOM_LEN))
        return f"{self.prefix}-{rand}"

    def slot_names(self):
        return [f"slot{i}" for i in range(1, len(self.topics()) + 1)]

    def topic_of(self, slot):
        topics = self.topics()
        i = _slot_index(slot)
        if i > len(topics):
            raise StateError(f"槽位 {slot} 不存在（池子里只有 {len(topics)} 个）")
        return topics[i - 1]

    def add_slot(self):
        """新建一个槽位：生成新 topic 追加进池子。不设上限。返回新槽位名。"""
        topics = self.topics() + [self._new_topic()]
        self.store.save(topics)
        self._topics = topics
        self._save_leases(self._load_leases())  # 让租约文件里立刻出现新槽位的记录
        return f"slot{len(topics)}"

    # -------- 租约文件

    def _load_leases(self):
        """读租约文件，补齐池子里每个槽位的记录；文件不存在视为全部未分配。"""
        data = {}
        if self.leases_path.exists():
            try:
                data = json.loads(self.leases_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                raise StateError(f"读租约文件 {self.leases_path} 失败：{e}") from e
            if not isinstance(data, dict):
                raise StateError(f"租约文件 {self.leases_path} 不是 JSON 对象")
        leases = {}
        for slot in self.slot_names():
            rec = _empty_record()
            got = data.get(slot)
            if isinstance(got, dict):
                rec["subscribed"] = got.get("subscribed") is True  # 字符串 "false" 之类脏值一律不算已订阅
                rec["leased_by"] = got.get("leased_by") or None
                rec["leased_at"] = got.get("leased_at") if rec["leased_by"] else None
            leases[slot] = rec
        return leases

    def _save_leases(self, leases):
        """先写临时文件再 rename，避免写到一半崩溃留下半截 JSON。文件 0600：里面是谁在用哪个槽位。"""
        self.leases_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = self.leases_path.with_name(self.leases_path.name + ".tmp")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(leases, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
            os.replace(tmp, self.leases_path)
        except OSError as e:
            raise StateError(f"写租约文件 {self.leases_path} 失败：{e}") from e

    # -------- 查询

    def slot_state(self, slot, active: Collection[str] = ()):
        """active 是调用方此刻有提问挂着等回复的槽位集合。"""
        leases = self._load_leases()
        _require(leases, slot)
        return _state_of(leases[slot], slot in active)

    def slots(self, active: Collection[str] = ()):
        """全部槽位的视图（给 slots 子命令用）：状态 + 订阅 + 占用者。不含 topic 名。"""
        return {
            slot: {"state": _state_of(rec, slot in active).value, **rec}
            for slot, rec in self._load_leases().items()
        }

    # -------- 分配 / 替换 / 新建 / 释放

    def acquire(self, leased_by: str, active: Collection[str] = ()):
        """租一个槽位。

        有「未分配」槽位就直接用（已过闸的优先，省一次手机确认；其余按槽位号）。
        全部已租用则抛 NeedsUserDecision，带上可替换的空闲槽位清单，由调用方去问用户。
        """
        leases = self._load_leases()
        _check_active(leases, active)
        free = [s for s, r in leases.items() if not r["leased_by"]]
        if free:
            slot = min(free, key=lambda s: (not leases[s]["subscribed"], _slot_index(s)))
            return self._grant(leases, slot, leased_by)
        raise NeedsUserDecision(s for s in leases if s not in active)

    def replace(self, slot: str, leased_by: str, active: Collection[str] = ()):
        """把一个「已租用·空闲」槽位转给新的使用者。活跃槽位拒绝——它上面正有人等回复。"""
        leases = self._load_leases()
        _require(leases, slot)
        _check_active(leases, active)
        st = _state_of(leases[slot], slot in active)
        if st is SlotState.ACTIVE:
            raise StateError(f"槽位 {slot} 正有提问等回复，不能替换")
        if st is SlotState.UNASSIGNED:
            raise StateError(f"槽位 {slot} 没有租约，直接 acquire 即可，不需要替换")
        return self._grant(leases, slot, leased_by)

    def _grant(self, leases, slot, leased_by):
        if not leased_by:
            raise StateError("leased_by 不能为空（要写清楚谁在用这个槽位）")
        leases[slot]["leased_by"] = leased_by
        leases[slot]["leased_at"] = _now()
        self._save_leases(leases)
        return Lease(slot=slot, topic=self.topic_of(slot), subscribed=leases[slot]["subscribed"])

    def release(self, slot: str, active: Collection[str] = ()):
        """释放租约，槽位回到「未分配」。订阅状态保留——可达性闸只需过一次。

        活跃槽位拒绝释放：上面正有提问等回复，释放后它会被租给别人，用户的回复就落到别人的提问上。
        要释放得先让那个提问结束。
        """
        leases = self._load_leases()
        _require(leases, slot)
        _check_active(leases, active)
        if slot in active:
            raise StateError(f"槽位 {slot} 正有提问等回复，不能释放")
        if not leases[slot]["leased_by"]:
            raise StateError(f"槽位 {slot} 没有租约，无需释放")
        leases[slot]["leased_by"] = None
        leases[slot]["leased_at"] = None
        self._save_leases(leases)

    def mark_subscribed(self, slot: str):
        """可达性确认闸通过后调用；之后该槽位不再要求确认。"""
        leases = self._load_leases()
        _require(leases, slot)
        leases[slot]["subscribed"] = True
        self._save_leases(leases)
