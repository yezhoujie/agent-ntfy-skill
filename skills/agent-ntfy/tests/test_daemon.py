"""daemon：socket 协议、ask 生命周期、订阅重连。进程内替身代替 ntfy 客户端，不打真网。

替身只模拟服务端可观察的行为（发布返回 id、订阅流里来事件、断线）；daemon 的所有状态变化都走真实代码路径。
"""

import json
import os
import queue
import select
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import agent_ntfy
import daemon
import ipc
import render
from ntfyclient import NtfyClient, NtfyClosed, NtfyError
from state import State
import inject
import texts
from tests.test_inject import FakeHerdr, herdr_error
from tests.test_render import NOTIFY, SAMPLE
from tests.test_state import MemoryStore

CLOSE = object()


class FakeSubscription:
    """一条假订阅流：测试往队列里放事件就是「服务端推了一条」；放异常就是断线；close() 让迭代抛 NtfyClosed。"""

    def __init__(self, topics, since):
        self.topics = list(topics)
        self.since = since
        self.q = queue.Queue()
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        item = self.q.get()
        if item is CLOSE:
            raise NtfyClosed()
        if isinstance(item, BaseException):
            raise item
        return item

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        self.closed = True
        self.q.put(CLOSE)


class FakeNtfyClient(NtfyClient):
    """记录 publish / update / clear 的调用，按顺序发 id；subscribe 交出 FakeSubscription。四个方法全部覆盖，不碰网络。"""

    def __init__(self):
        super().__init__("https://ntfy.example")
        self.published = []
        self.updates = []
        self.clears = []
        self.subscriptions = []
        self.fail_publish: NtfyError | str | None = None
        self.fail_update: str | None = None
        self.fail_subscribe = 0  # 接下来这么多次 subscribe() 抛 NtfyError（模拟网络不通、连不上）
        self.subscribe_gate: threading.Event | None = None  # 设了就让 subscribe() 卡在建连中，直到测试放行
        self._n = 0
        self._lock = threading.Lock()

    def topic_url(self, topic):
        return f"https://ntfy.example/{topic}"

    def _next_id(self):
        with self._lock:
            self._n += 1
            return f"m{self._n:04d}"

    def publish(self, topic, message, *, title=None, actions=None):
        if self.fail_publish:
            raise self.fail_publish if isinstance(self.fail_publish, NtfyError) else NtfyError("connect.failed", url=self.base_url, error=self.fail_publish)
        mid = self._next_id()
        self.published.append({"topic": topic, "message": message, "title": title, "actions": list(actions or []), "id": mid,
                               "timeout": self.timeout})
        return {"id": mid, "time": int(time.time()), "event": "message", "topic": topic, "title": title, "message": message,
                "actions": list(actions or [])}

    def update(self, topic, seq_id, message, *, title=None):
        if self.fail_update:
            raise NtfyError("connect.failed", url=self.base_url, error=self.fail_update)
        self.updates.append({"topic": topic, "seq": seq_id, "message": message, "title": title})
        return {"id": self._next_id(), "sequence_id": seq_id, "time": int(time.time()), "event": "message", "topic": topic,
                "title": title, "message": message}

    def clear(self, topic, seq_id):
        self.clears.append({"topic": topic, "seq": seq_id})
        return {"id": self._next_id(), "sequence_id": seq_id, "time": int(time.time()), "event": "message_clear", "topic": topic}

    def subscribe(self, topics, *, since=None, poll=False):  # type: ignore[override]  # 替身按鸭子类型交 FakeSubscription
        if self.fail_subscribe > 0:
            self.fail_subscribe -= 1
            raise NtfyError("connect.failed", url=self.base_url, error="连不上")
        if self.subscribe_gate is not None:
            self.subscribe_gate.wait(5)
        sub = FakeSubscription(topics, since)
        self.subscriptions.append(sub)
        return sub

    # ---- 测试侧操作

    def wait_subscription(self, n, timeout=5):
        deadline = time.monotonic() + timeout
        while len(self.subscriptions) < n:
            if time.monotonic() > deadline:
                raise AssertionError(f"{timeout}s 内没有出现第 {n} 条订阅（现有 {len(self.subscriptions)}）")
            time.sleep(0.02)
        return self.subscriptions[n - 1]

    def deliver(self, event):
        self.subscriptions[-1].q.put(event)

    def message(self, topic, text, *, mid=None, when=None):
        ev = {"id": mid or f"phone{self._next_id()}", "time": when or int(time.time()), "event": "message", "topic": topic, "message": text}
        self.deliver(ev)
        return ev

    def drop(self, why="断线"):
        self.subscriptions[-1].q.put(NtfyError("stream.broken", error=why))


def Z(key, **fmt):
    return texts.t(key, "zh", **fmt)


def wait_until(cond, timeout=5, what="条件"):
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            raise AssertionError(f"{timeout}s 内未满足：{what}")
        time.sleep(0.02)


class Harness:
    """临时 home 里起一个 daemon（后台线程），用真实的 IPC 协议与它说话。

    传输跟着环境走（AGENT_NTFY_IPC，由 ipc.transport() 在起 daemon 那一刻决定；缺省 = 平台缺省）：要在别的传输下跑同一批用例，
    用例 setUp 里设环境变量即可。`transport` / `endpoint` 记下这个实例用的传输与端点文件，用例据此断言。
    """

    transport: str = ""

    def __init__(self, case, *, subscribed=("slot1", "slot2", "slot3", "slot4", "slot5"), pool_size=5, lang="zh", **kw):
        self.tmp = tempfile.TemporaryDirectory()
        case.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.transport = ipc.transport(self.home)
        self.endpoint = ipc.endpoint_path(self.home)
        self.store = MemoryStore()
        self.state = State(self.store, self.home / "leases.json", pool_size=pool_size)
        self.state.topics()
        for slot in subscribed:
            self.state.mark_subscribed(slot)
        self.client = FakeNtfyClient()
        self.herdr = kw.pop("herdr", None) or FakeHerdr()  # 替身：任何用例都不许碰真 herdr（往活着的 pane 注会打扰它）
        self.daemon = daemon.Daemon(self.home, client=self.client, store=self.store, pool_size=pool_size, herdr=self.herdr, lang=lang, **kw)  # 显式 zh：既有用例断言的都是中文文案
        self.thread = threading.Thread(target=self.daemon.run, daemon=True)
        self.thread.start()
        case.addCleanup(self.stop)
        wait_until(lambda: self.endpoint.exists(), what="监听端点文件出现")
        self.client.wait_subscription(1)

    def stop(self):
        if self.thread.is_alive():
            self.daemon.stop()
            self.thread.join(5)

    def topic(self, slot):
        return self.state.topic_of(slot)

    def connect(self):
        return agent_ntfy.connect(self.home)

    def request(self, **req):
        """一问一答的命令：发一行（按传输补口令），收全部事件直到对端关连接。"""
        with self.connect() as sock:
            agent_ntfy.send_request(sock, req, home=self.home)
            return list(agent_ntfy.read_events(sock))

    def confirm(self, slot, *, again=False, timeout: float = 30):
        """发 confirm-sub 并读到第一条事件（topic / already_confirmed / error），把连接交回去继续。"""
        sock = self.connect()
        agent_ntfy.send_request(sock, {"cmd": "confirm-sub", "slot": slot, "again": again, "timeout": timeout}, home=self.home)
        events = agent_ntfy.read_events(sock)
        return sock, next(events), events

    def notify(self, *, leased_by="proj:/w/me", tag: str | None = "me", **extra):
        """发 notify（一问一答）：返回全部事件。extra（payload / pane / require_confirmed …）原样进请求；payload 不给就用样本。"""
        extra.setdefault("payload", NOTIFY)
        return self.request(cmd="notify", leased_by=leased_by, tag=tag, **extra)

    def ask(self, *, leased_by="wD:p1", tag: str | None = "wD:p1", timeout: float = 30, payload=None, **extra):
        """发 ask 并读到 sent（或首个终态事件），把连接交回去继续读。extra（pane / require_confirmed …）原样进请求；不给就是旧客户端形态。"""
        sock = self.connect()
        agent_ntfy.send_request(sock, {"cmd": "ask", "payload": payload or SAMPLE, "leased_by": leased_by, "tag": tag, "timeout": timeout, **extra}, home=self.home)
        events = agent_ntfy.read_events(sock)
        first = next(events)
        return sock, first, events


class AskFlowTest(unittest.TestCase):
    def test_ask_reply_roundtrip(self):
        h = Harness(self)
        sock, first, events = h.ask()
        self.assertEqual(first["event"], "sent")
        self.assertEqual(first["slot"], "slot1")
        pub = h.client.published[-1]
        self.assertEqual(first["id"], pub["id"])
        # 发出去的就是渲染层的产物：Title 带 tag，按钮回同一 topic
        self.assertEqual(pub["title"], "[wD:p1] " + SAMPLE["title"])
        self.assertEqual(pub["actions"][0]["url"], h.client.topic_url(h.topic("slot1")))
        self.assertEqual(h.request(cmd="slots")[0]["slots"]["slot1"]["state"], "已租用·活跃")
        self.assertEqual(h.request(cmd="slots")[0]["slots"]["slot1"]["state_key"], "active")
        # 手机回复
        h.client.message(h.topic("slot1"), "留固定目录")
        reply = next(events)
        self.assertEqual(reply, {"event": "reply", "text": "留固定目录"})
        with self.assertRaises(StopIteration):
            next(events)  # daemon 关了这条连接
        sock.close()
        # 立刻移出 active；卡片 update + clear
        wait_until(lambda: len(h.client.clears) == 1, what="clear 被调用")
        self.assertEqual(h.request(cmd="slots")[0]["slots"]["slot1"]["state"], "已租用·空闲")
        self.assertEqual(h.request(cmd="slots")[0]["slots"]["slot1"]["state_key"], "idle")
        upd = h.client.updates[-1]
        self.assertEqual(upd["seq"], pub["id"])
        self.assertEqual(upd["title"], Z("prefix.answered") + pub["title"])
        self.assertTrue(upd["message"].startswith("**【你的回复】** 留固定目录\n"))
        self.assertEqual(h.client.clears[-1]["seq"], pub["id"])

    def test_own_published_messages_are_ignored(self):
        h = Harness(self)
        sock, first, events = h.ask()
        pub = h.client.published[-1]
        # 订阅流里回显了我们自己发的提问（同 id），以及之后同 seq 的更新——都不是回复
        h.client.deliver({"id": pub["id"], "time": int(time.time()), "event": "message", "topic": pub["topic"], "message": pub["message"]})
        h.client.deliver({"id": "x1", "sequence_id": pub["id"], "time": int(time.time()), "event": "message", "topic": pub["topic"], "message": "更新"})
        h.client.deliver({"id": "x2", "time": int(time.time()), "event": "keepalive", "topic": pub["topic"]})
        # 正向信号：真正的回复到达并被当成回复——说明前面那三条都没有把 pending 消费掉（事件队列先进先出、单线程消费）
        h.client.message(pub["topic"], "真正的回复")
        self.assertEqual(next(events)["text"], "真正的回复")
        sock.close()
        wait_until(lambda: len(h.client.clears) == 1)
        self.assertEqual(len(h.client.updates), 1)  # 只有这一次「已回复」更新

    def test_timeout(self):
        h = Harness(self)
        sock, first, events = h.ask(timeout=1)
        pub = h.client.published[-1]
        self.assertEqual(next(events), {"event": "timeout"})
        with self.assertRaises(StopIteration):
            next(events)
        sock.close()
        wait_until(lambda: len(h.client.clears) == 1, what="clear 被调用")
        upd = h.client.updates[-1]
        self.assertEqual(upd["title"], "⌛ 已超时 · " + pub["title"])
        self.assertEqual(upd["message"], h.client.published[-1]["message"].split("\n\n---\n\n")[0])  # 正文保留原提问（六段），无提示
        self.assertEqual(h.request(cmd="slots")[0]["slots"]["slot1"]["state"], "已租用·空闲")
        # 超时之后再来的回复走无 pending 分支，不再有人等（先等它真的走到那条分支，再断言没有第二次更新）
        delivered = []
        h.daemon.deliver = lambda slot, event: delivered.append(event["id"])
        h.client.message(pub["topic"], "迟到的回复", mid="late7")
        wait_until(lambda: delivered == ["late7"], what="迟到的回复进无 pending 分支")
        self.assertEqual(len(h.client.updates), 1)

    def test_two_pending_time_out_in_the_same_round(self):
        h = Harness(self)
        s1, f1, e1 = h.ask(leased_by="wD:p1", timeout=1)
        s2, f2, e2 = h.ask(leased_by="wD:p2", timeout=1)
        self.assertEqual({f1["slot"], f2["slot"]}, {"slot1", "slot2"})
        self.assertEqual(next(e1), {"event": "timeout"})
        self.assertEqual(next(e2), {"event": "timeout"})
        s1.close(); s2.close()
        wait_until(lambda: len(h.client.clears) == 2)
        states = h.request(cmd="slots")[0]["slots"]
        self.assertEqual({states["slot1"]["state"], states["slot2"]["state"]}, {"已租用·空闲"})

    def test_reply_removes_active_even_if_update_fails_and_clear_still_runs(self):
        h = Harness(self)
        sock, first, events = h.ask()
        h.client.fail_update = "服务端 500"
        h.client.message(h.topic("slot1"), "回复")
        self.assertEqual(next(events)["text"], "回复")
        sock.close()
        wait_until(lambda: len(h.client.clears) == 1, what="update 失败仍 clear")
        self.assertEqual(h.request(cmd="slots")[0]["slots"]["slot1"]["state"], "已租用·空闲")

    def test_second_message_after_reply_goes_to_no_pending_branch(self):
        h = Harness(self)
        delivered = []
        h.daemon.deliver = lambda slot, event: delivered.append((slot, event["id"]))
        sock, first, events = h.ask()
        h.client.message(h.topic("slot1"), "第一条")
        next(events)
        sock.close()
        ev = h.client.message(h.topic("slot1"), "第二条", mid="late1")
        wait_until(lambda: delivered == [("slot1", "late1")], what="第二条进无 pending 分支")
        # 没租约的槽位来消息也一样
        ev2 = h.client.message(h.topic("slot4"), "没人租", mid="late2")
        wait_until(lambda: ("slot4", "late2") in delivered, what="未分配槽位的消息进无 pending 分支")

    def test_same_leased_by_reuses_slot_and_rejects_concurrent_ask(self):
        h = Harness(self)
        sock1, first1, events1 = h.ask(leased_by="wD:p1")
        sock2, first2, events2 = h.ask(leased_by="wD:p1")
        self.assertEqual(first2["event"], "error")
        self.assertEqual(first2["kind"], "busy")
        self.assertIs(first2["sent"], False)
        sock2.close()
        h.client.message(h.topic("slot1"), "答")
        next(events1)
        sock1.close()
        wait_until(lambda: len(h.client.clears) == 1)
        # 同一 leased_by 再问，复用 slot1
        sock3, first3, events3 = h.ask(leased_by="wD:p1")
        self.assertEqual(first3["slot"], "slot1")
        sock3.close()

    def test_unconfirmed_slot_is_refused_before_sending(self):
        h = Harness(self, subscribed=())
        sock, first, events = h.ask()
        self.assertEqual((first["event"], first["kind"], first["sent"]), ("error", "unconfirmed", False))
        self.assertIn("slot1", first["message"])
        self.assertEqual(h.client.published, [])
        sock.close()

    # 全满：列占用情况（谁租的 / 过没过闸 / 有没有提问挂着）交用户决定，不列「可替换候选」，也不让 agent 去 release 别人的
    def test_all_slots_leased_reports_holders_for_the_user_to_decide(self):
        h = Harness(self, pool_size=2, subscribed=("slot1",))
        h.state.acquire("someone-else")  # slot1 已租
        h.state.acquire("another")  # slot2 已租（未过闸）
        sock, first, events = h.ask(leased_by="wD:p9")
        self.assertEqual((first["event"], first["kind"], first["sent"]), ("error", "no_free_slot", False))
        self.assertEqual(first["holders"], [{"slot": "slot1", "leased_by": "someone-else", "subscribed": True, "active": False},
                                            {"slot": "slot2", "leased_by": "another", "subscribed": False, "active": False}])
        self.assertNotIn("candidates", first)
        self.assertNotIn("release", first["message"])
        self.assertIn("agent-ntfy add-slot", first["message"])
        self.assertEqual(first["message"], Z("daemon.no_free_slot", n=2))
        sock.close()

    def test_publish_failure_reports_not_sent(self):
        h = Harness(self)
        h.client.fail_publish = "HTTP 429"
        sock, first, events = h.ask()
        self.assertEqual((first["event"], first["kind"], first["sent"]), ("error", "publish_failed", False))
        self.assertEqual(h.request(cmd="slots")[0]["slots"]["slot1"]["state"], "已租用·空闲")
        sock.close()

    def test_invalid_payload_rejected_by_daemon(self):
        h = Harness(self)
        sock, first, events = h.ask(payload={**SAMPLE, "options": []})
        self.assertEqual((first["event"], first["kind"], first["sent"]), ("error", "invalid_input", False))
        self.assertEqual(h.client.published, [])
        sock.close()

    def test_tag_is_truncated_not_rejected(self):
        h = Harness(self)
        sock, first, events = h.ask(tag="标" * 20)  # 60 字节，预算 41 ⇒ 截到 13 个字（39 字节）
        self.assertEqual(first["event"], "sent")
        self.assertTrue(h.client.published[-1]["title"].startswith("[" + "标" * 13 + "] "))
        sock.close()

    def test_tag_defaults_to_slot_name_outside_herdr(self):
        h = Harness(self)
        sock, first, events = h.ask(tag=None)
        self.assertTrue(h.client.published[-1]["title"].startswith("[slot1] "))
        sock.close()

    def test_ask_client_disconnect_frees_active_and_cancels_card(self):
        h = Harness(self)
        sock, first, events = h.ask()
        pub = h.client.published[-1]
        sock.close()  # agent 那边 Ctrl-C / 被 kill 了
        wait_until(lambda: h.request(cmd="slots")[0]["slots"]["slot1"]["state"] == "已租用·空闲", what="连接断开后移出 active")
        # 卡片按超时同款收掉：不能让一张没人等的卡片带着按钮悬在手机上
        wait_until(lambda: len(h.client.clears) == 1, what="clear 被调用")
        upd = h.client.updates[-1]
        self.assertEqual(upd["seq"], pub["id"])
        self.assertEqual(upd["title"], "⚠️ 已取消 · " + pub["title"])
        self.assertEqual(upd["message"], pub["message"].split("\n\n---\n\n")[0])
        self.assertEqual(h.client.clears[-1]["seq"], pub["id"])
        # 之后来的回复走无 pending 分支
        delivered = []
        h.daemon.deliver = lambda slot, event: delivered.append(event["id"])
        h.client.message(pub["topic"], "迟到的回复", mid="late9")
        wait_until(lambda: delivered == ["late9"])

    def test_every_error_event_carries_sent(self):
        h = Harness(self, subscribed=())
        sock, first, events = h.ask()
        self.assertIn("sent", first)
        sock.close()
        for ev in h.request(cmd="release", slot="slot9") + h.request(cmd="confirm-sub", slot="slot9") + h.request(cmd="nonsense"):
            if ev["event"] == "error":
                self.assertIn("sent", ev, ev)


class LeasePaneTest(unittest.TestCase):
    """租约主体是项目、窗格另记：ask / slots 带 pane 就刷新租约的 pane；require_confirmed 只挑已过闸的。"""

    ME = "proj:/w/me"

    def test_ask_records_pane_and_refreshes_it_next_time(self):
        h = Harness(self)
        sock, first, events = h.ask(leased_by=self.ME, tag="me", pane="wD:p1")
        rec = h.request(cmd="slots")[0]["slots"]["slot1"]
        self.assertEqual((rec["leased_by"], rec["pane"]), (self.ME, "wD:p1"))
        h.client.message(h.topic("slot1"), "答")
        next(events)
        sock.close()
        wait_until(lambda: len(h.client.clears) == 1)
        # 同一项目换了窗格再问：还是 slot1，pane 刷新，leased_at 不动
        sock2, first2, events2 = h.ask(leased_by=self.ME, tag="me", pane="wD:p2")
        self.assertEqual(first2["slot"], "slot1")
        rec2 = h.request(cmd="slots")[0]["slots"]["slot1"]
        self.assertEqual((rec2["pane"], rec2["leased_at"]), ("wD:p2", rec["leased_at"]))
        sock2.close()

    def test_ask_with_null_pane_clears_it_but_absent_key_leaves_it(self):
        h = Harness(self)
        h.state.acquire(self.ME, pane="wD:p1")
        sock, first, events = h.ask(leased_by=self.ME, tag="me")  # 旧客户端：请求里没有 pane 这个键 ⇒ 不动
        self.assertEqual(first["event"], "sent")
        self.assertEqual(h.request(cmd="slots")[0]["slots"]["slot1"]["pane"], "wD:p1")
        sock.close()
        h.client.message(h.topic("slot1"), "答")
        wait_until(lambda: len(h.client.clears) == 1)
        sock, first, events = h.ask(leased_by=self.ME, tag="me", pane=None)  # 新客户端不在 herdr 里：pane 显式为 null ⇒ 清空
        self.assertEqual(first["event"], "sent")
        self.assertIsNone(h.request(cmd="slots")[0]["slots"]["slot1"]["pane"])
        sock.close()

    def test_release_refused_as_active_still_refreshes_pane(self):
        h = Harness(self)
        sock, first, events = h.ask(leased_by=self.ME, tag="me", pane="wD:p1")
        ev = h.request(cmd="release", leased_by=self.ME, pane="wD:p2")[0]  # 同一项目在另一个窗格里跑 release：槽位活跃，拒绝
        self.assertEqual((ev["event"], ev["kind"]), ("error", "active"))
        self.assertEqual(h.request(cmd="slots")[0]["slots"]["slot1"]["pane"], "wD:p2")  # 但窗格照样刷新：任何命令都刷新
        sock.close()

    def test_slots_with_identity_refreshes_pane_but_plain_slots_does_not(self):
        h = Harness(self)
        h.state.acquire(self.ME, pane="wD:p1")
        ev = h.request(cmd="slots", leased_by=self.ME, pane="wD:p3")[0]
        self.assertEqual(ev["event"], "slots")
        self.assertEqual(ev["slots"]["slot1"]["pane"], "wD:p3")  # 回的视图已经是刷新后的
        self.assertEqual(h.request(cmd="slots")[0]["slots"]["slot1"]["pane"], "wD:p3")  # 不带身份：只看不动
        ev = h.request(cmd="slots", leased_by="proj:/w/nobody", pane="wD:p9")[0]  # 本项目没租：什么都不刷新，也不报错
        self.assertEqual(ev["event"], "slots")
        self.assertEqual(ev["slots"]["slot1"]["pane"], "wD:p3")
        self.assertEqual(h.request(cmd="slots", leased_by=self.ME, pane=123)[0]["slots"]["slot1"]["pane"], None)  # 不是字符串当没有

    def test_require_confirmed_never_leases_an_unconfirmed_slot(self):
        h = Harness(self, pool_size=3, subscribed=("slot1",))
        h.state.acquire("proj:/w/other")  # 唯一已过闸的槽位被别的项目租走
        sock, first, events = h.ask(leased_by=self.ME, tag="me", require_confirmed=True)
        self.assertEqual((first["event"], first["kind"], first["sent"]), ("error", "no_free_slot", False))
        self.assertEqual([x["slot"] for x in first["holders"]], ["slot1"])  # 占用情况只有真租出去的
        self.assertEqual(h.request(cmd="slots")[0]["slots"]["slot2"]["state_key"], "unassigned")  # 没去租未过闸的
        self.assertEqual(first["message"], Z("daemon.no_confirmed_slot"))  # 两条出路都要人在键盘旁：等用户回来决定
        self.assertIn("confirm-sub", first["message"])
        sock.close()
        # 不带 require_confirmed（远程模式没开）照旧：租 slot2，报未过闸
        sock, first, events = h.ask(leased_by=self.ME, tag="me")
        self.assertEqual((first["event"], first["kind"], first["slot"]), ("error", "unconfirmed", "slot2"))
        sock.close()

    def test_require_confirmed_with_own_unconfirmed_lease_says_unconfirmed(self):
        h = Harness(self, subscribed=())
        h.state.acquire(self.ME)  # 本项目已租 slot1，但没过闸
        sock, first, events = h.ask(leased_by=self.ME, tag="me", require_confirmed=True)
        self.assertEqual((first["event"], first["kind"], first["slot"]), ("error", "unconfirmed", "slot1"))  # 不换槽位，提示去过闸
        sock.close()

    def test_release_without_slot_uses_leased_by(self):
        h = Harness(self)
        h.state.acquire(self.ME, pane="wD:p1")
        self.assertEqual(h.request(cmd="release", leased_by=self.ME, pane="wD:p1")[0], {"event": "released", "slot": "slot1"})
        self.assertEqual(h.request(cmd="slots")[0]["slots"]["slot1"], {"state": "未分配", "state_key": "unassigned", "subscribed": True, "leased_by": None, "leased_at": None, "pane": None})


class NotifyTest(unittest.TestCase):
    """notify：与 ask 共用租约解析，但不占「提问中」、无按钮、一问一答即关。"""

    ME = "proj:/w/me"

    def test_notify_publishes_card_without_buttons_and_does_not_occupy_the_slot(self):
        h = Harness(self)
        events = h.notify(leased_by=self.ME, tag="me", pane="wD:p1")
        pub = h.client.published[-1]
        self.assertEqual(events, [{"event": "sent", "slot": "slot1", "id": pub["id"]}])  # 一问一答：sent 之后连接就关了
        self.assertEqual(pub["title"], "[me] " + NOTIFY["title"])
        self.assertEqual(pub["message"], render.notify_message(NOTIFY, "zh"))
        self.assertEqual(pub["actions"], [])
        self.assertIn(pub["id"], h.daemon._own_ids)  # 走 _publish_own：订阅流回显它时认得出不是回复
        rec = h.request(cmd="slots")[0]["slots"]["slot1"]
        self.assertEqual((rec["state_key"], rec["leased_by"], rec["pane"]), ("idle", self.ME, "wD:p1"))  # 租到了、记了窗格，但不「活跃」
        self.assertEqual(h.request(cmd="status")[0]["pending"], 0)
        # 用户随后发的消息没有提问在等 ⇒ 走注入（无 pending 分支），不会被当成回复
        h.client.message(h.topic("slot1"), "收到")
        wait_until(lambda: any(c[1:3] == ["agent", "prompt"] for c in h.herdr.calls), what="注入")
        self.assertEqual(h.herdr.calls[-1][3], "wD:p1")
        self.assertIn("收到", h.herdr.calls[-1][4])
        self.assertEqual(len(h.client.published), 1)  # 通知卡不更新、不 clear

    def test_notify_refreshes_pane_like_any_command(self):
        h = Harness(self)
        h.state.acquire(self.ME, pane="wD:p1")
        self.assertEqual(h.notify(leased_by=self.ME, tag="me", pane="wD:p9")[0]["event"], "sent")
        self.assertEqual(h.state.slots()["slot1"]["pane"], "wD:p9")
        self.assertEqual(h.notify(leased_by=self.ME, tag="me", pane=None)[0]["event"], "sent")  # 不在 herdr 里发的
        self.assertIsNone(h.state.slots()["slot1"]["pane"])

    def test_notify_is_allowed_while_a_question_is_pending_and_reply_still_goes_to_the_question(self):
        h = Harness(self)
        sock, first, events = h.ask(leased_by=self.ME, tag="me")
        notified = h.notify(leased_by=self.ME, tag="me")
        self.assertEqual(notified[0]["event"], "sent")
        self.assertEqual(notified[0]["slot"], first["slot"])  # 同一项目同一槽位
        self.assertEqual(len(h.client.published), 2)
        h.client.message(h.topic("slot1"), "答")
        self.assertEqual(next(events), {"event": "reply", "text": "答"})  # 回复归提问（现有语义）
        sock.close()
        wait_until(lambda: len(h.client.clears) == 1)
        self.assertEqual(h.client.updates[-1]["seq"], first["id"])  # 更新的是提问卡，通知卡不动
        self.assertEqual(h.client.clears[-1]["seq"], first["id"])

    def test_notify_is_allowed_while_the_slot_is_confirming(self):
        h = Harness(self)
        h.state.acquire(self.ME)
        csock, cfirst, cevents = h.confirm("slot1", again=True)  # 换手机重新过闸：槽位「确认中」
        self.assertEqual(cfirst["event"], "topic")
        notified = h.notify(leased_by=self.ME, tag="me")
        self.assertEqual((notified[0]["event"], notified[0]["slot"]), ("sent", "slot1"))
        self.assertEqual(h.request(cmd="status")[0]["confirming"], 1)  # 确认态没被动
        csock.close()

    def test_notify_may_lease_a_slot_that_is_being_reconfirmed(self):
        h = Harness(self)
        csock, cfirst, cevents = h.confirm("slot1", again=True)  # 没人租的已过闸槽位正在重新确认
        self.assertEqual(cfirst["event"], "topic")
        notified = h.notify(leased_by=self.ME, tag="me")
        self.assertEqual((notified[0]["event"], notified[0]["slot"]), ("sent", "slot1"))  # 不像 ask 那样退回租约报 busy
        self.assertEqual(h.state.slots()["slot1"]["leased_by"], self.ME)  # 租约留着
        csock.close()

    def test_notify_unconfirmed_slot_is_refused_and_lease_is_kept(self):
        h = Harness(self, subscribed=())
        ev = h.notify(leased_by=self.ME, tag="me")[0]
        self.assertEqual((ev["event"], ev["kind"], ev["sent"], ev["slot"]), ("error", "unconfirmed", False, "slot1"))
        self.assertEqual(h.client.published, [])
        self.assertEqual(h.state.slots()["slot1"]["leased_by"], self.ME)  # 租约留着，让用户去 confirm-sub 这个槽位

    def test_notify_require_confirmed_reports_no_free_slot_with_holders(self):
        h = Harness(self, pool_size=2, subscribed=("slot1",))
        h.state.acquire("proj:/w/other")
        ev = h.notify(leased_by=self.ME, tag="me", require_confirmed=True)[0]
        self.assertEqual((ev["event"], ev["kind"], ev["sent"]), ("error", "no_free_slot", False))
        self.assertEqual([x["slot"] for x in ev["holders"]], ["slot1"])
        self.assertEqual(ev["message"], Z("daemon.no_confirmed_slot"))
        self.assertEqual(h.state.slots()["slot2"]["leased_by"], None)  # 没去租未过闸的
        self.assertEqual(h.client.published, [])

    def test_notify_invalid_input_is_refused_before_leasing(self):
        h = Harness(self)
        ev = h.notify(leased_by=self.ME, tag="me", payload={**NOTIFY, "body": "字" * 2000})[0]
        self.assertEqual((ev["event"], ev["kind"], ev["sent"]), ("error", "invalid_input", False))
        self.assertIn("body:", ev["message"])  # 通知卡的 body 就是 JSON 字段，按原名报
        ev = h.notify(leased_by=self.ME, tag="me", payload={"body": "x"})[0]
        self.assertEqual((ev["event"], ev["kind"]), ("error", "invalid_input"))
        self.assertIn("title", ev["message"])
        for bad in ("str", [1], None):
            ev = h.notify(leased_by=self.ME, tag="me", payload=bad)[0]
            self.assertEqual((ev["event"], ev["kind"]), ("error", "invalid_input"), bad)
        self.assertEqual(h.client.published, [])
        self.assertEqual(h.request(cmd="slots")[0]["slots"]["slot1"]["state_key"], "unassigned")  # 校验不过不租

    def test_notify_without_leased_by_is_a_bad_request(self):
        h = Harness(self)
        ev = h.request(cmd="notify", payload=NOTIFY, tag="me")[0]
        self.assertEqual((ev["event"], ev["kind"], ev["sent"]), ("error", "bad_request", False))
        self.assertEqual(ev["message"], Z("daemon.bad_request.notify_args"))  # notify 自己的那句，不是 ask 的、也不是「未知命令」

    def test_notify_publish_failure_reports_not_sent(self):
        h = Harness(self)
        h.client.fail_publish = "HTTP 429"
        ev = h.notify(leased_by=self.ME, tag="me")[0]
        self.assertEqual((ev["event"], ev["kind"], ev["sent"]), ("error", "publish_failed", False))
        self.assertEqual(h.request(cmd="status")[0]["pending"], 0)

    def test_notify_tag_defaults_to_slot_and_is_truncated(self):
        h = Harness(self)
        self.assertEqual(h.notify(leased_by=self.ME, tag=None)[0]["event"], "sent")
        self.assertEqual(h.client.published[-1]["title"], "[slot1] " + NOTIFY["title"])
        self.assertEqual(h.notify(leased_by=self.ME, tag="x" * 100)[0]["event"], "sent")
        self.assertEqual(h.client.published[-1]["title"], "[" + "x" * render.TAG_MAX_BYTES + "] " + NOTIFY["title"])


class CommandsTest(unittest.TestCase):
    # lease：away on 用——已有租约沿用；没有就当场租（已过闸优先，其次最小号未过闸）；全满 ⇒ no_free_slot + 候选
    def test_lease_prefers_confirmed_free_slot_then_reuses(self):
        h = Harness(self, subscribed=("slot2",))
        ev = h.request(cmd="lease", leased_by="proj:/w/a", pane="wD:p3")[0]
        self.assertEqual((ev["event"], ev["slot"], ev["subscribed"], ev["existing"]), ("leased", "slot2", True, False))
        self.assertEqual((h.state.slots()["slot2"]["leased_by"], h.state.slots()["slot2"]["pane"]), ("proj:/w/a", "wD:p3"))
        ev = h.request(cmd="lease", leased_by="proj:/w/a", pane="wD:p4")[0]
        self.assertEqual((ev["slot"], ev["existing"]), ("slot2", True))  # 沿用，且窗格刷新
        self.assertEqual(h.state.slots()["slot2"]["pane"], "wD:p4")

    def test_lease_takes_smallest_unconfirmed_when_nothing_confirmed_is_free(self):
        h = Harness(self, subscribed=())
        h.state.acquire("proj:/w/other")  # slot1 被占
        ev = h.request(cmd="lease", leased_by="proj:/w/a", pane=None)[0]
        self.assertEqual((ev["event"], ev["slot"], ev["subscribed"]), ("leased", "slot2", False))

    def test_lease_when_all_taken_lists_holders(self):
        h = Harness(self, pool_size=2, subscribed=("slot1",))
        h.state.acquire("proj:/w/a")
        h.state.acquire("proj:/w/b")
        ev = h.request(cmd="lease", leased_by="proj:/w/c", pane=None)[0]
        self.assertEqual((ev["event"], ev["kind"]), ("error", "no_free_slot"))
        self.assertEqual([(x["slot"], x["leased_by"]) for x in ev["holders"]], [("slot1", "proj:/w/a"), ("slot2", "proj:/w/b")])
        self.assertEqual(ev["message"], Z("daemon.no_free_slot", n=2))
        self.assertEqual(h.state.slots()["slot1"]["leased_by"], "proj:/w/a")  # 别人的租约一个都没动

    # 指名释放别的项目的槽位：拒绝，租约不动——要释放得由用户去那个项目关远程模式
    def test_release_by_name_refuses_another_projects_slot(self):
        h = Harness(self, pool_size=2, subscribed=("slot1",))
        h.state.acquire("proj:/w/a")
        ev = h.request(cmd="release", slot="slot1", leased_by="proj:/w/b")[0]
        self.assertEqual((ev["event"], ev["kind"]), ("error", "not_yours"))
        self.assertEqual(ev["message"], Z("daemon.release.not_yours", slot="slot1", holder="proj:/w/a"))
        self.assertEqual(h.state.slots()["slot1"]["leased_by"], "proj:/w/a")
        ev = h.request(cmd="release", slot="slot1", leased_by="proj:/w/a")[0]  # 自己的：照常
        self.assertEqual((ev["event"], ev["slot"]), ("released", "slot1"))

    # 硬约束：两个项目永远不会同时持有同一个槽位。A 租 slot1 → 释放 → 别人占满 2-5 → B 租到 slot1 → A 再来 ⇒ 只能是 no_free_slot，
    # 不能把 slot1 给 A（「以前用过」不是归属；归属只看当下的 leased_by）
    def test_lease_never_hands_a_slot_to_two_projects(self):
        h = Harness(self, subscribed=("slot1", "slot2", "slot3", "slot4", "slot5"))
        self.assertEqual(h.request(cmd="lease", leased_by="proj:/w/A", pane=None)[0]["slot"], "slot1")
        for n, other in enumerate(("proj:/w/c", "proj:/w/d", "proj:/w/e", "proj:/w/f"), start=2):
            self.assertEqual(h.request(cmd="lease", leased_by=other, pane=None)[0]["slot"], f"slot{n}")  # 其他项目占满 2-5
        self.assertEqual(h.request(cmd="release", leased_by="proj:/w/A")[0]["event"], "released")  # A 关远程模式，slot1 空出
        self.assertEqual(h.request(cmd="lease", leased_by="proj:/w/B", pane=None)[0]["slot"], "slot1")  # B 拿到 slot1
        ev = h.request(cmd="lease", leased_by="proj:/w/A", pane=None)[0]
        self.assertEqual((ev["event"], ev["kind"]), ("error", "no_free_slot"))
        holders = [r["leased_by"] for r in h.state.slots().values()]
        self.assertEqual(len(holders), len(set(holders)))  # 每个槽位一个持有者
        self.assertEqual(h.state.slots()["slot1"]["leased_by"], "proj:/w/B")
        # B 在活跃（提问挂着）时 A 也拿不到；B 释放后 A 才能租到 slot1
        self.assertEqual(h.request(cmd="release", leased_by="proj:/w/B")[0]["event"], "released")
        self.assertEqual(h.request(cmd="lease", leased_by="proj:/w/A", pane=None)[0]["slot"], "slot1")

    def test_lease_without_leased_by_is_a_bad_request(self):
        h = Harness(self)
        ev = h.request(cmd="lease")[0]
        self.assertEqual((ev["event"], ev["kind"]), ("error", "bad_request"))

    def test_slots_release_status(self):
        h = Harness(self)
        slots = h.request(cmd="slots")[0]["slots"]
        self.assertEqual(set(slots), {f"slot{i}" for i in range(1, 6)})
        self.assertNotIn("topic", json.dumps(slots))
        for t in h.state.topics():
            self.assertNotIn(t, json.dumps(slots))
        sock, first, events = h.ask()
        # 活跃拒绝释放
        ev = h.request(cmd="release", slot="slot1")[0]
        self.assertEqual((ev["event"], ev["kind"]), ("error", "active"))
        h.client.message(h.topic("slot1"), "答")
        next(events)
        sock.close()
        wait_until(lambda: len(h.client.clears) == 1)
        self.assertEqual(h.request(cmd="release", leased_by="wD:p1")[0], {"event": "released", "slot": "slot1"})
        self.assertEqual(h.request(cmd="slots")[0]["slots"]["slot1"]["state"], "未分配")
        self.assertEqual(h.request(cmd="slots")[0]["slots"]["slot1"]["state_key"], "unassigned")
        st = h.request(cmd="status")[0]
        self.assertEqual(st["event"], "status")
        self.assertEqual(st["pid"], os.getpid())
        self.assertEqual((st["subscribed"], st["pending"], st["pool"]), (True, 0, 5))

    # status 事件带传输类型（unix / tcp）：CLI 的 --status 行尾要打它
    def test_status_reports_transport(self):
        h = Harness(self)
        self.assertEqual(h.request(cmd="status")[0]["transport"], h.transport)

    # stop 命令：回 stopping（带 pid）后走正常收尾——pending 收到 daemon_stopping、文件删干净；这是三平台统一的停机路径
    def test_stop_command_replies_stopping_then_shuts_down_cleanly(self):
        h = Harness(self)
        sock, first, events = h.ask()
        self.assertEqual(h.request(cmd="stop"), [{"event": "stopping", "pid": os.getpid()}])
        ev = next(events)
        self.assertEqual((ev["event"], ev["kind"], ev["sent"]), ("error", "daemon_stopping", True))
        sock.close()
        h.thread.join(5)
        self.assertFalse(h.thread.is_alive())
        self.assertFalse((h.home / "daemon.pid").exists())
        self.assertFalse(h.endpoint.exists())
        self.assertTrue(h.client.subscriptions[-1].closed)

    def test_add_slot_extends_pool_and_resubscribes(self):
        h = Harness(self)
        ev = h.request(cmd="add-slot")[0]
        self.assertEqual(ev, {"event": "added", "slot": "slot6"})
        self.assertEqual(len(h.store.load() or []), 6)  # 钥匙串替身里多了一个 topic（Harness 自己的 State 有缓存，不看它）
        sub = h.client.wait_subscription(2)  # 池子变了：关流、按新列表重连
        self.assertEqual(len(sub.topics), 6)
        slots = h.request(cmd="slots")[0]["slots"]
        self.assertEqual((slots["slot6"]["state"], slots["slot6"]["subscribed"], slots["slot6"]["leased_by"]), ("未分配", False, None))
        self.assertEqual(h.request(cmd="status")[0]["pool"], 6)

    def test_request_split_across_two_sends(self):
        h = Harness(self)
        line = json.dumps(ipc.stamp({"cmd": "slots"}, h.home)).encode("utf-8")  # 按传输带口令，再切成两段发
        with h.connect() as sock:
            sock.sendall(line[:8])
            time.sleep(0.1)
            sock.sendall(line[8:] + b"\n")
            evs = list(agent_ntfy.read_events(sock))
        self.assertEqual(evs[0]["event"], "slots")

    def test_release_unknown_leased_by(self):
        h = Harness(self)
        ev = h.request(cmd="release", leased_by="nobody")[0]
        self.assertEqual((ev["event"], ev["kind"], ev["sent"]), ("error", "no_lease", False))

    def test_oversized_request_line_is_rejected(self):
        h = Harness(self)
        with h.connect() as sock:
            sock.settimeout(5)  # 没上限的话 daemon 会一直收下去、永远不回：那要算失败（超时抛 TimeoutError），不是「被切断」
            try:
                sock.sendall(b"x" * (2 * 1024 * 1024))
            except TimeoutError:
                raise  # daemon 一直收不断开 ⇒ 这才是用例要抓的失败，不能和下面的「对端已关」混在一起
            except OSError:
                pass  # daemon 到上限就断了，剩下的发不出去，属预期；macOS 上除 EPIPE / ECONNRESET 外还会报 ENOTCONN（errno 57），三种都是它
            try:
                tail = sock.recv(65536)  # 对端已关：要么读到 bad_request 事件后 EOF，要么直接被重置
            except TimeoutError:
                raise
            except OSError:
                tail = b""
            self.assertTrue(tail == b"" or tail.startswith(b'{"event": "error", "kind": "bad_request"'), tail[:80])
            if tail:
                try:
                    self.assertEqual(sock.recv(65536), b"")  # 事件之后就是 EOF
                except OSError:
                    pass  # tcp 上对端带着没读完的数据关连接 ⇒ 这里收到的是 RST，同样是「已关」
        self.assertEqual(h.request(cmd="slots")[0]["event"], "slots")  # daemon 没被拖垮

    def test_bad_request_line(self):
        h = Harness(self)
        with h.connect() as sock:
            sock.sendall(b"not json\n")
            evs = list(agent_ntfy.read_events(sock))
        self.assertEqual((evs[0]["event"], evs[0]["kind"]), ("error", "bad_request"))


class SubscriptionTest(unittest.TestCase):
    def test_cold_start_has_no_since_and_subscribes_all_topics(self):
        h = Harness(self)
        sub = h.client.subscriptions[0]
        self.assertIsNone(sub.since)
        self.assertEqual(sorted(sub.topics), sorted(h.state.topics()))

    def test_reconnect_uses_last_event_time_and_dedups(self):
        h = Harness(self)
        t = h.topic("slot1")
        h.client.deliver({"id": "open1", "time": 1000, "event": "open", "topic": ",".join(h.state.topics())})
        delivered = []
        h.daemon.deliver = lambda slot, event: delivered.append(event["id"])
        h.client.message(t, "a", mid="a1", when=1005)
        h.client.message(t, "b", mid="b1", when=1007)
        wait_until(lambda: delivered == ["a1", "b1"])
        h.client.drop()
        sub2 = h.client.wait_subscription(2)
        self.assertEqual(sub2.since, "1007")  # 最后事件的 time，不是 id
        # 服务端真实顺序（实测）：先发 open（time=现在），再回放 since 起的缓存——open 不能把游标推过去
        h.client.deliver({"id": "open2", "time": 2000, "event": "open", "topic": ",".join(h.state.topics())})
        # 按秒回放会把同一秒的 b1 再给一次——不能重复投递；同秒的新消息要投
        h.client.message(t, "b", mid="b1", when=1007)
        h.client.message(t, "c", mid="c1", when=1007)
        h.client.message(t, "d", mid="d1", when=1010)
        wait_until(lambda: delivered == ["a1", "b1", "c1", "d1"], what="去重后只多 c1 d1")
        time.sleep(0.2)
        self.assertEqual(delivered, ["a1", "b1", "c1", "d1"])

    def test_reconnect_backoff_and_warning_to_pending(self):
        h = Harness(self, backoff_base=0.05, backoff_max=0.2, warn_after_failures=3, warn_after_seconds=999)
        sock, first, events = h.ask()
        self.assertTrue(h.request(cmd="status")[0]["subscribed"])
        # 断线后连续 3 次重连失败 ⇒ 告警（断线本身不计）；第 4 次建连成功 ⇒ 告知恢复
        h.client.fail_subscribe = 3
        h.client.drop("断线")
        warn = next(events)
        self.assertEqual(warn["event"], "warning")
        self.assertIn("断开", warn["message"])
        sub2 = h.client.wait_subscription(2)
        back = next(events)
        self.assertEqual(back["event"], "warning")
        self.assertIn("恢复", back["message"])
        st = h.request(cmd="status")[0]
        self.assertTrue(st["subscribed"])
        self.assertIsNone(st["disconnected_for"])
        self.assertEqual(len(h.client.subscriptions), 2)
        sock.close()

    # 按时长告警：重连一直失败但次数门槛设得很高，断开超过 warn_after_seconds 也要告警
    def test_disconnect_warning_by_duration(self):
        h = Harness(self, backoff_base=0.1, backoff_max=0.1, warn_after_failures=999, warn_after_seconds=0.5)
        sock, first, events = h.ask()
        h.client.fail_subscribe = 100
        started = time.monotonic()
        h.client.drop()
        warn = next(events)
        elapsed = time.monotonic() - started
        self.assertEqual(warn["event"], "warning")
        self.assertIn("断开", warn["message"])
        self.assertGreaterEqual(elapsed, 0.5)
        h.client.fail_subscribe = 0
        sock.close()

    # 断开期间才进来的 ask（发布走 HTTP 照样通）也要被告知订阅断着
    def test_ask_during_outage_gets_warning(self):
        h = Harness(self, backoff_base=0.3, backoff_max=0.3, warn_after_failures=1, warn_after_seconds=999)
        h.client.fail_subscribe = 100
        h.client.drop()
        wait_until(lambda: not h.request(cmd="status")[0]["subscribed"], what="进入断开态")
        wait_until(lambda: h.daemon._warned_disconnect, what="告警门槛已到")
        sock, first, events = h.ask(leased_by="wD:p1")
        self.assertEqual(first["event"], "sent")
        self.assertEqual(next(events)["event"], "warning")
        h.client.fail_subscribe = 0
        sock.close()

    def test_stop_notifies_pending_with_sent_true_and_cleans_files(self):
        h = Harness(self)
        sock, first, events = h.ask()
        h.stop()
        ev = next(events)
        self.assertEqual((ev["event"], ev["kind"], ev["sent"]), ("error", "daemon_stopping", True))
        sock.close()
        self.assertFalse(h.endpoint.exists())
        self.assertFalse((h.home / "daemon.pid").exists())
        self.assertTrue(h.client.subscriptions[-1].closed)
        self.assertEqual(h.client.updates, [])  # 不改手机上的卡片

    # unix socket 路径有长度上限（macOS 104 字节）：home 太深时要说清楚，不能带着 traceback 死掉
    def test_socket_path_too_long_is_a_clear_error(self):
        import shutil
        base = Path(tempfile.mkdtemp(prefix="an-"))
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        if ipc.transport(base) != "unix":
            self.skipTest("路径长度上限只属于 unix socket")
        deep = base / ("d" * 120)
        d = daemon.Daemon(deep, client=FakeNtfyClient(), store=MemoryStore())
        with self.assertRaises(daemon.DaemonError) as cm:
            d.run()
        self.assertIn("AGENT_NTFY_HOME", str(cm.exception))
        self.assertFalse((deep / "daemon.pid").exists())

    def test_second_daemon_refuses_to_start(self):
        h = Harness(self)
        second = daemon.Daemon(h.home, client=FakeNtfyClient(), store=h.store)
        with self.assertRaises(daemon.DaemonError):
            second.run()
        # 第一个实例的 socket 与 pid 文件都还是它的
        self.assertEqual(h.request(cmd="status")[0]["pid"], os.getpid())
        self.assertTrue((h.home / "daemon.pid").exists())

    # pid 文件残留（进程早没了、pid 没被复用）+ socket 文件残留：新实例照常起来
    def test_stale_pid_and_socket_files_do_not_block_start(self):
        import shutil
        home = Path(tempfile.mkdtemp(prefix="an-")) / "h"
        self.addCleanup(shutil.rmtree, home.parent, ignore_errors=True)
        home.mkdir(mode=0o700)
        if ipc.transport(home) != "unix":
            self.skipTest("socket 文件残骸只属于 unix 传输（tcp 的残骸在 test_ipc 里测）")
        (home / "daemon.pid").write_text("999999999\n")
        (home / "daemon.sock").touch()
        d = daemon.Daemon(home, client=FakeNtfyClient(), store=MemoryStore(), pool_size=2)
        th = threading.Thread(target=d.run, daemon=True)
        th.start()
        self.addCleanup(lambda: (d.stop(), th.join(5)))
        wait_until(lambda: (home / "daemon.pid").read_text().strip() == str(os.getpid()) if (home / "daemon.pid").exists() else False, what="新实例写了自己的 pid")
        with agent_ntfy.connect(home) as sock:
            agent_ntfy.send_request(sock, {"cmd": "status"}, home=home)
            self.assertEqual(next(agent_ntfy.read_events(sock))["pid"], os.getpid())

    # 单例靠 bind 而不是 pid 文件：状态初始化再慢，第二个实例也拿不到 socket
    def test_concurrent_start_only_one_wins(self):
        import shutil
        home = Path(tempfile.mkdtemp(prefix="an-")) / "h"
        self.addCleanup(shutil.rmtree, home.parent, ignore_errors=True)
        gate = threading.Event()

        class SlowStore(MemoryStore):
            def load(self):
                gate.wait(5)  # 模拟钥匙串卡住
                return super().load()

        a = daemon.Daemon(home, client=FakeNtfyClient(), store=SlowStore(), pool_size=2)
        ta = threading.Thread(target=a.run, daemon=True)
        ta.start()
        self.addCleanup(lambda: (a.stop(), ta.join(5)))
        wait_until(lambda: ipc.endpoint_path(home).exists(), what="A 先占住监听端点")
        b = daemon.Daemon(home, client=FakeNtfyClient(), store=MemoryStore(), pool_size=2)
        with self.assertRaises(daemon.DaemonError):
            b.run()
        gate.set()
        wait_until(lambda: a.state is not None, what="A 完成初始化")
        with agent_ntfy.connect(home) as sock:
            agent_ntfy.send_request(sock, {"cmd": "status"}, home=home)
            self.assertEqual(next(agent_ntfy.read_events(sock))["pool"], 2)

    # 状态初始化失败（钥匙串读写失败是首跑最常见的失败）：要落日志、包成 DaemonError、不留 socket 残骸
    def test_state_failure_is_logged_and_wrapped(self):
        import shutil
        from state import StateError
        home = Path(tempfile.mkdtemp(prefix="an-")) / "h"
        self.addCleanup(shutil.rmtree, home.parent, ignore_errors=True)

        class BrokenStore(MemoryStore):
            def load(self):
                raise StateError("keychain.read_failed", service="x", rc=1, detail="")

        d = daemon.Daemon(home, client=FakeNtfyClient(), store=BrokenStore())
        with self.assertRaises(daemon.DaemonError) as cm:
            d.run()
        self.assertIn("钥匙串", str(cm.exception))
        self.assertIn("StateError", (home / "daemon.log").read_text(encoding="utf-8"))
        self.assertFalse(ipc.endpoint_path(home).exists())
        self.assertFalse((home / "daemon.pid").exists())

    # 没注入 store 时按 default_store 选实现（AGENT_NTFY_STORE=file ⇒ 池子落在 <home>/topics.json，不碰钥匙串）；
    # 选了 keychain 而 security 命令不存在 ⇒ 走 StateError 分支（keychain.missing），不能被当成 socket 错误，也不留 socket / pid 残骸
    def test_default_store_selection_and_missing_security_binary(self):
        import shutil
        from unittest import mock
        home = Path(tempfile.mkdtemp(prefix="an-")) / "h"
        self.addCleanup(shutil.rmtree, home.parent, ignore_errors=True)

        def no_binary(self_, argv, **kw):
            raise FileNotFoundError(2, "No such file or directory", "security")

        with self.subTest(store="file"), mock.patch.dict(os.environ, {"AGENT_NTFY_STORE": "file"}), mock.patch("state.KeychainStore._run", no_binary):
            client = FakeNtfyClient()
            d = daemon.Daemon(home, client=client, pool_size=2)
            t = threading.Thread(target=d.run, daemon=True)
            t.start()
            try:  # 断言失败也要在 patch 撤销之前把 daemon 停掉：线程若在撤销后才走到选实现那一步，会去碰真钥匙串
                wait_until(lambda: ipc.endpoint_path(home).exists(), what="监听端点文件出现")
                client.wait_subscription(1)
                self.assertEqual(len(json.loads((home / "topics.json").read_text(encoding="utf-8"))), 2)  # 池子在文件里，没去碰 security
            finally:
                d.stop()
                t.join(5)
            self.assertFalse(t.is_alive())
        home2 = home.parent / "h2"
        with self.subTest(store="keychain"), mock.patch.dict(os.environ, {"AGENT_NTFY_STORE": "keychain"}), mock.patch("state.KeychainStore._run", no_binary):
            d = daemon.Daemon(home2, client=FakeNtfyClient())
            with self.assertRaises(daemon.DaemonError) as cm:
                d.run()
            self.assertEqual(cm.exception.key, "state_init")
            self.assertIn("security", str(cm.exception))
            self.assertIn("AGENT_NTFY_STORE=file", str(cm.exception))
            self.assertNotIn("socket", str(cm.exception))
            self.assertIn("StateError", (home2 / "daemon.log").read_text(encoding="utf-8"))
            self.assertFalse(ipc.endpoint_path(home2).exists())
            self.assertFalse((home2 / "daemon.pid").exists())

    # restart 恰好落在「建连返回 → 采纳这条流」之间：采纳时必须发现代次已变、立刻换掉，不能永久挂在旧列表上
    def test_restart_between_connect_and_adopt_is_not_lost(self):
        h = Harness(self, backoff_base=0.05, backoff_max=0.05)
        real_adopt = h.daemon._adopt_subscription
        fired = threading.Event()

        def adopt_with_restart_in_the_window(sub, gen):
            if not fired.is_set():
                fired.set()
                assert h.daemon.state is not None
                h.daemon.state.add_slot()  # 池子是 daemon 自己的状态对象在管（新增槽位的命令跑在 daemon 里）
                h.daemon.restart_subscription()  # 此刻 _sub 还是 None：没有可关的流
            return real_adopt(sub, gen)
        h.daemon._adopt_subscription = adopt_with_restart_in_the_window
        h.client.drop()  # 触发重连，下一次采纳会撞上 restart
        wait_until(lambda: fired.is_set(), what="钩子触发")
        wait_until(lambda: h.daemon._sub is not None and len(h.daemon._sub.topics) == 6, what="换到 6 个 topic 的新流")
        current = h.daemon._sub
        assert h.daemon.state is not None and current is not None
        self.assertEqual(sorted(current.topics), sorted(h.daemon.state.topics()))

    # 新增槽位后重订阅：哪怕正赶上建连中（旧列表已经拿去连了、还没有可关的流），新列表也不能丢
    def test_restart_subscription_during_reconnect_is_not_lost(self):
        h = Harness(self, backoff_base=0.05, backoff_max=0.05)
        gate = threading.Event()
        h.client.subscribe_gate = gate
        h.client.drop()  # 订阅线程用旧列表进入 subscribe()，卡在 gate 上
        wait_until(lambda: h.daemon._sub is None and not h.daemon._connected, what="订阅线程进入建连中")
        time.sleep(0.2)
        assert h.daemon.state is not None
        new_slot = h.daemon.state.add_slot()
        h.daemon.restart_subscription()  # 此刻没有可 close 的流
        gate.set()  # 旧列表的流建好了——它订的是旧列表，必须被立刻换掉
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            latest = h.client.subscriptions[-1]
            if len(latest.topics) == 6 and h.daemon._sub is latest:
                break
            time.sleep(0.02)
        self.assertEqual(sorted(h.client.subscriptions[-1].topics), sorted(h.daemon.state.topics()))
        self.assertIn(h.daemon.state.topic_of(new_slot), h.client.subscriptions[-1].topics)

    def test_log_has_no_topic_or_message_text(self):
        h = Harness(self)
        sock, first, events = h.ask()
        h.client.message(h.topic("slot1"), "回复原文不该进日志")
        next(events)
        sock.close()
        wait_until(lambda: len(h.client.clears) == 1)
        h.stop()
        log = (h.home / "daemon.log").read_text(encoding="utf-8")
        self.assertIn("slot1", log)
        for t in h.state.topics():
            self.assertNotIn(t, log)
        self.assertNotIn("回复原文不该进日志", log)
        self.assertNotIn(SAMPLE["title"], log)


class ConfirmTest(unittest.TestCase):
    """可达性确认闸：只有按钮点击算确认；topic 只在这条流程里交给客户端；确认中按活跃对待。"""

    def start(self, h, slot, **kw):
        """走到「测试消息已发出」：topic → ready → sent。返回 (sock, events, sent 事件)。"""
        sock, first, events = h.confirm(slot, **kw)
        self.assertEqual(first["event"], "topic", first)
        agent_ntfy.send_request(sock, {"ready": True})
        sent = next(events)
        self.assertEqual(sent["event"], "sent", sent)
        return sock, events, sent

    def test_flow_topic_ready_sent_button_confirmed(self):
        h = Harness(self, subscribed=())
        sock, first, events = h.confirm("slot4")
        self.assertEqual(first, {"event": "topic", "topic": h.topic("slot4"), "url": h.client.topic_url(h.topic("slot4"))})
        time.sleep(0.15)
        self.assertEqual(h.client.published, [])  # 用户还没订阅：没按 ready 之前不发
        agent_ntfy.send_request(sock, {"ready": True})
        sent = next(events)
        pub = h.client.published[-1]
        self.assertEqual(sent, {"event": "sent", "slot": "slot4", "id": pub["id"]})
        self.assertEqual(pub["title"], "[slot4] 确认你能收到通知")
        self.assertEqual([a["label"] for a in pub["actions"]], [Z("confirm.button")])
        self.assertEqual(pub["actions"][0]["body"], "__agent-ntfy:confirmed:slot4__")
        self.assertEqual(h.request(cmd="slots")[0]["slots"]["slot4"]["state_key"], "confirming")
        self.assertEqual(h.request(cmd="status")[0]["confirming"], 1)
        # 手机点按钮
        h.client.message(h.topic("slot4"), inject.control_mark("confirmed", "slot4"))
        self.assertEqual(next(events), {"event": "confirmed", "slot": "slot4"})
        with self.assertRaises(StopIteration):
            next(events)
        sock.close()
        wait_until(lambda: len(h.client.clears) == 1, what="卡片 clear")
        self.assertTrue(h.state.slots()["slot4"]["subscribed"])
        upd = h.client.updates[-1]
        self.assertEqual((upd["seq"], upd["title"]), (pub["id"], Z("prefix.confirmed") + pub["title"]))
        self.assertEqual(h.request(cmd="slots")[0]["slots"]["slot4"]["state"], "未分配")  # 确认不租用
        self.assertEqual(h.request(cmd="status")[0]["confirming"], 0)
        self.assertEqual(h.herdr.calls, [])  # 标记不注入

    def test_subscribed_flag_skips_topic_phase_and_publishes_at_once(self):
        h = Harness(self, subscribed=())
        sock = h.connect()
        agent_ntfy.send_request(sock, {"cmd": "confirm-sub", "slot": "slot4", "subscribed": True, "timeout": 30}, home=h.home)
        events = agent_ntfy.read_events(sock)
        first = next(events)
        self.assertEqual(first["event"], "sent", first)  # 没有 topic 事件：topic 名不出 daemon
        self.assertEqual(h.client.published[-1]["title"], "[slot4] 确认你能收到通知")
        h.client.message(h.topic("slot4"), inject.control_mark("confirmed", "slot4"))
        self.assertEqual(next(events)["event"], "confirmed")
        sock.close()

    def test_show_topic_only_returns_topic_and_touches_nothing(self):
        h = Harness(self)
        evs = h.request(cmd="confirm-sub", slot="slot1", show_topic=True)  # 已确认的槽位也能看（换手机要重新订阅）
        self.assertEqual(evs, [{"event": "topic", "topic": h.topic("slot1"), "url": h.client.topic_url(h.topic("slot1"))}])
        self.assertEqual(h.client.published, [])
        self.assertEqual(h.request(cmd="status")[0]["confirming"], 0)

    def test_already_confirmed_without_again(self):
        h = Harness(self)
        sock, first, events = h.confirm("slot1")
        self.assertEqual(first, {"event": "already_confirmed", "slot": "slot1"})
        with self.assertRaises(StopIteration):
            next(events)
        sock.close()
        self.assertEqual(h.client.published, [])

    def test_again_reconfirms_a_confirmed_slot(self):
        h = Harness(self)
        sock, events, sent = self.start(h, "slot1", again=True)
        h.client.message(h.topic("slot1"), inject.control_mark("confirmed", "slot1"))
        self.assertEqual(next(events)["event"], "confirmed")
        sock.close()

    def test_busy_when_slot_active_or_already_confirming(self):
        h = Harness(self)
        asock, first, aevents = h.ask(leased_by="wD:p1")  # slot1 活跃
        sock, ev, events = h.confirm("slot1", again=True)
        self.assertEqual((ev["event"], ev["kind"], ev["sent"]), ("error", "busy", False))
        sock.close()
        csock, cevents, _ = self.start(h, "slot4", again=True)
        sock2, ev2, events2 = h.confirm("slot4", again=True)
        self.assertEqual((ev2["event"], ev2["kind"]), ("error", "busy"))
        sock2.close()
        self.assertEqual(h.client.published[-1]["title"], "[slot4] 确认你能收到通知")  # 第二次没再发
        self.assertEqual(sum(p["title"].startswith("[slot4]") for p in h.client.published), 1)
        csock.close()
        asock.close()

    def test_unknown_slot_is_not_a_state_failure(self):
        h = Harness(self)
        # 客户端给的槽位名不在池子里 / 形态不对：是输入错，不是状态层坏了——release / confirm-sub 统一 kind
        for slot in ("slot9", "slotX", "", None):
            sock, ev, events = h.confirm(slot)
            self.assertEqual((ev["event"], ev["kind"], ev["sent"]), ("error", "unknown_slot", False), slot)
            self.assertIn(str(slot), ev["message"])
            sock.close()
        for slot in ("slot9", "slotX"):  # release 不给 slot 也不给 leased_by 是 bad_request，不在此列
            ev = h.request(cmd="release", slot=slot)[0]
            self.assertEqual((ev["event"], ev["kind"]), ("error", "unknown_slot"), slot)
        ev = h.request(cmd="confirm-sub", slot="slot9", show_topic=True)[0]
        self.assertEqual(ev["kind"], "unknown_slot")
        self.assertEqual(h.client.published, [])

    def test_state_failure_keeps_kind_state(self):
        h = Harness(self)
        (h.home / "leases.json").write_text("{not json", encoding="utf-8")
        sock, ev, events = h.confirm("slot1")
        self.assertEqual((ev["event"], ev["kind"], ev["sent"]), ("error", "state", False))
        sock.close()
        ev = h.request(cmd="release", slot="slot1")[0]
        self.assertEqual((ev["event"], ev["kind"]), ("error", "state"))
        self.assertEqual(h.request(cmd="add-slot")[0]["kind"], "state")

    def test_text_during_confirm_is_a_warning_not_a_confirmation(self):
        h = Harness(self, subscribed=())
        sock, events, sent = self.start(h, "slot4")
        h.client.message(h.topic("slot4"), "我收到了")  # 手打的文字，不是按钮
        warn = next(events)
        self.assertEqual(warn["event"], "warning")
        self.assertIn("按钮", warn["message"])
        self.assertFalse(h.state.slots()["slot4"]["subscribed"])
        self.assertEqual(h.herdr.calls, [])  # 没有目标 agent 可注
        self.assertEqual(h.request(cmd="slots")[0]["slots"]["slot4"]["state_key"], "confirming")
        h.client.message(h.topic("slot4"), inject.control_mark("confirmed", "slot4"))
        self.assertEqual(next(events)["event"], "confirmed")
        sock.close()

    def test_timeout_updates_card_and_leaves_unconfirmed(self):
        h = Harness(self, subscribed=())
        sock, events, sent = self.start(h, "slot4", timeout=0.5)
        self.assertEqual(next(events), {"event": "timeout"})
        sock.close()
        wait_until(lambda: len(h.client.clears) == 1)
        self.assertEqual(h.client.updates[-1]["title"], Z("prefix.timeout") + "[slot4] 确认你能收到通知")
        self.assertFalse(h.state.slots()["slot4"]["subscribed"])
        self.assertEqual(h.request(cmd="status")[0]["confirming"], 0)

    def test_timeout_counts_from_request_even_before_ready(self):
        h = Harness(self, subscribed=())
        sock, first, events = h.confirm("slot4", timeout=0.5)  # 用户一直没按回车
        self.assertEqual(next(events), {"event": "timeout"})
        sock.close()
        self.assertEqual(h.client.published, [])
        self.assertEqual(h.request(cmd="status")[0]["confirming"], 0)

    def test_client_disconnect_cancels_and_closes_card(self):
        h = Harness(self, subscribed=())
        sock, events, sent = self.start(h, "slot4")
        sock.close()
        wait_until(lambda: len(h.client.clears) == 1, what="卡片 clear")
        self.assertEqual(h.client.updates[-1]["title"], Z("prefix.cancelled") + "[slot4] 确认你能收到通知")
        self.assertEqual(h.request(cmd="status")[0]["confirming"], 0)
        # 之后到达的标记已无效：丢弃、不注入、不改状态
        h.client.message(h.topic("slot4"), inject.control_mark("confirmed", "slot4"))
        h.stop()
        self.assertIn("无效的确认标记", (h.home / "daemon.log").read_text(encoding="utf-8"))
        self.assertFalse(h.state.slots()["slot4"]["subscribed"])
        self.assertEqual(h.herdr.calls, [])

    def test_release_refused_while_confirming(self):
        h = Harness(self)
        h.state.acquire("wD:p1")  # slot1 已租
        sock, events, sent = self.start(h, "slot1", again=True)
        ev = h.request(cmd="release", slot="slot1")[0]
        self.assertEqual((ev["event"], ev["kind"]), ("error", "active"))
        self.assertIn("确认", ev["message"])
        # 手机上的「释放这个槽位」同样拒绝
        h.client.message(h.topic("slot1"), inject.control_mark("release", "slot1"))
        wait_until(lambda: any(p["title"].startswith(Z("prefix.not_released")) for p in h.client.published), what="未释放说明")
        self.assertEqual(h.state.slots()["slot1"]["leased_by"], "wD:p1")
        sock.close()

    def test_ask_refused_while_slot_confirming(self):
        h = Harness(self)
        sock, events, sent = self.start(h, "slot1", again=True)  # slot1 未分配、已过闸、确认中
        asock, first, aevents = h.ask(leased_by="wD:p1")  # acquire 会挑到 slot1
        self.assertEqual((first["event"], first["kind"], first["sent"]), ("error", "busy", False))
        self.assertIn("确认", first["message"])
        asock.close()
        self.assertEqual(sum(p["title"].startswith("[slot1] " + SAMPLE["title"]) for p in h.client.published), 0)
        self.assertIsNone(h.state.slots()["slot1"]["leased_by"])  # busy 就是什么都没动：刚租到的租约要退回去
        h.client.message(h.topic("slot1"), inject.control_mark("confirmed", "slot1"))
        self.assertEqual(next(events)["event"], "confirmed")
        sock.close()
        wait_until(lambda: len(h.client.clears) == 1)
        asock2, first2, aevents2 = h.ask(leased_by="wD:p1")  # 确认完成后重试：重新租到 slot1
        self.assertEqual((first2["event"], first2["slot"]), ("sent", "slot1"))
        asock2.close()

    def test_ready_in_same_packet_as_request_is_honoured(self):
        h = Harness(self, subscribed=())
        sock = h.connect()
        req = json.dumps(ipc.stamp({"cmd": "confirm-sub", "slot": "slot4", "timeout": 30}, h.home)) + "\n" + json.dumps({"ready": True}) + "\n"
        sock.sendall(req.encode("utf-8"))  # 两行一个包到达
        events = agent_ntfy.read_events(sock)
        self.assertEqual(next(events)["event"], "topic")
        self.assertEqual(next(events)["event"], "sent")
        sock.close()

    def test_ready_split_across_two_sends_is_honoured(self):
        h = Harness(self, subscribed=())
        sock, first, events = h.confirm("slot4")
        sock.sendall(b'{"ready": tr')
        time.sleep(0.1)
        sock.sendall(b'ue}\n')
        self.assertEqual(next(events)["event"], "sent")
        sock.close()

    def test_junk_before_ready_is_ignored_and_second_ready_is_no_op(self):
        h = Harness(self, subscribed=())
        sock, first, events = h.confirm("slot4")
        sock.sendall(b'not json\n{"ready": false}\n')
        time.sleep(0.1)
        self.assertEqual(h.client.published, [])
        agent_ntfy.send_request(sock, {"ready": True})
        self.assertEqual(next(events)["event"], "sent")
        agent_ntfy.send_request(sock, {"ready": True})  # 发过了再 ready：不重发
        time.sleep(0.1)
        self.assertEqual(len(h.client.published), 1)
        sock.close()

    def test_client_disconnect_before_ready_leaves_no_trace(self):
        h = Harness(self, subscribed=())
        sock, first, events = h.confirm("slot4")
        sock.close()
        wait_until(lambda: h.request(cmd="status")[0]["confirming"] == 0, what="退出确认中")
        self.assertEqual((h.client.published, h.client.updates, h.client.clears), ([], [], []))  # 没发过消息，没有卡片可收

    def test_mark_subscribed_failure_is_an_error_and_card_is_closed(self):
        h = Harness(self, subscribed=())
        sock, events, sent = self.start(h, "slot4")
        h.daemon.state.mark_subscribed = lambda slot: (_ for _ in ()).throw(daemon.StateError("leases.write_failed", path="x", error="磁盘满"))  # type: ignore[union-attr]
        h.client.message(h.topic("slot4"), inject.control_mark("confirmed", "slot4"))
        ev = next(events)
        self.assertEqual((ev["event"], ev["kind"], ev["sent"]), ("error", "state", True))
        sock.close()
        wait_until(lambda: len(h.client.clears) == 1, what="卡片收掉")
        self.assertEqual(h.client.updates[-1]["seq"], sent["id"])
        self.assertEqual(h.request(cmd="status")[0]["confirming"], 0)

    def test_confirm_cli_gets_disconnect_warning_like_ask(self):
        h = Harness(self, subscribed=(), backoff_base=0.01, backoff_max=0.02, warn_after_failures=2)
        sock, events, sent = self.start(h, "slot4")
        h.client.fail_subscribe = 2
        h.client.drop()
        warn = next(events)
        self.assertEqual(warn["event"], "warning")
        self.assertIn("断开", warn["message"])
        sock.close()

    # CLI 发 ready 之前只凭「socket 可读」判断终态是否已到（agent_ntfy.cmd_confirm_sub）：前提是 topic 段 daemon 不往这条连接
    # 发任何非终态事件——断线 / 恢复告警与手机来文字的 warning 都以 msg_id 为门，等测试消息发出之后才发。拿一条正等着的 ask
    # 当对照：同一时刻它收到了两条 warning，确认连接一行都没有
    def test_nothing_reaches_the_confirm_connection_between_topic_and_ready(self):
        h = Harness(self, subscribed=("slot1",), backoff_base=0.01, backoff_max=0.02, warn_after_failures=2)
        ask_sock, first, ask_events = h.ask()
        self.addCleanup(ask_sock.close)
        self.assertEqual(first["event"], "sent")
        sock = h.connect()
        self.addCleanup(sock.close)
        sock.settimeout(5)
        agent_ntfy.send_request(sock, {"cmd": "confirm-sub", "slot": "slot4", "again": False, "timeout": 30}, home=h.home)
        buf = b""
        while b"\n" not in buf:
            buf += sock.recv(65536)
        self.assertEqual(buf.count(b"\n"), 1, buf)  # 恰好一行
        self.assertEqual(json.loads(buf)["event"], "topic")
        h.client.fail_subscribe = 2
        h.client.drop()  # 触发一：断线 + 两次重连失败 ⇒ 告警；第三次连上 ⇒ 「已恢复」——两条 warning 正等着的 ask 都收到了
        self.assertIn("断开", next(ask_events)["message"])
        self.assertIn("恢复", next(ask_events)["message"])
        with self.assertLogs("agent-ntfy.daemon", "INFO") as logs:
            h.client.message(h.topic("slot4"), "我收到了")  # 触发二：手机来的文字（不是按钮）
            wait_until(lambda: any("确认中收到文字" in r.getMessage() for r in logs.records), what="daemon 处理了那条文字")
        self.assertEqual(select.select([sock], [], [], 0.5), ([], [], []))  # 0.5 s 内一行都没有
        agent_ntfy.send_request(sock, {"ready": True})
        events = agent_ntfy.read_events(sock)
        self.assertEqual(next(events)["event"], "sent")
        h.client.message(h.topic("slot4"), inject.control_mark("confirmed", "slot4"))
        self.assertEqual(next(events)["event"], "confirmed")

    def test_publish_failure_is_error_not_sent_and_leaves_nothing(self):
        h = Harness(self, subscribed=())
        h.client.fail_publish = "ntfy 不通"
        sock = h.connect()
        agent_ntfy.send_request(sock, {"cmd": "confirm-sub", "slot": "slot4", "subscribed": True, "timeout": 30}, home=h.home)
        events = agent_ntfy.read_events(sock)
        ev = next(events)
        self.assertEqual((ev["event"], ev["kind"], ev["sent"]), ("error", "publish_failed", False))
        with self.assertRaises(StopIteration):
            next(events)
        sock.close()
        self.assertEqual(h.request(cmd="status")[0]["confirming"], 0)
        self.assertEqual((h.client.updates, h.client.clears), ([], []))  # 没发出去的卡片没有可收的

    def test_second_ready_after_publish_failure_does_not_publish_again(self):
        h = Harness(self, subscribed=())
        orig = h.client.publish

        def fail_once(*a, **k):  # 第一次发失败、第二次能成——同包里的第二个 ready 不能再发一张没人等的卡片
            if h.client.fail_publish:
                h.client.fail_publish = None
                raise NtfyError("connect.failed", url="x", error="暂时不通")
            return orig(*a, **k)

        h.client.fail_publish = "暂时不通"
        h.client.publish = fail_once  # type: ignore[method-assign]
        sock = h.connect()
        req = json.dumps(ipc.stamp({"cmd": "confirm-sub", "slot": "slot4", "timeout": 30}, h.home)) + "\n" + json.dumps({"ready": True}) + "\n" + json.dumps({"ready": True}) + "\n"
        sock.sendall(req.encode("utf-8"))
        events = agent_ntfy.read_events(sock)
        self.assertEqual(next(events)["event"], "topic")
        ev = next(events)
        self.assertEqual((ev["event"], ev["kind"]), ("error", "publish_failed"))
        sock.close()
        time.sleep(0.15)
        self.assertEqual(h.client.published, [])  # 终态之后的 ready 一律忽略
        self.assertEqual(h.request(cmd="status")[0]["confirming"], 0)

    def test_text_during_topic_phase_is_only_logged(self):
        h = Harness(self, subscribed=())
        sock, first, events = h.confirm("slot4")  # 还没 ready：测试消息没发，「请点按钮」这句没意义
        h.client.message(h.topic("slot4"), "这是啥")
        wait_until(lambda: "不算确认" in (h.home / "daemon.log").read_text(encoding="utf-8"), what="文字被记日志")
        agent_ntfy.send_request(sock, {"ready": True})
        self.assertEqual(next(events)["event"], "sent")  # 文字处理完了才发 ready：中间没有 warning
        sock.close()

    def test_stray_confirmed_mark_is_dropped_not_injected(self):
        h = Harness(self)
        h.state.acquire("wD:p1")
        h.client.message(h.topic("slot1"), inject.control_mark("confirmed", "slot1"))
        time.sleep(0.2)
        self.assertEqual(h.herdr.calls, [])
        self.assertEqual(h.client.published, [])
        h.stop()
        self.assertIn("无效", (h.home / "daemon.log").read_text(encoding="utf-8"))

    def test_shutdown_cancels_confirm(self):
        h = Harness(self, subscribed=())
        sock, events, sent = self.start(h, "slot4")
        h.stop()
        ev = next(events)
        self.assertEqual((ev["event"], ev["kind"], ev["sent"]), ("error", "daemon_stopping", True))
        sock.close()
        self.assertEqual(h.client.updates[-1]["title"], Z("prefix.cancelled") + "[slot4] 确认你能收到通知")
        self.assertEqual(h.client.clears[-1]["seq"], sent["id"])

    def test_unconfirmed_and_full_messages_follow_the_agreed_wording(self):
        h = Harness(self, subscribed=())
        sock, first, events = h.ask()
        self.assertEqual(first["kind"], "unconfirmed")
        self.assertIn("请用户在自己的终端跑 agent-ntfy confirm-sub slot1", first["message"])
        sock.close()
        h2 = Harness(self, pool_size=1, subscribed=("slot1",))
        h2.state.acquire("someone-else")
        sock2, first2, _ = h2.ask(leased_by="wD:p9")
        self.assertEqual(first2["kind"], "no_free_slot")
        self.assertEqual(first2["message"], Z("daemon.no_free_slot", n=1))
        sock2.close()


class InjectTest(unittest.TestCase):
    """无 pending 的消息：注入 / 回执 / 控制按钮。herdr 是替身，ntfy 是替身。"""

    def lease(self, h, slot_owner="wD:p1"):
        """给 slot1 一个租约（不经 ask，免得留 pending），租约的 pane 就用 owner 同名：下面的用例按 pane 找 FakeHerdr 里的窗格。"""
        lease = h.state.acquire(slot_owner, pane=slot_owner)
        return lease.slot

    def test_injection_targets_the_lease_pane_not_its_owner(self):
        h = Harness(self)
        h.state.acquire("proj:/w/me", pane="wD:p1")
        h.client.message(h.topic("slot1"), "把 B 方案也列进去")
        wait_until(lambda: any(c[1:3] == ["agent", "prompt"] for c in h.herdr.calls), what="herdr agent prompt 被调用")
        self.assertEqual(h.herdr.calls[-1], ["herdr", "agent", "prompt", "wD:p1", "[agent-ntfy remote] 把 B 方案也列进去"])

    def test_lease_without_pane_gets_receipt_without_touching_herdr(self):
        h = Harness(self)
        h.state.acquire("proj:/w/me")  # 不在 herdr 里租的，或升级前留下的旧租约：没有窗格可注
        h.client.message(h.topic("slot1"), "在吗")
        wait_until(lambda: len(h.client.published) == 1, what="回执发出")
        pub = h.client.published[0]
        self.assertEqual(pub["title"], "[slot1] 消息未送达")
        self.assertNotIn("proj:/w/me", pub["message"])  # 租约主体是本机路径，不上手机
        self.assertIn(Z("receipt.no_pane", slot="slot1"), pub["message"])
        self.assertIn(Z("receipt.not_delivered"), pub["message"])
        self.assertEqual([a["label"] for a in pub["actions"]], [Z("receipt.button.release"), Z("receipt.button.ignore")])
        self.assertEqual(h.herdr.calls, [])  # 没有目标就不问 herdr

    def test_message_is_injected_with_pane_id_and_prefixed_text(self):
        h = Harness(self)
        slot = self.lease(h, "wD:p1")
        n = len(h.client.published)
        h.client.message(h.topic(slot), "把 B 方案也列进去")
        wait_until(lambda: any(c[1:3] == ["agent", "prompt"] for c in h.herdr.calls), what="herdr agent prompt 被调用")
        self.assertEqual(h.herdr.calls[-1], ["herdr", "agent", "prompt", "wD:p1", "[agent-ntfy remote] 把 B 方案也列进去"])
        time.sleep(0.1)
        self.assertEqual(len(h.client.published), n)  # 送达了：没有回执

    def test_no_lease_receipt_has_only_ignore_and_goes_through_publish_own(self):
        h = Harness(self)
        h.client.message(h.topic("slot4"), "没人租")
        wait_until(lambda: len(h.client.published) == 1, what="回执发出")
        pub = h.client.published[0]
        self.assertEqual(pub["topic"], h.topic("slot4"))
        self.assertEqual(pub["title"], "[slot4] 消息未送达")
        self.assertEqual([a["label"] for a in pub["actions"]], [Z("receipt.button.ignore")])
        self.assertEqual(pub["actions"][0]["body"], "__agent-ntfy:ignore:slot4__")
        self.assertNotIn("没人租", pub["message"])
        self.assertEqual(h.herdr.calls, [])  # 没租约：herdr 一次都没调
        self.assertIn(pub["id"], h.daemon._own_ids)
        # 订阅流回显这条回执：不能再触发一条回执（成环）
        h.client.deliver({"id": pub["id"], "time": int(time.time()), "event": "message", "topic": pub["topic"], "message": pub["message"]})
        time.sleep(0.15)
        self.assertEqual(len(h.client.published), 1)

    def test_pane_missing_receipt_then_release_button_frees_slot(self):
        h = Harness(self)
        slot = self.lease(h, "wX:p9")  # 不在 pane 列表里
        h.client.message(h.topic(slot), "在吗")
        wait_until(lambda: len(h.client.published) == 1, what="回执发出")
        pub = h.client.published[0]
        self.assertEqual(pub["title"], f"[{slot}] 消息未送达")
        self.assertEqual([a["label"] for a in pub["actions"]], [Z("receipt.button.release"), Z("receipt.button.ignore")])
        self.assertIn("wX:p9", pub["message"])
        self.assertIn(Z("receipt.not_delivered"), pub["message"])
        self.assertEqual([c[1:3] for c in h.herdr.calls], [["pane", "list"]])  # 核实存在性，但没 prompt
        # 点「释放这个槽位」
        h.client.message(h.topic(slot), inject.control_mark("release", slot))
        wait_until(lambda: len(h.client.clears) == 1, what="回执被 clear")
        self.assertIsNone(h.state.slots()[slot]["leased_by"])
        upd = h.client.updates[-1]
        self.assertEqual(upd["seq"], pub["id"])
        self.assertEqual(upd["title"], Z("prefix.released") + pub["title"])
        self.assertEqual(h.client.clears[-1]["seq"], pub["id"])
        self.assertEqual(len(h.client.published), 1)  # 标记本身没有变成新回执，也没被注入
        self.assertEqual([c[1:3] for c in h.herdr.calls], [["pane", "list"]])

    def test_ignore_button_closes_receipt_and_keeps_lease(self):
        h = Harness(self)
        slot = self.lease(h, "wX:p9")
        h.client.message(h.topic(slot), "在吗")
        wait_until(lambda: len(h.client.published) == 1)
        pub = h.client.published[0]
        known = len(h.daemon._own_ids)
        h.client.message(h.topic(slot), inject.control_mark("ignore", slot))
        wait_until(lambda: len(h.client.clears) == 1, what="回执被 clear")
        self.assertEqual(h.state.slots()[slot]["leased_by"], "wX:p9")
        upd = h.client.updates[-1]
        self.assertEqual((upd["seq"], upd["title"]), (pub["id"], Z("prefix.ignored") + pub["title"]))
        self.assertEqual(len(h.daemon._own_ids), known + 2)  # update 与 clear 各返回一个新 id，都要登记：回显时才认得出不是回复

    def test_mark_for_another_slot_is_a_plain_message(self):
        h = Harness(self)
        slot = self.lease(h, "wD:p1")
        stray = inject.control_mark("release", "slot2")
        h.client.message(h.topic(slot), stray)
        wait_until(lambda: any(c[1:3] == ["agent", "prompt"] for c in h.herdr.calls), what="当普通消息注入")
        self.assertEqual(h.herdr.calls[-1][-1], inject.REMOTE_PREFIX + stray)
        self.assertEqual(h.state.slots()[slot]["leased_by"], "wD:p1")

    def test_release_mark_while_active_is_refused_and_not_taken_as_reply(self):
        h = Harness(self)
        sock, first, events = h.ask(leased_by="wD:p1")
        slot = first["slot"]
        # 旧回执的按钮在槽位活跃时被点：不许当成 ask 的回复
        h.client.message(h.topic(slot), inject.control_mark("release", slot))
        wait_until(lambda: len(h.client.published) == 2, what="无按钮的说明发出（内存里没有这条回执）")
        notice = h.client.published[-1]
        self.assertEqual(notice["actions"], [])
        self.assertTrue(notice["title"].startswith(Z("prefix.not_released")), notice["title"])
        self.assertEqual(h.request(cmd="slots")[0]["slots"][slot]["state"], "已租用·活跃")
        # ask 仍在等；真回复到达才结束
        h.client.message(h.topic(slot), "真回复")
        self.assertEqual(next(events), {"event": "reply", "text": "真回复"})
        sock.close()

    def test_release_mark_while_active_updates_existing_receipt(self):
        h = Harness(self)
        slot = self.lease(h, "wD:p1")
        h.herdr.list_result = inject.HerdrResult(rc=0, stdout='{"result":{"panes":[]}}', stderr="")  # pane 先不在
        h.client.message(h.topic(slot), "在吗")
        wait_until(lambda: len(h.client.published) == 1, what="回执发出")
        pub = h.client.published[0]
        sock, first, events = h.ask(leased_by="wD:p1")  # 同一目标再问：槽位变活跃
        self.assertEqual(first["slot"], slot)
        h.client.message(h.topic(slot), inject.control_mark("release", slot))
        wait_until(lambda: len(h.client.clears) == 1, what="回执更新并 clear")
        upd = h.client.updates[-1]
        self.assertEqual((upd["seq"], upd["title"]), (pub["id"], Z("prefix.not_released") + pub["title"]))
        self.assertIn("正在使用中", upd["message"])
        self.assertEqual(h.state.slots()[slot]["leased_by"], "wD:p1")
        h.client.message(h.topic(slot), "真回复")
        self.assertEqual(next(events), {"event": "reply", "text": "真回复"})
        sock.close()

    def test_mark_without_receipt_in_memory_still_acts_and_publishes_notice(self):
        h = Harness(self)
        slot = self.lease(h, "wX:p9")
        # daemon 重启过的形态：没有回执 id 可更新
        h.client.message(h.topic(slot), inject.control_mark("release", slot))
        wait_until(lambda: len(h.client.published) == 1, what="无按钮的说明发出")
        notice = h.client.published[0]
        self.assertEqual(notice["actions"], [])
        self.assertTrue(notice["title"].startswith(Z("prefix.released")))
        self.assertIsNone(h.state.slots()[slot]["leased_by"])
        self.assertEqual(h.client.updates, [])
        self.assertIn(notice["id"], h.daemon._own_ids)

    def test_release_mark_on_unleased_slot_reports_not_released(self):
        h = Harness(self)
        h.client.message(h.topic("slot4"), inject.control_mark("release", "slot4"))
        wait_until(lambda: len(h.client.published) == 1)
        notice = h.client.published[0]
        self.assertTrue(notice["title"].startswith(Z("prefix.not_released")), notice["title"])
        self.assertIn("没有租约", notice["message"])

    def test_prompt_failure_receipt_carries_code(self):
        h = Harness(self)
        slot = self.lease(h, "wD:p1")
        h.herdr.prompt_result = inject.HerdrResult(rc=1, stdout="", stderr=herdr_error("agent:prompt", "agent_blocked"))
        h.client.message(h.topic(slot), "在吗")
        wait_until(lambda: len(h.client.published) == 1, what="回执发出")
        pub = h.client.published[0]
        self.assertIn("agent_blocked", pub["message"])
        self.assertNotIn("在吗", pub["message"])
        self.assertEqual([a["label"] for a in pub["actions"]], [Z("receipt.button.release"), Z("receipt.button.ignore")])

    def test_kimi_wake_failure_receipt_has_only_ignore(self):
        h = Harness(self)
        slot = self.lease(h, "wD:p2")  # kimi
        h.herdr.keys_result = inject.HerdrResult(rc=1, stdout="", stderr=herdr_error("agent:send-keys", "agent_not_found"))
        h.client.message(h.topic(slot), "在吗")
        wait_until(lambda: len(h.client.published) == 1, what="回执发出")
        pub = h.client.published[0]
        self.assertEqual([a["label"] for a in pub["actions"]], [Z("receipt.button.ignore")])
        self.assertIn("agent_not_found", pub["message"])
        self.assertEqual([c[1:3] for c in h.herdr.calls], [["pane", "list"], ["agent", "prompt"], ["agent", "send-keys"]])

    def test_herdr_call_does_not_block_main_loop(self):
        gate, entered = threading.Event(), threading.Event()
        inner = FakeHerdr()

        def slow(argv):
            if argv[1:3] == ["pane", "list"]:
                entered.set()
                gate.wait(5)
            return inner(argv)

        h = Harness(self, herdr=slow)
        self.addCleanup(gate.set)
        slot = self.lease(h, "wD:p1")
        h.client.message(h.topic(slot), "慢")
        wait_until(entered.is_set, what="注入线程进入 herdr 调用")  # 主线程此时已把它计入 injecting
        t0 = time.monotonic()
        st = h.request(cmd="status")[0]  # 主循环没被子进程卡住
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertEqual((st["event"], st["injecting"], inner.calls), ("status", 1, []))
        gate.set()
        wait_until(lambda: any(c[1:3] == ["agent", "prompt"] for c in inner.calls), what="放行后注入完成")

    def test_deliveries_on_one_slot_keep_arrival_order(self):
        h = Harness(self)
        slot = self.lease(h, "wD:p1")
        for i in range(3):
            h.client.message(h.topic(slot), f"第{i}条")
        wait_until(lambda: sum(c[1:3] == ["agent", "prompt"] for c in h.herdr.calls) == 3, what="三条都注入")
        self.assertEqual([c[-1] for c in h.herdr.calls if c[1:3] == ["agent", "prompt"]], ["[agent-ntfy remote] 第0条", "[agent-ntfy remote] 第1条", "[agent-ntfy remote] 第2条"])

    def test_second_receipt_on_same_slot_closes_the_first(self):
        h = Harness(self)
        slot = self.lease(h, "wX:p9")
        h.client.message(h.topic(slot), "第一条")
        wait_until(lambda: len(h.client.published) == 1, what="第一张回执")
        first = h.client.published[0]
        h.client.message(h.topic(slot), "第二条")
        wait_until(lambda: len(h.client.published) == 2, what="第二张回执")
        # 第一张已被取代：同 seq 更新成无按钮 + clear，它的按钮不再悬着
        wait_until(lambda: len(h.client.clears) == 1, what="第一张回执被 clear")
        upd = h.client.updates[-1]
        self.assertEqual(upd["seq"], first["id"])
        self.assertEqual(upd["title"], Z("prefix.superseded") + first["title"])
        self.assertEqual(h.client.clears[-1]["seq"], first["id"])
        # 之后点「忽略」改的是第二张
        second = h.client.published[1]
        h.client.message(h.topic(slot), inject.control_mark("ignore", slot))
        wait_until(lambda: len(h.client.clears) == 2)
        self.assertEqual(h.client.updates[-1]["seq"], second["id"])

    def test_shutdown_sends_best_effort_receipts_for_undelivered(self):
        gate = threading.Event()
        inner = FakeHerdr()

        def slow(argv):
            gate.wait(10)
            return inner(argv)

        h = Harness(self, herdr=slow)
        self.addCleanup(gate.set)
        slot = self.lease(h, "wD:p1")
        first = h.client.message(h.topic(slot), "飞行中的")
        second = h.client.message(h.topic(slot), "还在排队的")
        wait_until(lambda: h.request(cmd="status")[0]["injecting"] == 2, what="两条都计入 injecting")
        h.stop()  # 在途那条 herdr 一直不返回：等过宽限期后按「无法确认」处理
        pubs = h.client.published
        self.assertEqual(len(pubs), 2, [p["title"] for p in pubs])
        by_title = {p["title"]: p for p in pubs}
        queued = by_title[f"[{slot}] 消息未送达"]
        self.assertEqual(queued["message"], "**daemon 正在停止，你刚才的消息未送达，请稍后再发。**")
        inflight = by_title[f"[{slot}] 消息可能未送达"]
        self.assertIn("无法确认", inflight["message"])
        for p in pubs:
            self.assertEqual([a["label"] for a in p["actions"]], [Z("receipt.button.ignore")])
            self.assertEqual(p["timeout"], daemon.STOP_RECEIPT_TIMEOUT)  # 关停路径每条最多等 5 秒，不用 30 秒
            self.assertNotIn("排队的", p["message"])
        log = (h.home / "daemon.log").read_text(encoding="utf-8")
        self.assertRegex(log, r"WARNING .*未投递.*id=" + second["id"])  # 排队中那条：关停时被丢，必须留痕
        self.assertRegex(log, r"WARNING .*id=" + first["id"])
        self.assertNotIn("还在排队的", log)
        gate.set()

    def test_message_pushed_during_shutdown_window_gets_receipt(self):
        h = Harness(self)
        slot = self.lease(h, "wX:p9")
        sub = h.client.subscriptions[-1]
        ev = {"id": "late-x", "time": int(time.time()), "event": "message", "topic": h.topic(slot), "message": "关停窗口里到的"}
        orig_close = sub.close

        def close_then_push():
            h.daemon._events.put(ev)  # 订阅线程在主循环退出之后、流关闭之前推进来的：已从流里消费，冷启动不回放
            orig_close()

        sub.close = close_then_push
        h.stop()
        pubs = h.client.published
        self.assertEqual(len(pubs), 1, [p["title"] for p in pubs])
        self.assertEqual((pubs[0]["title"], pubs[0]["message"]), (f"[{slot}] 消息未送达", f"**{Z('receipt.stopping')}**"))
        self.assertEqual([a["label"] for a in pubs[0]["actions"]], [Z("receipt.button.ignore")])
        self.assertEqual(pubs[0]["timeout"], daemon.STOP_RECEIPT_TIMEOUT)
        log = (h.home / "daemon.log").read_text(encoding="utf-8")
        self.assertRegex(log, r"WARNING .*id=late-x")
        self.assertNotIn("关停窗口里到的", log)

    def test_inject_layer_logs_land_in_daemon_log(self):
        h = Harness(self)
        slot = self.lease(h, "wD:p3")  # pane 列表里没有 agent 字段：注入层会记一行「按只 prompt 处理」
        h.client.message(h.topic(slot), "在吗")
        wait_until(lambda: any(c[1:3] == ["agent", "prompt"] for c in h.herdr.calls))
        h.stop()
        log = (h.home / "daemon.log").read_text(encoding="utf-8")
        self.assertIn("没有检测到 agent 种类", log)

    def test_shutdown_waits_briefly_for_inflight_delivery(self):
        inner = FakeHerdr()

        def slow(argv):
            time.sleep(0.3)
            return inner(argv)

        h = Harness(self, herdr=slow)
        slot = self.lease(h, "wD:p1")
        h.client.message(h.topic(slot), "关停前一瞬发的")
        wait_until(lambda: h.request(cmd="status")[0]["injecting"] == 1)
        h.stop()  # 在途那条在宽限期内送达了：不发回执
        self.assertEqual(h.client.published, [])
        self.assertEqual([c[1:3] for c in inner.calls], [["pane", "list"], ["agent", "prompt"]])
        log = (h.home / "daemon.log").read_text(encoding="utf-8")
        self.assertIn("已注入", log)

    def test_shutdown_receipt_publish_failure_is_only_logged(self):
        h = Harness(self)
        slot = self.lease(h, "wX:p9")
        h.client.fail_publish = "ntfy 不通"
        gate = threading.Event()
        h.daemon._herdr = lambda argv: (gate.wait(10), FakeHerdr()(argv))[1]  # 卡住在途那条，好让排队的留下
        h.client.message(h.topic(slot), "a")
        h.client.message(h.topic(slot), "b")
        wait_until(lambda: h.request(cmd="status")[0]["injecting"] == 2)
        self.addCleanup(gate.set)
        h.stop()  # 不抛、不卡
        log = (h.home / "daemon.log").read_text(encoding="utf-8")
        self.assertRegex(log, r"WARNING .*关停回执发布失败")
        self.assertIn("daemon 已退出", log)

    def test_corrupt_leases_file_yields_receipt_not_crash(self):
        h = Harness(self)
        (h.home / "leases.json").write_text("{not json", encoding="utf-8")
        h.client.message(h.topic("slot1"), "在吗")
        wait_until(lambda: len(h.client.published) == 1, what="回执发出")
        pub = h.client.published[0]
        self.assertIn(Z("receipt.uncertain"), pub["message"])
        self.assertNotIn(str(h.home), pub["message"])  # 本机路径不上手机
        self.assertEqual(h.request(cmd="status")[0]["event"], "status")  # daemon 还活着

    def test_release_mark_on_unleased_slot_does_not_leak_paths(self):
        h = Harness(self)
        h.client.message(h.topic("slot4"), inject.control_mark("release", "slot4"))
        wait_until(lambda: len(h.client.published) == 1)
        self.assertNotIn(str(h.home), h.client.published[0]["message"])

    def test_log_has_no_injected_text(self):
        h = Harness(self)
        slot = self.lease(h, "wD:p1")
        h.client.message(h.topic(slot), "注入的正文不该进日志")
        wait_until(lambda: any(c[1:3] == ["agent", "prompt"] for c in h.herdr.calls))
        h.client.message(h.topic("slot4"), "回执场景的正文也不该进日志")
        wait_until(lambda: len(h.client.published) == 1)
        h.stop()
        log = (h.home / "daemon.log").read_text(encoding="utf-8")
        self.assertIn(slot, log)
        self.assertNotIn("注入的正文不该进日志", log)
        self.assertNotIn("回执场景的正文也不该进日志", log)
        for t in h.state.topics():
            self.assertNotIn(t, log)


class ProtocolSmokeMixin:
    """协议在两种传输下都通：ask 往返 / notify / slots / stop 各一条。派生类把 transport 钉进环境；其余用例只跑平台缺省传输。"""

    transport = ""

    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"AGENT_NTFY_IPC": self.transport})
        self.env.start()
        self.addCleanup(self.env.stop)  # type: ignore[attr-defined]

    def test_ask_roundtrip(self):
        h = Harness(self)  # type: ignore[arg-type]
        self.assertEqual(h.transport, self.transport)  # type: ignore[attr-defined]
        sock, first, events = h.ask()
        self.assertEqual(first["event"], "sent")  # type: ignore[attr-defined]
        h.client.message(h.topic("slot1"), "答")
        self.assertEqual(next(events), {"event": "reply", "text": "答"})  # type: ignore[attr-defined]
        sock.close()
        wait_until(lambda: len(h.client.clears) == 1)

    def test_notify(self):
        h = Harness(self)  # type: ignore[arg-type]
        ev = h.notify()[0]
        self.assertEqual((ev["event"], ev["slot"]), ("sent", "slot1"))  # type: ignore[attr-defined]

    def test_slots_and_status_carry_transport(self):
        h = Harness(self)  # type: ignore[arg-type]
        self.assertEqual(set(h.request(cmd="slots")[0]["slots"]), {f"slot{i}" for i in range(1, 6)})  # type: ignore[attr-defined]
        self.assertEqual(h.request(cmd="status")[0]["transport"], self.transport)  # type: ignore[attr-defined]

    def test_stop(self):
        h = Harness(self)  # type: ignore[arg-type]
        self.assertEqual(h.request(cmd="stop")[0]["event"], "stopping")  # type: ignore[attr-defined]
        h.thread.join(5)
        self.assertFalse(h.thread.is_alive())  # type: ignore[attr-defined]
        self.assertFalse(h.endpoint.exists())  # type: ignore[attr-defined]


@unittest.skipIf(sys.platform == "win32", "unix socket 在 Windows 上没有")
class UnixProtocolTest(ProtocolSmokeMixin, unittest.TestCase):
    transport = "unix"


class TcpProtocolTest(ProtocolSmokeMixin, unittest.TestCase):
    transport = "tcp"

    # tcp 下口令是唯一的门：不带 / 带错都被拒，连接断开，daemon 照常活着
    def test_missing_or_wrong_token_is_rejected(self):
        h = Harness(self)
        with h.connect() as sock:
            agent_ntfy.send_request(sock, {"cmd": "status"})  # 不带口令
            events = list(agent_ntfy.read_events(sock))
        self.assertEqual((events[0]["event"], events[0]["kind"], events[0]["sent"]), ("error", "unauthorized", False))
        with h.connect() as sock:
            agent_ntfy.send_request(sock, {"cmd": "status", "token": "x" * 32})
            self.assertEqual(list(agent_ntfy.read_events(sock))[0]["kind"], "unauthorized")
        self.assertEqual(h.request(cmd="status")[0]["event"], "status")  # 带对口令的照常


if __name__ == "__main__":
    unittest.main()
