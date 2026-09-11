"""daemon：socket 协议、ask 生命周期、订阅重连。进程内替身代替 ntfy 客户端，不打真网。

替身只模拟服务端可观察的行为（发布返回 id、订阅流里来事件、断线）；daemon 的所有状态变化都走真实代码路径。
"""

import json
import os
import queue
import tempfile
import threading
import time
import unittest
from pathlib import Path

import agent_ntfy
import daemon
import render
from ntfyclient import NtfyClient, NtfyClosed, NtfyError
from state import State
import inject
from tests.test_inject import FakeHerdr, herdr_error
from tests.test_render import SAMPLE
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
            raise NtfyClosed("订阅已被本进程关闭")
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
        self.fail_publish: str | None = None
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
            raise NtfyError(self.fail_publish)
        mid = self._next_id()
        self.published.append({"topic": topic, "message": message, "title": title, "actions": list(actions or []), "id": mid,
                               "timeout": self.timeout})
        return {"id": mid, "time": int(time.time()), "event": "message", "topic": topic, "title": title, "message": message,
                "actions": list(actions or [])}

    def update(self, topic, seq_id, message, *, title=None):
        if self.fail_update:
            raise NtfyError(self.fail_update)
        self.updates.append({"topic": topic, "seq": seq_id, "message": message, "title": title})
        return {"id": self._next_id(), "sequence_id": seq_id, "time": int(time.time()), "event": "message", "topic": topic,
                "title": title, "message": message}

    def clear(self, topic, seq_id):
        self.clears.append({"topic": topic, "seq": seq_id})
        return {"id": self._next_id(), "sequence_id": seq_id, "time": int(time.time()), "event": "message_clear", "topic": topic}

    def subscribe(self, topics, *, since=None, poll=False):  # type: ignore[override]  # 替身按鸭子类型交 FakeSubscription
        if self.fail_subscribe > 0:
            self.fail_subscribe -= 1
            raise NtfyError("连不上")
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
        self.subscriptions[-1].q.put(NtfyError(why))


def wait_until(cond, timeout=5, what="条件"):
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            raise AssertionError(f"{timeout}s 内未满足：{what}")
        time.sleep(0.02)


class Harness:
    """临时 home 里起一个 daemon（后台线程），用真实的 unix socket 协议与它说话。"""

    def __init__(self, case, *, subscribed=("slot1", "slot2", "slot3", "slot4", "slot5"), pool_size=5, **kw):
        self.tmp = tempfile.TemporaryDirectory()
        case.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.store = MemoryStore()
        self.state = State(self.store, self.home / "leases.json", pool_size=pool_size)
        self.state.topics()
        for slot in subscribed:
            self.state.mark_subscribed(slot)
        self.client = FakeNtfyClient()
        self.herdr = kw.pop("herdr", None) or FakeHerdr()  # 替身：任何用例都不许碰真 herdr（往活着的 pane 注会打扰它）
        self.daemon = daemon.Daemon(self.home, client=self.client, store=self.store, pool_size=pool_size, herdr=self.herdr, **kw)
        self.thread = threading.Thread(target=self.daemon.run, daemon=True)
        self.thread.start()
        case.addCleanup(self.stop)
        wait_until(lambda: (self.home / "daemon.sock").exists(), what="socket 文件出现")
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
        """一问一答的命令：发一行，收全部事件直到对端关连接。"""
        with self.connect() as sock:
            agent_ntfy.send_request(sock, req)
            return list(agent_ntfy.read_events(sock))

    def ask(self, *, leased_by="wD:p1", tag: str | None = "wD:p1", timeout=30, payload=None):
        """发 ask 并读到 sent（或首个终态事件），把连接交回去继续读。"""
        sock = self.connect()
        agent_ntfy.send_request(sock, {"cmd": "ask", "payload": payload or SAMPLE, "leased_by": leased_by, "tag": tag, "timeout": timeout})
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
        upd = h.client.updates[-1]
        self.assertEqual(upd["seq"], pub["id"])
        self.assertEqual(upd["title"], render.ANSWERED_PREFIX + pub["title"])
        self.assertTrue(upd["message"].startswith("【你的回复】留固定目录\n"))
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
        self.assertEqual(upd["message"], h.client.published[-1]["message"].split("\n──────────\n")[0])  # 正文保留原提问（六段），无提示
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

    def test_all_slots_leased_reports_candidates_with_subscription_state(self):
        h = Harness(self, pool_size=2, subscribed=("slot1",))
        h.state.acquire("someone-else")  # slot1 已租
        h.state.acquire("another")  # slot2 已租（未过闸）
        sock, first, events = h.ask(leased_by="wD:p9")
        self.assertEqual((first["event"], first["kind"], first["sent"]), ("error", "no_free_slot", False))
        self.assertEqual(first["candidates"], [{"slot": "slot1", "subscribed": True}, {"slot": "slot2", "subscribed": False}])
        self.assertIn("agent-ntfy release <slot>", first["message"])
        self.assertIn("agent-ntfy add-slot", first["message"])
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
        sock, first, events = h.ask(tag="标" * 20)  # 60 字节，预算 44
        self.assertEqual(first["event"], "sent")
        self.assertTrue(h.client.published[-1]["title"].startswith("[" + "标" * 14 + "] "))
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
        self.assertEqual(upd["message"], pub["message"].split("\n──────────\n")[0])
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
        for ev in h.request(cmd="release", slot="slot9") + h.request(cmd="confirm-sub", slot="slot1") + h.request(cmd="nonsense"):
            if ev["event"] == "error":
                self.assertIn("sent", ev, ev)


class CommandsTest(unittest.TestCase):
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
        st = h.request(cmd="status")[0]
        self.assertEqual(st["event"], "status")
        self.assertEqual(st["pid"], os.getpid())
        self.assertEqual((st["subscribed"], st["pending"], st["pool"]), (True, 0, 5))

    def test_confirm_sub_and_add_slot_are_reserved(self):
        h = Harness(self)
        for req in ({"cmd": "confirm-sub", "slot": "slot1"}, {"cmd": "add-slot"}):
            ev = h.request(**req)[0]
            self.assertEqual((ev["event"], ev["kind"], ev["sent"]), ("error", "not_implemented", False), req)

    def test_request_split_across_two_sends(self):
        h = Harness(self)
        with h.connect() as sock:
            sock.sendall(b'{"cmd": "sl')
            time.sleep(0.1)
            sock.sendall(b'ots"}\n')
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
                self.assertEqual(sock.recv(65536), b"")  # 事件之后就是 EOF
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
        self.assertFalse((h.home / "daemon.sock").exists())
        self.assertFalse((h.home / "daemon.pid").exists())
        self.assertTrue(h.client.subscriptions[-1].closed)
        self.assertEqual(h.client.updates, [])  # 不改手机上的卡片

    # unix socket 路径有长度上限（macOS 104 字节）：home 太深时要说清楚，不能带着 traceback 死掉
    def test_socket_path_too_long_is_a_clear_error(self):
        import shutil
        base = Path(tempfile.mkdtemp(prefix="an-"))
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
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
        (home / "daemon.pid").write_text("999999999\n")
        (home / "daemon.sock").touch()
        d = daemon.Daemon(home, client=FakeNtfyClient(), store=MemoryStore(), pool_size=2)
        th = threading.Thread(target=d.run, daemon=True)
        th.start()
        self.addCleanup(lambda: (d.stop(), th.join(5)))
        wait_until(lambda: (home / "daemon.pid").read_text().strip() == str(os.getpid()) if (home / "daemon.pid").exists() else False, what="新实例写了自己的 pid")
        with agent_ntfy.connect(home) as sock:
            agent_ntfy.send_request(sock, {"cmd": "status"})
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
        wait_until(lambda: (home / "daemon.sock").exists(), what="A 先占住 socket")
        b = daemon.Daemon(home, client=FakeNtfyClient(), store=MemoryStore(), pool_size=2)
        with self.assertRaises(daemon.DaemonError):
            b.run()
        gate.set()
        wait_until(lambda: a.state is not None, what="A 完成初始化")
        with agent_ntfy.connect(home) as sock:
            agent_ntfy.send_request(sock, {"cmd": "status"})
            self.assertEqual(next(agent_ntfy.read_events(sock))["pool"], 2)

    # 状态初始化失败（钥匙串读写失败是首跑最常见的失败）：要落日志、包成 DaemonError、不留 socket 残骸
    def test_state_failure_is_logged_and_wrapped(self):
        import shutil
        from state import StateError
        home = Path(tempfile.mkdtemp(prefix="an-")) / "h"
        self.addCleanup(shutil.rmtree, home.parent, ignore_errors=True)

        class BrokenStore(MemoryStore):
            def load(self):
                raise StateError("读钥匙串条目失败（rc=1）")

        d = daemon.Daemon(home, client=FakeNtfyClient(), store=BrokenStore())
        with self.assertRaises(daemon.DaemonError) as cm:
            d.run()
        self.assertIn("钥匙串", str(cm.exception))
        self.assertIn("StateError", (home / "daemon.log").read_text(encoding="utf-8"))
        self.assertFalse((home / "daemon.sock").exists())
        self.assertFalse((home / "daemon.pid").exists())

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


class InjectTest(unittest.TestCase):
    """无 pending 的消息：注入 / 回执 / 控制按钮。herdr 是替身，ntfy 是替身。"""

    def lease(self, h, slot_owner="wD:p1"):
        """给 slot1 一个租约（不经 ask，免得留 pending）。"""
        lease = h.state.acquire(slot_owner)
        return lease.slot

    def test_message_is_injected_with_pane_id_and_raw_text(self):
        h = Harness(self)
        slot = self.lease(h, "wD:p1")
        n = len(h.client.published)
        h.client.message(h.topic(slot), "把 B 方案也列进去")
        wait_until(lambda: any(c[1:3] == ["agent", "prompt"] for c in h.herdr.calls), what="herdr agent prompt 被调用")
        self.assertEqual(h.herdr.calls[-1], ["herdr", "agent", "prompt", "wD:p1", "把 B 方案也列进去"])
        time.sleep(0.1)
        self.assertEqual(len(h.client.published), n)  # 送达了：没有回执

    def test_no_lease_receipt_has_only_ignore_and_goes_through_publish_own(self):
        h = Harness(self)
        h.client.message(h.topic("slot4"), "没人租")
        wait_until(lambda: len(h.client.published) == 1, what="回执发出")
        pub = h.client.published[0]
        self.assertEqual(pub["topic"], h.topic("slot4"))
        self.assertEqual(pub["title"], "[slot4] 消息未送达")
        self.assertEqual([a["label"] for a in pub["actions"]], [inject.IGNORE_LABEL])
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
        self.assertEqual([a["label"] for a in pub["actions"]], [inject.RELEASE_LABEL, inject.IGNORE_LABEL])
        self.assertIn("wX:p9", pub["message"])
        self.assertIn(inject.NOT_DELIVERED_LINE, pub["message"])
        self.assertEqual([c[1:3] for c in h.herdr.calls], [["pane", "list"]])  # 核实存在性，但没 prompt
        # 点「释放这个槽位」
        h.client.message(h.topic(slot), inject.control_mark("release", slot))
        wait_until(lambda: len(h.client.clears) == 1, what="回执被 clear")
        self.assertIsNone(h.state.slots()[slot]["leased_by"])
        upd = h.client.updates[-1]
        self.assertEqual(upd["seq"], pub["id"])
        self.assertEqual(upd["title"], inject.RELEASED_PREFIX + pub["title"])
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
        self.assertEqual((upd["seq"], upd["title"]), (pub["id"], inject.IGNORED_PREFIX + pub["title"]))
        self.assertEqual(len(h.daemon._own_ids), known + 2)  # update 与 clear 各返回一个新 id，都要登记：回显时才认得出不是回复

    def test_mark_for_another_slot_is_a_plain_message(self):
        h = Harness(self)
        slot = self.lease(h, "wD:p1")
        stray = inject.control_mark("release", "slot2")
        h.client.message(h.topic(slot), stray)
        wait_until(lambda: any(c[1:3] == ["agent", "prompt"] for c in h.herdr.calls), what="当普通消息注入")
        self.assertEqual(h.herdr.calls[-1][-1], stray)
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
        self.assertTrue(notice["title"].startswith(inject.NOT_RELEASED_PREFIX), notice["title"])
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
        self.assertEqual((upd["seq"], upd["title"]), (pub["id"], inject.NOT_RELEASED_PREFIX + pub["title"]))
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
        self.assertTrue(notice["title"].startswith(inject.RELEASED_PREFIX))
        self.assertIsNone(h.state.slots()[slot]["leased_by"])
        self.assertEqual(h.client.updates, [])
        self.assertIn(notice["id"], h.daemon._own_ids)

    def test_release_mark_on_unleased_slot_reports_not_released(self):
        h = Harness(self)
        h.client.message(h.topic("slot4"), inject.control_mark("release", "slot4"))
        wait_until(lambda: len(h.client.published) == 1)
        notice = h.client.published[0]
        self.assertTrue(notice["title"].startswith(inject.NOT_RELEASED_PREFIX), notice["title"])
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
        self.assertEqual([a["label"] for a in pub["actions"]], [inject.RELEASE_LABEL, inject.IGNORE_LABEL])

    def test_kimi_wake_failure_receipt_has_only_ignore(self):
        h = Harness(self)
        slot = self.lease(h, "wD:p2")  # kimi
        h.herdr.keys_result = inject.HerdrResult(rc=1, stdout="", stderr=herdr_error("agent:send-keys", "agent_not_found"))
        h.client.message(h.topic(slot), "在吗")
        wait_until(lambda: len(h.client.published) == 1, what="回执发出")
        pub = h.client.published[0]
        self.assertEqual([a["label"] for a in pub["actions"]], [inject.IGNORE_LABEL])
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
        self.assertEqual([c[-1] for c in h.herdr.calls if c[1:3] == ["agent", "prompt"]], ["第0条", "第1条", "第2条"])

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
        self.assertEqual(upd["title"], inject.SUPERSEDED_PREFIX + first["title"])
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
        self.assertEqual(queued["message"], "daemon 正在停止，你刚才的消息未送达，请稍后再发。")
        inflight = by_title[f"[{slot}] 消息可能未送达"]
        self.assertIn("无法确认", inflight["message"])
        for p in pubs:
            self.assertEqual([a["label"] for a in p["actions"]], [inject.IGNORE_LABEL])
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
        self.assertEqual((pubs[0]["title"], pubs[0]["message"]), (f"[{slot}] 消息未送达", inject.STOPPING_LINE))
        self.assertEqual([a["label"] for a in pubs[0]["actions"]], [inject.IGNORE_LABEL])
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
        self.assertIn(inject.UNCERTAIN_LINE, pub["message"])
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


if __name__ == "__main__":
    unittest.main()
