"""状态层：topic 池（密钥存储）与槽位租约。

密钥存储用内存实现替身；钥匙串（security 命令）是外部程序，只在 AGENT_NTFY_SMOKE=1 时用临时条目真跑一遍。
"""

import json
import os
import secrets
import subprocess
import tempfile
import unittest
from pathlib import Path

import state
from state import Lease, NeedsUserDecision, SecretStore, SlotState, State


class MemoryStore(SecretStore):
    """内存版密钥存储：load() 在首次 save() 之前返回 None，模拟钥匙串里还没有池子。"""

    def __init__(self):
        self.topics = None
        self.saves = 0

    def load(self):
        return None if self.topics is None else list(self.topics)

    def save(self, topics):
        self.topics = list(topics)
        self.saves += 1


class StateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.leases_path = Path(self.tmp.name) / "leases.json"
        self.store = MemoryStore()
        self.state = State(self.store, self.leases_path)

    def lease_all(self, active=()):
        """把默认 5 个槽位全部租出去，返回租到的槽位名列表。"""
        return [self.state.acquire(f"user-{i}", active=active).slot for i in range(5)]

    # 钥匙串池不存在时惰性创建，默认 5 个槽位
    def test_pool_created_lazily_with_five_slots(self):
        self.assertIsNone(self.store.topics)
        topics = self.state.topics()
        self.assertEqual(len(topics), 5)
        self.assertEqual(self.store.topics, topics)  # 已写回密钥存储
        self.assertEqual(self.state.slot_names(), ["slot1", "slot2", "slot3", "slot4", "slot5"])
        # 再读一次不重建
        self.assertEqual(self.state.topics(), topics)
        self.assertEqual(self.store.saves, 1)

    # topic 命名符合 <前缀>-<20 位随机串>，前缀可配置
    def test_topic_naming_with_configurable_prefix(self):
        for t in self.state.topics():
            self.assertRegex(t, r"^agent-ntfy-[a-z0-9]{20}$")
        custom = State(MemoryStore(), Path(self.tmp.name) / "other.json", prefix="team-x")
        for t in custom.topics():
            self.assertRegex(t, r"^team-x-[a-z0-9]{20}$")
        self.assertEqual(len(set(self.state.topics())), 5)  # 互不相同
        with self.assertRaises(state.StateError):
            State(MemoryStore(), Path(self.tmp.name) / "bad.json", prefix="has space")
        with self.assertRaises(state.StateError):
            State(MemoryStore(), Path(self.tmp.name) / "bad.json", pool_size=0)

    # 租约三态判定正确
    def test_three_slot_states(self):
        self.assertEqual(self.state.slot_state("slot1"), SlotState.UNASSIGNED)
        lease = self.state.acquire("wD:p1")
        self.assertEqual(self.state.slot_state(lease.slot), SlotState.IDLE)
        self.assertEqual(self.state.slot_state(lease.slot, active={lease.slot}), SlotState.ACTIVE)
        # 别的槽位活跃不影响本槽位
        self.assertEqual(self.state.slot_state(lease.slot, active={"slot5"}), SlotState.IDLE)

    # 有「未分配」槽位时直接返回，不触发用户选择
    def test_acquire_uses_unassigned_slot_without_asking(self):
        lease = self.state.acquire("wD:p1")
        self.assertIsInstance(lease, Lease)
        self.assertEqual(lease.slot, "slot1")
        self.assertEqual(lease.topic, self.state.topics()[0])
        self.assertFalse(lease.subscribed)
        # 第二次拿到的是另一个未分配槽位
        self.assertEqual(self.state.acquire("wD:p2").slot, "slot2")

    # 全部已租用时，返回「需用户决定」及可替换候选清单
    def test_all_leased_raises_needs_decision_with_candidates(self):
        slots = self.lease_all()
        with self.assertRaises(NeedsUserDecision) as cm:
            self.state.acquire("wD:p9")
        self.assertEqual(cm.exception.candidates, slots)

    # 可替换候选中不含「活跃」槽位
    def test_candidates_exclude_active_slots(self):
        slots = self.lease_all()
        active = {slots[1], slots[3]}
        with self.assertRaises(NeedsUserDecision) as cm:
            self.state.acquire("wD:p9", active=active)
        self.assertEqual(cm.exception.candidates, [s for s in slots if s not in active])

    # 全部槽位都是「活跃」时，候选为空，只剩「新建」一条路
    def test_all_active_leaves_no_candidates(self):
        slots = self.lease_all()
        with self.assertRaises(NeedsUserDecision) as cm:
            self.state.acquire("wD:p9", active=set(slots))
        self.assertEqual(cm.exception.candidates, [])

    # 新建槽位后池子长度 +1，且不设上限
    def test_add_slot_grows_pool_without_limit(self):
        self.assertEqual(len(self.state.topics()), 5)
        for n in range(6, 26):
            slot = self.state.add_slot()
            self.assertEqual(slot, f"slot{n}")
            self.assertEqual(len(self.state.topics()), n)
            self.assertEqual(self.store.topics, self.state.topics())  # 已写回密钥存储
        self.assertRegex(self.state.topic_of("slot25"), r"^agent-ntfy-[a-z0-9]{20}$")
        self.assertEqual(self.state.slot_state("slot25"), SlotState.UNASSIGNED)
        # 全满后新建的槽位可以直接租到
        self.lease_all()
        self.assertEqual(self.state.acquire("wD:p9").slot, "slot6")

    # release 后该槽位回到「未分配」
    def test_release_returns_slot_to_unassigned(self):
        slots = self.lease_all()
        self.state.release(slots[2])
        self.assertEqual(self.state.slot_state(slots[2]), SlotState.UNASSIGNED)
        self.assertEqual(self.state.acquire("wD:p9").slot, slots[2])
        # 释放不存在 / 未租用的槽位要响亮失败，而不是静默通过
        with self.assertRaises(state.StateError):
            self.state.release("slot99")
        self.state.release(slots[2])  # 正常释放
        with self.assertRaises(state.StateError):
            self.state.release(slots[2])  # 已经是未分配，再释放一次

    # 活跃槽位上正有人等回复：不能释放，也不能替换；活跃集合与租约对不上要响亮失败
    def test_active_slot_cannot_be_released_or_replaced(self):
        slots = self.lease_all()
        with self.assertRaises(state.StateError):
            self.state.release(slots[0], active={slots[0]})
        self.assertEqual(self.state.slot_state(slots[0]), SlotState.IDLE)  # 没被动过
        with self.assertRaises(state.StateError):
            self.state.replace(slots[0], "wD:p9", active={slots[0]})
        self.state.release(slots[1])
        with self.assertRaises(state.StateError):  # 未分配的槽位不可能活跃
            self.state.acquire("wD:p9", active={slots[1]})
        with self.assertRaises(state.StateError):
            self.state.release(slots[0], active={"slot99"})

    # leases.json 里不出现任何 topic 名
    def test_leases_file_never_contains_topic_names(self):
        self.state.acquire("wD:p1")
        self.state.mark_subscribed("slot1")
        self.state.acquire("wD:p2")
        self.state.add_slot()
        text = self.leases_path.read_text(encoding="utf-8")
        for topic in self.state.topics():
            self.assertNotIn(topic, text)
            self.assertNotIn(topic[-20:], text)  # 随机串本身也不能出现

    # 落盘格式：{"slotN": {"subscribed": bool, "leased_by": str|null, "leased_at": str|null}}
    def test_leases_file_format(self):
        self.state.acquire("wD:p1")
        self.state.mark_subscribed("slot1")
        data = json.loads(self.leases_path.read_text(encoding="utf-8"))
        self.assertEqual(sorted(data), ["slot1", "slot2", "slot3", "slot4", "slot5"])
        self.assertEqual(set(data["slot1"]), {"subscribed", "leased_by", "leased_at"})
        self.assertIs(data["slot1"]["subscribed"], True)
        self.assertEqual(data["slot1"]["leased_by"], "wD:p1")
        self.assertRegex(data["slot1"]["leased_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")
        self.assertEqual(data["slot2"], {"subscribed": False, "leased_by": None, "leased_at": None})
        self.assertEqual(oct(self.leases_path.stat().st_mode & 0o777), oct(0o600))

    # 订阅状态跨租约保留：释放再租，不用重新过可达性闸
    def test_subscribed_survives_release(self):
        lease = self.state.acquire("wD:p1")
        self.state.mark_subscribed(lease.slot)
        self.state.release(lease.slot)
        self.assertTrue(self.state.acquire("wD:p2").subscribed)

    # 替换：只能替换「已租用·空闲」的槽位
    def test_replace_idle_slot(self):
        slots = self.lease_all()
        lease = self.state.replace(slots[0], "wD:p9")
        self.assertEqual(lease.slot, slots[0])
        data = json.loads(self.leases_path.read_text(encoding="utf-8"))
        self.assertEqual(data[slots[0]]["leased_by"], "wD:p9")
        with self.assertRaises(state.StateError):
            self.state.replace("slot99", "wD:p9")
        self.state.release(slots[1])
        with self.assertRaises(state.StateError):  # 未分配的直接 acquire，不走替换
            self.state.replace(slots[1], "wD:p9")

    # 状态在进程重启后可以从文件与密钥存储恢复
    def test_state_reloads_from_disk(self):
        lease = self.state.acquire("wD:p1")
        self.state.mark_subscribed(lease.slot)
        again = State(self.store, self.leases_path)
        self.assertEqual(again.topics(), self.state.topics())
        self.assertEqual(again.slot_state(lease.slot), SlotState.IDLE)
        view = again.slots()[lease.slot]
        self.assertEqual(view, {"state": "已租用·空闲", "subscribed": True,
                                "leased_by": "wD:p1", "leased_at": view["leased_at"]})

    # 池子被重建（钥匙串条目被删后再起）时，旧租约文件作废：新 topic 谁都没订阅过
    def test_pool_recreation_resets_stale_leases(self):
        lease = self.state.acquire("wD:p1")
        self.state.mark_subscribed(lease.slot)
        fresh = State(MemoryStore(), self.leases_path)  # 密钥存储里没有池子，租约文件却还在
        fresh.topics()
        self.assertEqual(fresh.slot_state("slot1"), SlotState.UNASSIGNED)
        self.assertFalse(fresh.acquire("wD:p2").subscribed)
        # 密钥存储里是空数组也当没有池子
        empty = MemoryStore()
        empty.topics = []
        self.assertEqual(len(State(empty, Path(self.tmp.name) / "e.json").topics()), 5)

    # 租约文件里的脏值：subscribed 不是布尔 true 就不算已订阅（"false" 字符串不能被读成真）
    def test_non_boolean_subscribed_is_not_subscribed(self):
        self.state.topics()
        self.leases_path.write_text(json.dumps({"slot1": {"subscribed": "false", "leased_by": None}}), encoding="utf-8")
        self.assertFalse(self.state.acquire("wD:p1").subscribed)

    # 租约对象打印出来不能带 topic（它会被写进日志）
    def test_lease_repr_hides_topic(self):
        lease = self.state.acquire("wD:p1")
        self.assertNotIn(lease.topic, repr(lease))
        self.assertIn("slot1", repr(lease))


class FakeRun:
    """替身 security：按 argv[1]（子命令）返回预设结果，并记录每次调用。"""

    def __init__(self, **by_subcommand):
        self.by_subcommand = by_subcommand
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        rc, out, err = self.by_subcommand[argv[1]]
        return subprocess.CompletedProcess(argv, rc, out, err)


class KeychainStoreTest(unittest.TestCase):
    """只检查本模块自己的逻辑：命令怎么拼、返回码怎么判、回读怎么比。security 本身不在这里跑。"""

    SVC = "AGENT_NTFY_TEST"
    TOPICS = ["agent-ntfy-abcdefghijklmnopqrst", "agent-ntfy-0123456789abcdefghij"]

    def store(self, run):
        s = state.KeychainStore(service=self.SVC)
        s._run = run
        return s

    def test_service_name_with_shell_separators_is_rejected(self):
        for bad in ("has space", 'quo"te', "", "a\nb"):
            with self.assertRaises(state.StateError, msg=repr(bad)):
                state.KeychainStore(service=bad)

    def test_load_returns_none_only_when_entry_missing(self):
        self.assertIsNone(self.store(FakeRun(**{"find-generic-password": (44, "", "not found")})).load())
        with self.assertRaises(state.StateError):  # 别的失败不能被当成「池子不存在」而触发重建
            self.store(FakeRun(**{"find-generic-password": (1, "", "keychain locked")})).load()

    def test_load_rejects_malformed_entry(self):
        for bad in ("not json", '{"a": 1}', '["ok", 2]'):
            with self.assertRaises(state.StateError, msg=bad):
                self.store(FakeRun(**{"find-generic-password": (0, bad + "\n", "")})).load()

    def test_load_parses_entry_and_queries_by_account_and_service(self):
        run = FakeRun(**{"find-generic-password": (0, json.dumps(self.TOPICS) + "\n", "")})
        self.assertEqual(self.store(run).load(), self.TOPICS)
        self.assertEqual(run.calls[0], ["security", "find-generic-password", "-a", "agent-ntfy", "-s", self.SVC, "-w"])

    def test_save_writes_hex_payload_then_reads_back(self):
        run = FakeRun(**{"add-generic-password": (0, "", ""),
                         "find-generic-password": (0, json.dumps(self.TOPICS) + "\n", "")})
        self.store(run).save(self.TOPICS)
        add, readback = run.calls
        self.assertEqual(add[:8], ["security", "add-generic-password", "-U", "-a", "agent-ntfy", "-s", self.SVC, "-X"])
        self.assertEqual(json.loads(bytes.fromhex(add[8]).decode("utf-8")), self.TOPICS)
        self.assertEqual(readback[1], "find-generic-password")

    def test_save_fails_loudly_when_write_or_readback_is_off(self):
        with self.assertRaises(state.StateError):
            self.store(FakeRun(**{"add-generic-password": (1, "", "boom")})).save(self.TOPICS)
        # 写入返回 0 但回读的不是刚写的内容：也要报错，不能让内存里的池子和钥匙串分叉
        run = FakeRun(**{"add-generic-password": (0, "", ""),
                         "find-generic-password": (0, json.dumps(self.TOPICS[:1]) + "\n", "")})
        with self.assertRaises(state.StateError):
            self.store(run).save(self.TOPICS)

    def test_error_messages_do_not_leak_payload(self):
        payload = json.dumps(self.TOPICS).encode().hex()
        run = FakeRun(**{"add-generic-password": (1, "", f'security: unknown command "{payload}"')})
        with self.assertRaises(state.StateError) as cm:
            self.store(run).save(self.TOPICS)
        self.assertNotIn(payload, str(cm.exception))
        self.assertNotIn(self.TOPICS[0], str(cm.exception))


@unittest.skipUnless(os.environ.get("AGENT_NTFY_SMOKE") == "1", "设 AGENT_NTFY_SMOKE=1 才真调 security（会在钥匙串建一个临时条目，跑完删除）")
class KeychainSmokeTest(unittest.TestCase):
    """对真实钥匙串走一遍：不存在 → 建池 → 回读一致 → 加槽位到超过 4KB 也不丢 → 删条目 → 确认删干净。"""

    def test_roundtrip_on_real_keychain(self):
        service = f"agent-ntfy-smoke-{secrets.token_hex(4)}"
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)

        def gone():
            r = subprocess.run(["security", "find-generic-password", "-s", service, "-w"], capture_output=True, text=True)
            return r.returncode == state.KeychainStore.NOT_FOUND_RC

        def cleanup():
            subprocess.run(["security", "delete-generic-password", "-s", service], capture_output=True, text=True)
            self.assertTrue(gone(), f"临时钥匙串条目 {service} 没删干净，请手动 security delete-generic-password -s {service}")
        self.addCleanup(cleanup)

        store = state.KeychainStore(service=service)
        self.assertIsNone(store.load())
        st = State(store, Path(tmp.name) / "leases.json", prefix="smoke")
        topics = st.topics()
        self.assertEqual(len(topics), 5)
        self.assertEqual(state.KeychainStore(service=service).load(), topics)
        for _ in range(60):  # 60 个 topic 的 JSON 十六进制超过 4KB，覆盖长载荷路径
            st.add_slot()
        self.assertEqual(state.KeychainStore(service=service).load(), st.topics())
        self.assertEqual(len(st.topics()), 65)
