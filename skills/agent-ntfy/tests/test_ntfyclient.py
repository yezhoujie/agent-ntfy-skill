"""ntfy 客户端层：发布 / 更新 / clear / 订阅，对真实 ntfy.sh 跑，不 mock。

网络不可达时用例直接报错，不 skip——「OK (skipped=N)」与全绿在观感上一样，会把坏的当好的。
唯一的跳过开关是 AGENT_NTFY_OFFLINE=1（CI / 省配额用）：整类 RealNtfyTest 显式 skip，本地替身照跑。
每次运行随机生成测试 topic（前缀 agent-ntfy-test-，与正式池的 agent-ntfy- 区分开），不订阅、不清理，
服务端 12 小时自动过期。
ntfy.sh 对同一来源 IP 每天限 250 条消息（docs.ntfy.sh/publish → Limitations），每条真网用例要发 1~3 条，
别把整套放进循环里反复跑；收到 HTTP 429 先当限流，别怀疑代码。
"""

import email.header
import http.server
import inspect
import io
import json
import os
import re
import secrets
import socket
import threading
import time
import unittest
from unittest import mock

import ntfyclient
from ntfyclient import NtfyClient, NtfyClosed, NtfyError, http_action

RUN_ID = secrets.token_hex(4)
CHINESE_TITLE = "✅ 已回复 · 助手没检出代码时，临时目录留还是删"


def topic_for(name):
    """每个用例一个 topic，互不串扰；形如 agent-ntfy-test-<8 位十六进制>-<用例名>。"""
    return f"agent-ntfy-test-{RUN_ID}-{name}"


class NoNetworkClient(NtfyClient):
    """不许发请求的替身：本地校验必须在发请求之前拦下，一旦走到网络就是失败。"""

    def _open(self, req, timeout):
        raise AssertionError(f"不该发请求：{req.get_method()} {req.full_url}")

    def _connect(self, path, timeout):
        raise AssertionError(f"不该建连：GET {path}")


REPLAY_TIMEOUT = 180  # 服务端把消息写进缓存的延迟实测从 1 秒到数分钟不等，回放类用例要等它
REPLAY_INTERVAL = 5  # 轮询别太密：ntfy.sh 的请求桶 60 个、每 5 秒补 1 个


def messages(client, topics, since, want):
    """poll 直到收到 want 条 message 事件，只返回 message 事件；等不到就响亮失败，不 skip。"""
    deadline = time.monotonic() + REPLAY_TIMEOUT
    while True:
        got = [e for e in client.subscribe(topics, since=since, poll=True) if e["event"] == "message"]
        if len(got) >= want:
            return got
        if time.monotonic() > deadline:
            raise AssertionError(f"ntfy.sh 缓存回放未在 {REPLAY_TIMEOUT}s 内出现（期望 {want} 条，拿到 {len(got)} 条），"
                                 "是服务端条件不是代码错")
        time.sleep(REPLAY_INTERVAL)


def live_messages(sub, want):
    """从一条已收到 open 事件的实时流里读 want 条 message 事件（其余事件跳过），读完由调用方 close()。"""
    got = []
    while len(got) < want:
        ev = next(sub)
        if ev["event"] == "message":
            got.append(ev)
    return got


class CannedClient(NtfyClient):
    """服务端替身：不管发什么都返回预置的 JSON——用来验证「响应对不上」这一支的行为。"""

    def __init__(self, body: dict):
        super().__init__()
        self.body = body

    def _open(self, req, timeout):
        return io.BytesIO(json.dumps(self.body).encode("utf-8"))


class LocalValidationTest(unittest.TestCase):
    """不碰网络的部分。"""

    def setUp(self):
        self.client = NoNetworkClient()

    # topic 名为空 / 非法字符 → 响亮失败
    def test_rejects_empty_or_illegal_topic(self):
        for bad in ("", "has space", "a/b", "中文", "a" * 65, "x?poll=1"):
            with self.subTest(topic=bad):
                with self.assertRaises(NtfyError):
                    self.client.publish(bad, "正文")
                with self.assertRaises(NtfyError):
                    self.client.update(bad, "seq1", "正文")
                with self.assertRaises(NtfyError):
                    self.client.clear(bad, "seq1")
                with self.assertRaises(NtfyError):
                    list(self.client.subscribe(["okay-topic", bad]))
        # sequence ID 进 URL 路径，同样不能带路径字符，否则 "x/clear" 会把更新变成清除
        with self.assertRaises(NtfyError):
            self.client.update("okay-topic", "x/clear", "正文")
        with self.assertRaises(NtfyError):
            self.client.clear("okay-topic", "")

    # actions 超过 3 个 → 本地拦下，不发请求
    def test_more_than_three_actions_rejected_locally(self):
        url = self.client.topic_url("okay-topic")
        four = [http_action(f"选项{i}", url, f"body{i}") for i in range(4)]
        with self.assertRaises(NtfyError) as cm:
            self.client.publish("okay-topic", "正文", actions=four)
        self.assertIn("3", str(cm.exception))
        # 形态不对的按钮也在本地拦
        with self.assertRaises(NtfyError):
            self.client.publish("okay-topic", "正文", actions=[{"label": "缺 action 字段"}])

    # 正文超过 4096 字节会被 ntfy 转成附件（docs.ntfy.sh/publish → Limitations），本地拦下
    def test_message_over_limit_rejected_locally(self):
        with self.assertRaises(NtfyError):
            self.client.publish("okay-topic", "字" * 1366)  # 1366 × 3 字节 = 4098
        with self.assertRaises(NtfyError):
            self.client.update("okay-topic", "seq1", "字" * 1366)

    # 服务端消息对象必带 topic，而 topic 名就是密码：响应对不上时的异常文本不能把它带出去
    def test_error_messages_do_not_leak_topic(self):
        secret = "secret-topic-do-not-log"
        canned = {"id": "m1", "time": 1, "event": "message", "topic": secret, "message": "x"}
        button = http_action("采纳推荐", f"https://ntfy.sh/{secret}", "留固定目录")
        with self.assertRaises(NtfyError) as cm:
            CannedClient(canned).publish(secret, "x", actions=[button])  # 按钮没回显
        self.assertNotIn(secret, str(cm.exception))
        with self.assertRaises(NtfyError) as cm:
            CannedClient(canned).update(secret, "seq-other", "x")  # sequence_id 没挂上
        self.assertNotIn(secret, str(cm.exception))
        with self.assertRaises(NtfyError) as cm:
            CannedClient(canned).clear(secret, "m1")  # 不是 message_clear
        self.assertNotIn(secret, str(cm.exception))
        # 参数校验这一支同样不能回显：topic 尾带换行被拒时、按钮缺字段但 url 里带着 topic 时
        with self.assertRaises(NtfyError) as cm:
            NoNetworkClient().publish(secret + "\n", "x")
        self.assertNotIn(secret, str(cm.exception))
        with self.assertRaises(NtfyError) as cm:
            NoNetworkClient().publish("okay-topic", "x", actions=[{"label": "缺 action", "url": f"https://ntfy.sh/{secret}"}])
        self.assertNotIn(secret, str(cm.exception))

    # 没有按钮的首发也要能识破「JSON 被当纯文本」：标题回显不一致、topic 对不上、被转成附件，都响亮失败
    def test_publish_guards_beyond_action_count(self):
        ok = {"id": "m1", "time": 1, "event": "message", "topic": "t", "message": "x", "title": "A"}
        self.assertEqual(CannedClient(ok).publish("t", "x", title="A")["id"], "m1")
        self.assertEqual(CannedClient({**ok, "title": " A "}).publish("t", "x", title="A")["id"], "m1")  # 首尾空白不算偏差
        for bad in ({**ok, "title": "B"}, {**ok, "topic": "other"}, {**ok, "attachment": {"name": "x.txt"}}):
            with self.subTest(bad=bad):
                with self.assertRaises(NtfyError):
                    CannedClient(bad).publish("t", "x", title="A")

    # clear 的返回也要挂在同一个 sequence 上，否则清掉的是别的通知
    def test_clear_checks_sequence_id(self):
        wrong = {"id": "c1", "time": 1, "event": "message_clear", "topic": "t", "sequence_id": "other"}
        with self.assertRaises(NtfyError):
            CannedClient(wrong).clear("t", "m1")
        self.assertEqual(CannedClient({**wrong, "sequence_id": "m1"}).clear("t", "m1")["event"], "message_clear")

    # 禁止使用 DELETE：实测服务端缓存不清、客户端也忽略，两侧都不生效
    def test_source_has_no_delete_endpoint(self):
        src = inspect.getsource(ntfyclient)
        self.assertIsNone(re.search(r"""["']DELETE["']""", src), "源码里出现了 DELETE 方法字面量")
        self.assertFalse(hasattr(NtfyClient, "delete"))


class ClosingStreamHandler(http.server.BaseHTTPRequestHandler):
    """本机 ntfy 替身：按 chunked 吐一条 open 事件，然后按 mode 收尾——
    graceful 发终止块后关连接 · abrupt 不发终止块直接断 · hang 挂着不发任何东西（模拟等 keepalive 的空档）·
    bad-utf8 再吐一行不是 UTF-8 的字节。"""

    protocol_version = "HTTP/1.1"  # 与 ntfy.sh 一致；chunked 本来也只属于 1.1
    mode = "graceful"
    release = threading.Event()  # hang 模式下由测试在清理时放行，别让服务端线程一直挂着

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        line = b'{"id":"x1","time":1,"event":"open","topic":"t"}\n'
        try:
            self.wfile.write(b"%x\r\n%s\r\n" % (len(line), line))
            self.wfile.flush()
            if self.mode == "hang":
                self.release.wait(10)
            if self.mode == "bad-utf8":
                bad = b"\xff\xfe not utf-8\n"
                self.wfile.write(b"%x\r\n%s\r\n" % (len(bad), bad))
            if self.mode != "abrupt":
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
        except OSError:
            pass  # 客户端先断了，属预期
        self.close_connection = True

    def log_message(self, format, *args):  # 静音，别把请求日志打进测试输出
        pass


class RecordingPublishHandler(http.server.BaseHTTPRequestHandler):
    """本机 ntfy 替身的发布端：记下每个 POST 的路径 / 头 / body，按 ntfy 的形态回显（首发 JSON 按字段回显；
    更新纯文本从 URL 取 sequence_id、从 Title 头解 RFC 2047）——让 publish() / update() 走真实的 HTTP 路径到达这里。"""

    protocol_version = "HTTP/1.1"
    requests: list[dict] = []

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.requests.append({"path": self.path, "headers": dict(self.headers), "body": raw})
        if self.headers.get("Content-Type", "").startswith("application/json"):
            body = json.loads(raw.decode("utf-8"))
            echo = {"id": "m1", "time": 1, "event": "message", **body}
        else:
            topic, seq = self.path.rsplit("/", 2)[-2:]
            echo = {"id": "m2", "time": 1, "event": "message", "topic": topic, "sequence_id": seq, "message": raw.decode("utf-8")}
            if self.headers.get("Title"):
                text, charset = email.header.decode_header(self.headers["Title"])[0]
                echo["title"] = text.decode(charset or "ascii") if isinstance(text, bytes) else text
        data = json.dumps(echo).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        self.close_connection = True

    def log_message(self, format, *args):
        pass


class LocalStreamTest(unittest.TestCase):
    """订阅流的断线形态：ntfy.sh 上没法按需制造，用本机 HTTP 替身跑真实的 http.client 路径。发布 / 更新的请求形态同样在这里验。"""

    def serve_publish(self):
        RecordingPublishHandler.requests = []
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), RecordingPublishHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return NtfyClient(f"http://127.0.0.1:{server.server_port}")

    # 所有卡片都是 Markdown：首发 JSON 带 markdown: true，无开关
    def test_publish_sends_markdown_flag(self):
        client = self.serve_publish()
        button = http_action("采纳推荐", client.topic_url("t"), "留固定目录")
        resp = client.publish("t", "**【正在做】** x", title="标题", actions=[button])
        self.assertEqual(resp["id"], "m1")
        req = RecordingPublishHandler.requests[-1]
        self.assertEqual(req["path"], "/")
        body = json.loads(req["body"].decode("utf-8"))
        self.assertIs(body["markdown"], True)
        self.assertEqual(body["message"], "**【正在做】** x")
        self.assertEqual(body["title"], "标题")
        self.assertEqual(len(body["actions"]), 1)

    # 更新端点是纯文本 body，Markdown 靠请求头 Markdown: yes 开启
    def test_update_sends_markdown_header(self):
        client = self.serve_publish()
        resp = client.update("t", "seq1", "**【你的回复】** 留固定目录", title=CHINESE_TITLE)
        self.assertEqual(resp["sequence_id"], "seq1")
        req = RecordingPublishHandler.requests[-1]
        self.assertEqual(req["path"], "/t/seq1")
        self.assertEqual(req["headers"].get("Markdown"), "yes")
        self.assertTrue(req["headers"]["Content-Type"].startswith("text/plain"))
        self.assertEqual(req["body"].decode("utf-8"), "**【你的回复】** 留固定目录")

    def serve(self, mode, stream_timeout=5):
        ClosingStreamHandler.mode = mode
        ClosingStreamHandler.release.clear()
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ClosingStreamHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.addCleanup(ClosingStreamHandler.release.set)
        return NtfyClient(f"http://127.0.0.1:{server.server_port}", stream_timeout=stream_timeout)

    # 长连接被对端关掉（不论有没有终止块）都必须响亮失败，否则 daemon 会静静地等一条永远不会来的回复
    def test_live_stream_closed_by_server_raises(self):
        for mode in ("graceful", "abrupt"):
            with self.subTest(mode=mode):
                client = self.serve(mode)
                stream = client.subscribe(["t"])
                self.assertEqual(next(stream)["event"], "open")
                with self.assertRaises(NtfyError):
                    next(stream)

    # daemon 要能从别的线程收掉一条正阻塞在 readline 的订阅：1 秒内结束，且抛的是 NtfyClosed（区分于网络断开）
    def test_close_from_another_thread_unblocks_within_one_second(self):
        client = self.serve("hang")
        sub = client.subscribe(["t"])
        self.assertEqual(next(sub)["event"], "open")
        outcome = {}

        def reader():
            started = time.monotonic()
            try:
                outcome["event"] = next(sub)
            except NtfyError as e:
                outcome["exc"] = e
            outcome["elapsed"] = time.monotonic() - started

        thread = threading.Thread(target=reader)
        thread.start()
        time.sleep(0.3)  # 让 reader 真的阻塞在 readline 上
        sub.close()
        thread.join(5)
        self.assertFalse(thread.is_alive(), "reader 线程 5 秒内没结束")
        self.assertIsInstance(outcome.get("exc"), NtfyClosed, outcome)
        self.assertLess(outcome["elapsed"], 1.0)
        with self.assertRaises(NtfyClosed):
            next(sub)  # 关掉之后再迭代也是 NtfyClosed，不是 StopIteration

    # Windows 上 shutdown(SHUT_RDWR) 叫不醒阻塞在 recv 的读线程（实测：close() 等满了 stream_timeout 才返回）——只有把句柄
    # 真关掉它才醒。三平台都能跑的替身：读线程等在 socketpair 的一端，shutdown 是空操作，detach 出去的另一端被真关才让 recv 抛。
    # 验的是 close() 的顺序（shutdown → 等不到锁 → detach 出真句柄关掉 → 读线程以 NtfyClosed 结束），不是 Windows 的
    # closesocket 能否叫醒 select——那由上面那条真 socket 用例在 Windows CI 上验
    def test_close_really_closes_the_handle_when_shutdown_does_not_wake_the_reader(self):
        class StubbornSocket:
            def __init__(self):
                self.ours, self.reader_end = socket.socketpair()
                self.shutdown_calls, self.detached = 0, False

            def shutdown(self, how):
                self.shutdown_calls += 1  # 什么都不做：读线程醒不了

            def detach(self):
                self.detached = True
                return self.ours.detach()  # 交出真句柄；调用方把它关掉，对端的 recv 才会返回

            def close(self):
                pass  # 真实语义：响应对象还持着 makefile 的引用，close() 只减引用、关不掉句柄——读线程照样醒不了

        class StubbornResponse:
            in_recv = threading.Event()  # 读线程已握着 _lock、正要进 recv：测试等到这一刻才 close()，别撞上它还没拿锁

            def __init__(self, end):
                self.end, self.closed = end, False
                end.settimeout(5)

            def readline(self):
                self.in_recv.set()
                if not self.end.recv(1):
                    raise OSError(10038, "An operation was attempted on something that is not a socket")  # 句柄被关后 Windows 给的那个
                return b"{}\n"

            def close(self):
                self.closed = True
                self.end.close()

        sock, conn = StubbornSocket(), mock.Mock()
        resp = StubbornResponse(sock.reader_end)
        self.addCleanup(sock.ours.close)  # detach 过就是空操作；没 detach（实现退化）才由这里收掉
        self.addCleanup(resp.close)
        sub = ntfyclient.Subscription(conn, sock, resp, poll=False)  # type: ignore[arg-type]  # 替身按鸭子类型
        outcome = {}

        def reader():
            try:
                outcome["event"] = next(sub)
            except NtfyError as e:
                outcome["exc"] = e

        thread = threading.Thread(target=reader)
        thread.start()
        self.assertTrue(resp.in_recv.wait(5), "reader 没进到 readline")
        started = time.monotonic()
        sub.close()
        close_elapsed = time.monotonic() - started
        thread.join(6)
        self.assertFalse(thread.is_alive(), "reader 线程没结束")
        self.assertLess(close_elapsed, 1.0, "close() 等到了读线程自己超时")
        self.assertTrue(sock.detached, "shutdown 没叫醒读线程就该 detach 出真句柄去关，_sock.close() 关不掉（响应对象还持着引用）")
        self.assertIsInstance(outcome.get("exc"), NtfyClosed, outcome)  # 句柄是自己关的 ⇒ 是 NtfyClosed，不是「断线」
        self.assertEqual(sock.shutdown_calls, 1)  # 先 shutdown（POSIX 上这一下就够）
        self.assertTrue(resp.closed)
        conn.close.assert_called_once()
        with self.assertRaises(NtfyClosed):
            next(sub)

    # stream_timeout 内一个字节都没来 ⇒ 当连接死了：抛的是 NtfyError 而不是 NtfyClosed，耗时约等于 stream_timeout
    def test_stream_timeout_raises_disconnect_not_closed(self):
        client = self.serve("hang", stream_timeout=1)
        sub = client.subscribe(["t"])
        self.assertEqual(next(sub)["event"], "open")
        started = time.monotonic()
        with self.assertRaises(NtfyError) as cm:
            next(sub)
        elapsed = time.monotonic() - started
        self.assertNotIsInstance(cm.exception, NtfyClosed)
        self.assertGreater(elapsed, 0.8)
        self.assertLess(elapsed, 3.0)

    # 流里混进不是 UTF-8 的字节：响亮失败且是 NtfyError 族（裸 UnicodeDecodeError 调用方接不住）
    def test_non_utf8_line_raises_ntfy_error(self):
        client = self.serve("bad-utf8")
        sub = client.subscribe(["t"])
        self.assertEqual(next(sub)["event"], "open")
        with self.assertRaises(NtfyError) as cm:
            next(sub)
        self.assertNotIsInstance(cm.exception, NtfyClosed)

    # with 块退出即 close()
    def test_subscription_is_a_context_manager(self):
        client = self.serve("hang")
        with client.subscribe(["t"]) as sub:
            self.assertEqual(next(sub)["event"], "open")
        with self.assertRaises(NtfyClosed):
            next(sub)

    # poll 模式下服务端回放完就关流，这是预期的正常结束
    def test_poll_stream_ends_normally(self):
        client = self.serve("graceful")
        events = list(client.subscribe(["t"], poll=True))
        self.assertEqual([e["event"] for e in events], ["open"])


@unittest.skipIf(os.environ.get("AGENT_NTFY_OFFLINE") == "1", "AGENT_NTFY_OFFLINE=1：不打真 ntfy.sh（CI / 省配额）")
class RealNtfyTest(unittest.TestCase):
    """对真实 ntfy.sh 跑的部分（每次约 16 条配额；ntfy.sh 限 250 条/天/IP）。"""

    @classmethod
    def setUpClass(cls):
        cls.client = NtfyClient()

    # 发布带自定义 sequence ID：POST /<topic>/<seq>，返回的 sequence_id 等于传入值
    def test_post_to_sequence_path_echoes_custom_sequence_id(self):
        topic = topic_for("custom-seq")
        r = self.client.update(topic, "my-seq-001", "首发正文")
        self.assertEqual(r["sequence_id"], "my-seq-001")
        self.assertEqual(r["event"], "message")
        self.assertEqual(r["topic"], topic)
        self.assertNotEqual(r["id"], "my-seq-001")  # 消息 id 仍是服务端生成的

    # 发布带 actions → 返回里 actions 回显（判据不是 HTTP 200：JSON 被当纯文本发出去时也返回 200）
    def test_publish_echoes_actions(self):
        topic = topic_for("actions")
        button = http_action("采纳推荐", self.client.topic_url(topic), "留固定目录")
        r = self.client.publish(topic, "正文", title="带按钮的提问", actions=[button])
        self.assertEqual(len(r["actions"]), 1)
        self.assertEqual(r["actions"][0]["label"], "采纳推荐")
        self.assertEqual(r["actions"][0]["body"], "留固定目录")
        self.assertEqual(r["actions"][0]["url"], self.client.topic_url(topic))
        self.assertEqual(r["title"], "带按钮的提问")
        self.assertEqual(r["message"], "正文")

    # 发布侧不认 http(s)_proxy：env 里指一个没人听的代理端口，发布照样成功（认了就是连接被拒）
    def test_publish_ignores_proxy_env(self):
        topic = topic_for("no-proxy")
        dead = "http://127.0.0.1:9"
        with mock.patch.dict(os.environ, {"https_proxy": dead, "HTTPS_PROXY": dead, "http_proxy": dead}):
            r = NtfyClient().publish(topic, "不走代理")
        self.assertEqual(r["topic"], topic)

    # 中文标题（Title 头）正确回显——Python http.client 强制把 header 编成 latin-1，此用例是回归防线
    def test_update_chinese_title_roundtrip(self):
        topic = topic_for("cn-title")
        r = self.client.update(topic, "seq-cn", "【你的回复】留固定目录", title=CHINESE_TITLE)
        self.assertEqual(r["title"], CHINESE_TITLE)
        self.assertEqual(r["message"], "【你的回复】留固定目录")

    # 同 sequence ID 再发一次 → 返回 sequence_id 指向原消息（更新语义）
    def test_update_same_sequence_points_to_original_message(self):
        topic = topic_for("update")
        first = self.client.publish(topic, "提问", title="原标题")
        second = self.client.update(topic, first["id"], "已回复", title="✅ 已回复 · 原标题")
        self.assertEqual(second["sequence_id"], first["id"])
        self.assertNotEqual(second["id"], first["id"])
        self.assertNotIn("actions", second)

    # PUT /<topic>/<seq>/clear → 返回 event=message_clear
    def test_clear_returns_message_clear_event(self):
        topic = topic_for("clear")
        first = self.client.publish(topic, "要被清掉的通知")
        r = self.client.clear(topic, first["id"])
        self.assertEqual(r["event"], "message_clear")
        self.assertEqual(r["sequence_id"], first["id"])

    # 订阅 ?since=<消息 id> → 不含锚点本身（exclusive）
    def test_subscribe_since_message_id_is_exclusive(self):
        topic = topic_for("since")
        m1 = self.client.publish(topic, "第一条")
        m2 = self.client.publish(topic, "第二条")
        got = messages(self.client, [topic], m1["id"], want=1)
        self.assertEqual([e["id"] for e in got], [m2["id"]])

    # 单连接订阅多 topic（逗号分隔），返回的消息带 topic 字段可用于路由——走实时流（daemon 的真实路径）
    def test_subscribe_multiple_topics_routes_by_topic_field(self):
        ta, tb = topic_for("multi-a"), topic_for("multi-b")
        with self.client.subscribe([ta, tb]) as sub:
            self.assertEqual(next(sub)["event"], "open")
            ma = self.client.publish(ta, "给 a")
            mb = self.client.publish(tb, "给 b")
            got = live_messages(sub, want=2)
        by_id = {e["id"]: e["topic"] for e in got}
        self.assertEqual(by_id, {ma["id"]: ta, mb["id"]: tb})

    # 订阅列表里含不存在的 topic → 不影响整条连接——走实时流
    def test_subscribe_with_unknown_topic_still_delivers(self):
        ta = topic_for("known")
        with self.client.subscribe([ta, topic_for("never-published")]) as sub:
            self.assertEqual(next(sub)["event"], "open")
            ma = self.client.publish(ta, "只有这个 topic 有消息")
            got = live_messages(sub, want=1)
        self.assertEqual([e["id"] for e in got], [ma["id"]])

    # 网络中断后重连，带上次消费的消息 id → 不重放已处理消息
    def test_reconnect_with_last_consumed_id_does_not_replay(self):
        topic = topic_for("reconnect")
        # 第一段：真实的长连接流，收到 open 事件后再发布，确认消息经流到达
        stream = self.client.subscribe([topic])
        self.assertEqual(next(stream)["event"], "open")
        m1 = self.client.publish(topic, "断线前消费掉的")
        ev = next(e for e in stream if e["event"] == "message")
        self.assertEqual(ev["id"], m1["id"])
        stream.close()  # 模拟网络中断：主动断开连接
        # 断线期间又来了两条
        m2 = self.client.publish(topic, "断线期间第一条")
        m3 = self.client.publish(topic, "断线期间第二条")
        # 重连：带上最后消费的 id，只该拿到断线期间的两条，m1 不重放
        got = messages(self.client, [topic], m1["id"], want=2)
        self.assertEqual([e["id"] for e in got], [m2["id"], m3["id"]])


if __name__ == "__main__":
    unittest.main()
