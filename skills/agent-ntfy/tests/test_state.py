"""状态层：topic 池（密钥存储）与槽位租约。

密钥存储用内存实现替身；钥匙串（security 命令）是外部程序，只在 AGENT_NTFY_SMOKE=1 时用临时条目真跑一遍。
"""

import json
import os
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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

    # 末尾带换行的名字一律拒绝：re.match 配 $ 会放过 "xxx\n"，而它会原样进 topic 名 / 钥匙串参数
    def test_names_with_trailing_newline_rejected(self):
        with self.assertRaises(state.StateError):
            State(MemoryStore(), Path(self.tmp.name) / "bad.json", prefix="agent-ntfy\n")
        with self.assertRaises(state.StateError):
            self.state.slot_state("slot1\n")
        with self.assertRaises(state.StateError):
            state.KeychainStore(service="AGENT_NTFY_TOPICS\n")

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

    # 全部已租用时抛「需用户决定」——不管别人的租约空不空闲，都不会被顶替
    def test_all_leased_raises_needs_decision_and_never_takes_over(self):
        slots = self.lease_all()
        with self.assertRaises(NeedsUserDecision):
            self.state.acquire("wD:p9")
        with self.assertRaises(NeedsUserDecision):
            self.state.acquire("wD:p9", active={slots[1], slots[3]})
        self.assertEqual([r["leased_by"] for r in self.state.slots().values()], [f"user-{i}" for i in range(5)])  # 一个都没被动

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

    # 活跃槽位上正有人等回复：不能释放；活跃集合与租约对不上要响亮失败
    def test_active_slot_cannot_be_released(self):
        slots = self.lease_all()
        with self.assertRaises(state.StateError):
            self.state.release(slots[0], active={slots[0]})
        self.assertEqual(self.state.slot_state(slots[0]), SlotState.IDLE)  # 没被动过
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

    # 落盘格式：{"slotN": {"subscribed": bool, "leased_by": str|null, "leased_at": str|null, "pane": str|null}}
    def test_leases_file_format(self):
        self.state.acquire("wD:p1")
        self.state.mark_subscribed("slot1")
        data = json.loads(self.leases_path.read_text(encoding="utf-8"))
        self.assertEqual(sorted(data), ["slot1", "slot2", "slot3", "slot4", "slot5"])
        self.assertEqual(set(data["slot1"]), {"subscribed", "leased_by", "leased_at", "pane"})
        self.assertIs(data["slot1"]["subscribed"], True)
        self.assertEqual(data["slot1"]["leased_by"], "wD:p1")
        self.assertRegex(data["slot1"]["leased_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")
        self.assertEqual(data["slot2"], {"subscribed": False, "leased_by": None, "leased_at": None, "pane": None})

    # 租约文件只有属主可读写（Windows 的 st_mode 不表达权限位）
    @unittest.skipIf(sys.platform == "win32", "POSIX 权限位")
    def test_leases_file_is_owner_only(self):
        self.state.acquire("wD:p1")
        self.assertEqual(stat.S_IMODE(self.leases_path.stat().st_mode), 0o600)

    # 订阅状态跨租约保留：释放再租，不用重新过可达性闸
    def test_subscribed_survives_release(self):
        lease = self.state.acquire("wD:p1")
        self.state.mark_subscribed(lease.slot)
        self.state.release(lease.slot)
        self.assertTrue(self.state.acquire("wD:p2").subscribed)

    # 状态在进程重启后可以从文件与密钥存储恢复
    def test_state_reloads_from_disk(self):
        lease = self.state.acquire("wD:p1")
        self.state.mark_subscribed(lease.slot)
        again = State(self.store, self.leases_path)
        self.assertEqual(again.topics(), self.state.topics())
        self.assertEqual(again.slot_state(lease.slot), SlotState.IDLE)
        view = again.slots()[lease.slot]
        self.assertEqual(view, {"state": "已租用·空闲", "subscribed": True,
                                "leased_by": "wD:p1", "leased_at": view["leased_at"], "pane": None})

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

    # 租约记录带 pane：acquire 时写入、视图里能看到、落盘
    def test_acquire_records_pane_and_slots_view_shows_it(self):
        self.state.acquire("proj:/w/a", pane="wD:p1")
        self.assertEqual(self.state.slots()["slot1"]["pane"], "wD:p1")
        data = json.loads(self.leases_path.read_text(encoding="utf-8"))
        self.assertEqual(data["slot1"]["pane"], "wD:p1")
        # 不给 pane（不在 herdr 里）就是 None，字段仍在
        self.state.acquire("proj:/w/b")
        self.assertIsNone(self.state.slots()["slot2"]["pane"])
        self.assertIn("pane", data["slot2"])

    # 旧版租约文件没有 pane 字段：读出来补 None，不报错
    def test_old_leases_file_without_pane_loads_as_none(self):
        self.state.topics()
        self.leases_path.write_text(json.dumps({"slot1": {"subscribed": True, "leased_by": "wD:p1", "leased_at": "2026-01-01T00:00:00+08:00"}}), encoding="utf-8")
        view = self.state.slots()["slot1"]
        self.assertEqual((view["leased_by"], view["pane"]), ("wD:p1", None))

    # touch_pane 只改 pane 一个字段并落盘；leased_at 不动
    def test_touch_pane_changes_only_pane(self):
        self.state.acquire("proj:/w/a", pane="wD:p1")
        before = self.state.slots()["slot1"]
        self.state.touch_pane("slot1", "wD:p7")
        after = json.loads(self.leases_path.read_text(encoding="utf-8"))["slot1"]
        self.assertEqual(after["pane"], "wD:p7")
        self.assertEqual((after["leased_by"], after["leased_at"], after["subscribed"]), (before["leased_by"], before["leased_at"], before["subscribed"]))
        self.state.touch_pane("slot1", None)  # 离开 herdr 再跑命令：pane 清空
        self.assertIsNone(self.state.slots()["slot1"]["pane"])
        with self.assertRaises(state.StateError):
            self.state.touch_pane("slot2", "wD:p1")  # 未分配的槽位没有 pane 可刷新

    # release 清空 pane
    def test_release_clears_pane(self):
        self.state.acquire("proj:/w/a", pane="wD:p1")
        self.state.release("slot1")
        data = json.loads(self.leases_path.read_text(encoding="utf-8"))
        self.assertEqual(data["slot1"], {"subscribed": False, "leased_by": None, "leased_at": None, "pane": None})

    # require_confirmed：候选只取已过闸的空闲槽位；没有就按「全满」报，候选 = 已租·空闲·已过闸
    def test_acquire_require_confirmed_uses_only_subscribed_free_slots(self):
        self.state.mark_subscribed("slot3")
        lease = self.state.acquire("proj:/w/a", require_confirmed=True)
        self.assertEqual((lease.slot, lease.subscribed), ("slot3", True))
        # 已过闸的都租出去了，剩下的空闲槽位都未过闸 ⇒ 不租，报 NeedsUserDecision
        self.state.mark_subscribed("slot4")
        self.state.acquire("proj:/w/b", require_confirmed=True)
        with self.assertRaises(NeedsUserDecision):
            self.state.acquire("proj:/w/c", require_confirmed=True)
        with self.assertRaises(NeedsUserDecision):
            self.state.acquire("proj:/w/c", active={"slot3"}, require_confirmed=True)
        self.assertEqual(self.state.slot_state("slot1"), SlotState.UNASSIGNED)  # 未过闸的空闲槽位一个都没被动
        # 不带 require_confirmed 照旧：租未过闸的空闲槽位
        self.assertEqual(self.state.acquire("proj:/w/c").slot, "slot1")


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

    # security 命令不存在（非 macOS，或 PATH 不对）：是状态层的报错，不是 socket 错；文案指路 AGENT_NTFY_STORE=file
    def test_missing_security_binary_is_keychain_missing(self):
        def no_binary(argv, **kw):
            raise FileNotFoundError(2, "No such file or directory", "security")
        for op in ("load", "save"):
            with self.subTest(op=op):
                s = self.store(no_binary)
                with self.assertRaises(state.StateError) as cm:
                    getattr(s, op)(*([] if op == "load" else [self.TOPICS]))
                self.assertEqual(cm.exception.key, "keychain.missing")
                self.assertIn("AGENT_NTFY_STORE=file", str(cm.exception))
                self.assertIn("security", str(cm.exception))

    def test_error_messages_do_not_leak_payload(self):
        payload = json.dumps(self.TOPICS).encode().hex()
        run = FakeRun(**{"add-generic-password": (1, "", f'security: unknown command "{payload}"')})
        with self.assertRaises(state.StateError) as cm:
            self.store(run).save(self.TOPICS)
        self.assertNotIn(payload, str(cm.exception))
        self.assertNotIn(self.TOPICS[0], str(cm.exception))


class FileStoreTest(unittest.TestCase):
    """0600 明文文件实现（Linux 与兜底）：读写 / 不存在 / 坏内容 / 权限位 / 文件层错误都包成 StateError。"""

    TOPICS = ["agent-ntfy-abcdefghijklmnopqrst", "agent-ntfy-0123456789abcdefghij"]

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="an-")) / "home"
        self.addCleanup(shutil.rmtree, self.home.parent, ignore_errors=True)
        self.path = self.home / "topics.json"

    def test_missing_file_is_none_and_roundtrip(self):
        store = state.FileStore(self.path)
        self.assertIsNone(store.load())
        self.assertFalse(self.home.exists())  # 只读不建目录
        store.save(self.TOPICS)
        self.assertEqual(store.load(), self.TOPICS)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), self.TOPICS)  # 就是一个 JSON 数组
        self.assertFalse(self.path.with_name("topics.json.tmp").exists())  # 临时文件已 rename 掉
        store.save(self.TOPICS[:1])  # 整体覆盖
        self.assertEqual(state.FileStore(self.path).load(), self.TOPICS[:1])

    @unittest.skipIf(sys.platform == "win32", "POSIX 权限位")
    def test_file_and_dir_are_private(self):
        state.FileStore(self.path).save(self.TOPICS)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.home.stat().st_mode), 0o700)

    def test_corrupt_content_is_state_error(self):
        self.home.mkdir(parents=True)
        for bad in ("not json", '{"a": 1}', '["ok", 2]', ""):
            with self.subTest(bad=bad):
                self.path.write_text(bad, encoding="utf-8")
                with self.assertRaises(state.StateError) as cm:
                    state.FileStore(self.path).load()
                self.assertEqual(cm.exception.key, "file.corrupt")
                self.assertIn(str(self.path), str(cm.exception))

    @unittest.skipIf(sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0), "POSIX 权限位，且 root 无视 0000")
    def test_read_and_write_failures_are_state_errors(self):
        self.home.mkdir(parents=True)
        self.path.write_text(json.dumps(self.TOPICS), encoding="utf-8")
        self.path.chmod(0)
        self.addCleanup(lambda: self.path.chmod(0o600))
        with self.assertRaises(state.StateError) as cm:
            state.FileStore(self.path).load()
        self.assertEqual(cm.exception.key, "file.read_failed")
        self.path.chmod(0o600)
        blocked = self.home / "not-a-dir" / "topics.json"
        (self.home / "not-a-dir").write_text("file", encoding="utf-8")  # 父「目录」是个文件：建不了目录、写不进去
        with self.assertRaises(state.StateError) as cm:
            state.FileStore(blocked).save(self.TOPICS)
        self.assertEqual(cm.exception.key, "file.write_failed")


def fake_protect(data: bytes) -> bytes:
    return b"DPAPI:" + bytes(b ^ 0x5A for b in data)


def fake_unprotect(data: bytes) -> bytes:
    if not data.startswith(b"DPAPI:"):
        raise OSError("not our blob")
    return bytes(b ^ 0x5A for b in data[6:])


class DpapiStoreTest(unittest.TestCase):
    """DPAPI 实现的模块逻辑：编解码经注入的 protect / unprotect；ctypes 那两个真函数只在 Windows CI 上 smoke。"""

    TOPICS = ["agent-ntfy-abcdefghijklmnopqrst", "agent-ntfy-0123456789abcdefghij"]

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="an-")) / "home"
        self.addCleanup(shutil.rmtree, self.home.parent, ignore_errors=True)
        self.path = self.home / "topics.dpapi"

    def store(self, **kw):
        return state.DpapiStore(self.path, protect=fake_protect, unprotect=fake_unprotect, **kw)

    def test_missing_file_is_none_and_roundtrip_is_ciphertext_on_disk(self):
        store = self.store()
        self.assertIsNone(store.load())
        store.save(self.TOPICS)
        raw = self.path.read_bytes()
        self.assertTrue(raw.startswith(b"DPAPI:"))
        for t in self.TOPICS:
            self.assertNotIn(t.encode("utf-8"), raw)  # 磁盘上是密文，topic 名不明文落盘
        self.assertEqual(store.load(), self.TOPICS)
        self.assertEqual(self.store().load(), self.TOPICS)
        self.assertFalse(self.path.with_name("topics.dpapi.tmp").exists())

    @unittest.skipIf(sys.platform == "win32", "POSIX 权限位")
    def test_file_and_dir_are_private(self):
        self.store().save(self.TOPICS)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.home.stat().st_mode), 0o700)

    def test_unprotect_failure_is_state_error(self):
        self.home.mkdir(parents=True)
        self.path.write_bytes(b"garbage from another user or machine")
        with self.assertRaises(state.StateError) as cm:
            self.store().load()
        self.assertEqual(cm.exception.key, "dpapi.unprotect_failed")
        self.assertIn(str(self.path), str(cm.exception))

    def test_decrypted_content_that_is_not_a_pool_is_corrupt(self):
        self.home.mkdir(parents=True)
        self.path.write_bytes(fake_protect(b'{"a": 1}'))
        with self.assertRaises(state.StateError) as cm:
            self.store().load()
        self.assertEqual(cm.exception.key, "file.corrupt")

    @unittest.skipIf(sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0), "POSIX 权限位，且 root 无视 0000")
    def test_read_and_write_failures_are_state_errors(self):
        self.home.mkdir(parents=True)
        self.path.write_bytes(fake_protect(json.dumps(self.TOPICS).encode()))
        self.path.chmod(0)
        self.addCleanup(lambda: self.path.chmod(0o600))
        with self.assertRaises(state.StateError) as cm:
            self.store().load()
        self.assertEqual(cm.exception.key, "file.read_failed")
        self.path.chmod(0o600)
        (self.home / "not-a-dir").write_text("file", encoding="utf-8")
        with self.assertRaises(state.StateError) as cm:
            state.DpapiStore(self.home / "not-a-dir" / "topics.dpapi", protect=fake_protect, unprotect=fake_unprotect).save(self.TOPICS)
        self.assertEqual(cm.exception.key, "file.write_failed")

    # 加密本身失败（比如 SSH 会话里用户主密钥不可用）：包成 StateError，且什么都不写
    def test_protect_failure_is_state_error_and_writes_nothing(self):
        def broken(data: bytes) -> bytes:
            raise OSError(13, "CryptProtectData failed")
        with self.assertRaises(state.StateError) as cm:
            state.DpapiStore(self.path, protect=broken, unprotect=fake_unprotect).save(self.TOPICS)
        self.assertEqual(cm.exception.key, "file.write_failed")
        self.assertFalse(self.home.exists())

    # 缺省就指向真函数：非 Windows 上一碰就是 dpapi.unavailable，不会静默存成明文
    @unittest.skipIf(sys.platform == "win32", "非 Windows 才会不可用")
    def test_real_dpapi_is_unavailable_off_windows(self):
        with self.assertRaises(state.StateError) as cm:
            state._protect(b"x")
        self.assertEqual(cm.exception.key, "dpapi.unavailable")
        with self.assertRaises(state.StateError) as cm:
            state._unprotect(b"x")
        self.assertEqual(cm.exception.key, "dpapi.unavailable")
        with self.assertRaises(state.StateError) as cm:
            state.DpapiStore(self.path).save(self.TOPICS)
        self.assertEqual(cm.exception.key, "dpapi.unavailable")
        self.assertFalse(self.path.exists())

    @unittest.skipUnless(sys.platform == "win32", "真 DPAPI 只在 Windows 上跑")
    def test_real_dpapi_roundtrip_on_windows(self):
        store = state.DpapiStore(self.path)
        store.save(self.TOPICS)
        raw = self.path.read_bytes()
        for t in self.TOPICS:
            self.assertNotIn(t.encode("utf-8"), raw)
        self.assertEqual(state.DpapiStore(self.path).load(), self.TOPICS)
        self.path.write_bytes(b"not a dpapi blob")
        with self.assertRaises(state.StateError) as cm:
            state.DpapiStore(self.path).load()
        self.assertEqual(cm.exception.key, "dpapi.unprotect_failed")


class DefaultStoreTest(unittest.TestCase):
    """default_store(home)：环境变量三值 / 非法 / 空串当没给 / 三个平台的缺省。环境变量只在这一个函数里读。"""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="an-")) / "home"
        self.addCleanup(shutil.rmtree, self.home.parent, ignore_errors=True)

    def with_env(self, value):
        return mock.patch.dict(os.environ, {"AGENT_NTFY_STORE": value} if value is not None else {}, clear=False)

    def test_env_selects_implementation(self):
        with self.with_env("keychain"):
            self.assertIsInstance(state.default_store(self.home), state.KeychainStore)
        with self.with_env("file"):
            s = state.default_store(self.home)
            assert isinstance(s, state.FileStore)
            self.assertEqual(s.path, self.home / "topics.json")
        with self.with_env("dpapi"):
            s = state.default_store(self.home)
            assert isinstance(s, state.DpapiStore)
            self.assertEqual(s.path, self.home / "topics.dpapi")

    def test_invalid_env_fails_loudly(self):
        for bad in ("Keychain", "sqlite", " file"):
            with self.subTest(value=bad), self.with_env(bad):
                with self.assertRaises(state.StateError) as cm:
                    state.default_store(self.home)
                self.assertEqual(cm.exception.key, "store.bad_env")
                self.assertIn(bad, str(cm.exception))
                self.assertIn("keychain / file / dpapi", str(cm.exception))

    # 测试进程的缺省：tests 包导入时把 AGENT_NTFY_STORE 钉成 file，没显式注入 store 的用例不会碰真钥匙串 / DPAPI
    def test_test_process_defaults_to_file_store(self):
        self.assertEqual(os.environ.get("AGENT_NTFY_STORE"), "file", "tests/__init__.py 把缺省钉成 file；shell 里导出了别的值先 unset")
        self.assertIsInstance(state.default_store(self.home), state.FileStore)

    def test_platform_defaults(self):
        for env in (None, ""):  # 没给 / 空串都走平台缺省
            with self.subTest(env=env):
                with mock.patch.dict(os.environ, {k: v for k, v in os.environ.items() if k != "AGENT_NTFY_STORE"}, clear=True), self.with_env(env):
                    with mock.patch.object(state.sys, "platform", "darwin"):
                        self.assertIsInstance(state.default_store(self.home), state.KeychainStore)
                    with mock.patch.object(state.sys, "platform", "win32"):
                        self.assertIsInstance(state.default_store(self.home), state.DpapiStore)
                    for other in ("linux", "freebsd14", "cygwin"):
                        with mock.patch.object(state.sys, "platform", other):
                            self.assertIsInstance(state.default_store(self.home), state.FileStore)


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
