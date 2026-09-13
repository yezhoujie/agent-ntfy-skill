"""ntfy 客户端层：发布 / 更新 / clear / 订阅四件事，只用标准库。

两条发布路径按用途分开，不要混用：
    首发  publish()  JSON POST 到根路径，topic / title / message / actions 全在 body 里，
                     标题天然不经过 HTTP header，也就没有 header 只能放 latin-1 的问题
    更新  update()   POST /<topic>/<seq>，纯文本 body + Title 头。同一个 sequence ID 再发一次，
                     客户端原地替换那条通知，按钮随之消失；首次出现的 sequence ID 就是首发
    清除  clear()    PUT /<topic>/<seq>/clear，通知栏那条自动消失，消息列表里的记录还在
发布与更新都无条件开 Markdown（首发 JSON 的 "markdown": true，更新的 Markdown: yes 头）：所有卡片的版式都是 Markdown，
不支持 Markdown 的客户端看到的是源码，版式只用源码也读得通的那几样（见 render）。

不提供删除：实测 ntfy 的删除端点两侧都不生效——服务端缓存里那条仍在（poll 查得到），
手机 app 完全忽略，唯一效果是往 topic 里多塞一条事件。要让通知不再是活的，用 update 覆盖再 clear。

订阅只交一个同步的事件迭代器（Subscription，逐条给出已解析的事件），不开线程、不用 asyncio，
断线（含对端关流）时抛 NtfyError 让调用方决定何时以 since=<最后消费的 id> 重连；
Subscription.close() 可以从别的线程调用，让阻塞中的迭代立刻以 NtfyClosed 结束。

环境变量: AGENT_NTFY_URL（换 ntfy 实例，默认 https://ntfy.sh）
"""

import base64
import http.client
import json
import os
import re
import socket
import string
import threading
import urllib.error
import urllib.parse
import urllib.request

import texts
from collections.abc import Iterable

BASE_URL = os.environ.get("AGENT_NTFY_URL", "https://ntfy.sh").rstrip("/")
TIMEOUT = 30  # 发布 / 更新 / clear 单次请求
# 订阅流两次读之间的最长等待。ntfy 每 45 秒发一条 keepalive 事件，超过这个时长一个字都没来就当连接死了
STREAM_TIMEOUT = 90
# 服务端硬顶：第 4 个按钮起返回 HTTP 400 code 40018；本地拦下，不该让它跑到服务端
MAX_ACTIONS = 3
# 正文超过这个字节数 ntfy 会把它转成附件（.txt）而不是当消息推送（docs.ntfy.sh/publish → Limitations）
MAX_MESSAGE_BYTES = 4096
# ntfy 对 topic 名的限制；sequence ID 同样进 URL 路径，用同一条规则拦住 "x/clear" 这类会改写端点的值
TOPIC_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class NtfyError(Exception):
    """可预期错误：参数非法、连不上、HTTP 非 2xx、响应对不上、订阅流断开。

    文案在 texts 表里（ntfy.<key>）：str(e) 是中文（日志用），text(lang) 按语言取——它会作为 {error} 进回执与 CLI 文案。
    """

    def __init__(self, key: str, **fmt):
        self.key, self.fmt = key, fmt
        super().__init__(self.text("zh"))

    def text(self, lang: str) -> str:
        return texts.t(f"ntfy.{self.key}", lang, **self.fmt)


class NtfyClosed(NtfyError):
    """订阅被本进程主动关掉（Subscription.close()），不是网络故障——调用方据此决定不重连。"""

    def __init__(self):
        super().__init__("closed")


class NtfyRateLimited(NtfyError):
    """HTTP 429。ntfy.sh 按来源 IP 限流：60 次请求的桶、每 5 秒补 1 次；每天 250 条消息（docs.ntfy.sh/publish → Limitations）。"""


def http_action(label: str, url: str, body: str, *, method: str = "POST") -> dict:
    """一个「点击后向 url 发 HTTP 请求」的按钮，按 ntfy JSON 发布的 actions 元素格式。

    把 url 指回同一个 topic（topic_url()），点击就等于用户往这个 topic 回了一条正文为 body 的消息。
    """
    if not label or not url:
        raise NtfyError("action.empty")
    return {"action": "http", "label": label, "url": url, "method": method, "body": body}


NAME_KINDS = {"topic": "name.kind.topic", "sequence ID": "name.kind.seq"}


def _check_name(kind: str, value: str) -> str:
    """topic 名就是密码：不合法时只报长度与违规类别，不回显它。kind 是 "topic" / "sequence ID"。"""
    kind_ref = texts.Ref(f"ntfy.{NAME_KINDS[kind]}")
    if not isinstance(value, str):
        raise NtfyError("name.not_string", kind=kind_ref, type=type(value).__name__)
    if TOPIC_RE.fullmatch(value):  # match() 的 $ 会放过末尾换行
        return value
    if not value:
        why = "empty"
    elif len(value) > 64:
        why = "too_long"
    elif any(c in string.whitespace for c in value):
        why = "whitespace"
    else:
        why = "illegal"
    raise NtfyError("name.invalid", kind=kind_ref, why=texts.Ref(f"ntfy.name.why.{why}"), n=len(value))


def _check_message(message: str) -> bytes:
    data = message.encode("utf-8")
    if not data:
        raise NtfyError("message.empty")
    if len(data) > MAX_MESSAGE_BYTES:
        raise NtfyError("message.too_long", n=len(data), limit=MAX_MESSAGE_BYTES)
    return data


def _check_actions(actions: Iterable[dict] | None) -> list[dict]:
    actions = list(actions or [])
    if len(actions) > MAX_ACTIONS:
        raise NtfyError("actions.too_many", n=len(actions), limit=MAX_ACTIONS)
    for a in actions:
        if not isinstance(a, dict):
            raise NtfyError("action.not_object", type=type(a).__name__)
        if not a.get("action") or not a.get("label"):
            # 只报字段名：url / body 里常带着 topic
            raise NtfyError("action.malformed", fields=sorted(a))
    return actions


def _header_value(text: str) -> str:
    """把标题放进 HTTP header。

    http.client 把 header 值强制按 latin-1 编码，中文直接 UnicodeEncodeError（实测崩过）。
    ntfy 官方支持把任意 header 按 RFC 2047 编码（=?UTF-8?B?<base64>?=），服务端解码后原样回显。
    ASCII 原样放，其余走编码。整段编成一个 encoded-word、不按 RFC 2047 的 75 字符拆分：
    ntfy 用 Go 的 mime.WordDecoder 解码，它不限单个 word 的长度（104 字符的中文标题实测原样回显）。
    """
    if "\r" in text or "\n" in text:
        raise NtfyError("title.newline")
    if text.isascii():
        return text
    return "=?UTF-8?B?" + base64.b64encode(text.encode("utf-8")).decode("ascii") + "?="


def _brief(resp: dict) -> dict:
    """异常消息里只带诊断要用的字段。服务端消息对象必带 topic，而 topic 名就是密码，不能随异常进日志。"""
    out = {k: resp.get(k) for k in ("id", "event", "sequence_id", "title") if k in resp}
    out["actions"] = len(resp.get("actions") or [])
    return out


def _same_text(sent: str | None, got) -> bool:
    """回显比对。两侧都去掉首尾空白再比：header 与 JSON 字段服务端可能 trim，这不是我们要抓的那种偏差。"""
    return (sent or "").strip() == (str(got) if got is not None else "").strip()


def _check_echo(resp: dict, topic: str, title: str | None, action_count: int) -> None:
    """发出去的和服务端记下的必须一致，不一致就是走错了端点或编码坏了——这两种失败 HTTP 都是 200。"""
    if resp.get("topic") != topic:
        raise NtfyError("echo.topic", brief=_brief(resp))
    if title and not _same_text(title, resp.get("title")):
        raise NtfyError("echo.title", sent=repr(title), got=repr(resp.get("title")))
    if len(resp.get("actions") or []) != action_count:
        raise NtfyError("echo.actions", got=len(resp.get("actions") or []), sent=action_count, brief=_brief(resp))
    if "attachment" in resp:
        raise NtfyError("echo.attachment", brief=_brief(resp))


class Subscription:
    """一条订阅流。迭代得到事件 dict；close() 从任何线程调用都能让阻塞中的迭代立刻结束。

    自己持有 HTTP 连接（不经 urlopen）是为了拿到 socket：另一个线程要收掉这条流，只有
    shutdown(SHUT_RDWR) 能把阻塞在 recv 里的读线程叫醒——单纯 close 文件对象不行，
    对一个正在执行的生成器调 close() 更是直接 ValueError。读线程醒来后看到 _closed 标记，
    抛 NtfyClosed 而不是「断线」，daemon 据此分辨是自己关的还是网络断了。
    响应与连接对象只在持有 _lock 时释放：读线程整个 readline 期间都握着锁，关流线程
    shutdown 之后再拿锁去释放，就不会撞上 http.client 内部把 fp 置 None 的那一刻
    （HTTPConnection.close() 会顺带 close 它持有的响应，与读线程并发时读线程会 AttributeError）。
    """

    def __init__(self, conn: http.client.HTTPConnection, sock: socket.socket, resp: http.client.HTTPResponse, poll: bool):
        self._conn = conn
        self._sock = sock  # 单独记一份：响应带 Connection: close 时 getresponse() 会把 conn.sock 置 None
        self._resp = resp
        self._poll = poll
        self._closed = False
        self._finished = False
        self._lock = threading.Lock()

    def __iter__(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def __next__(self) -> dict:
        with self._lock:
            if self._finished:
                if self._closed:
                    raise NtfyClosed()
                raise StopIteration
            while True:
                try:
                    raw = self._resp.readline()
                except (OSError, ValueError, http.client.HTTPException) as e:
                    self._release()
                    if self._closed:
                        raise NtfyClosed() from None
                    raise NtfyError("stream.broken", error=e) from e
                if not raw:
                    # 流到头了。poll 模式是回放完的正常结束；长连接则是断线——http.client 在 chunk 边界上
                    # 遇到连接丢失会当成 EOF 而不是报错（_peek_chunked 吞掉 IncompleteRead），不在这里补一刀，
                    # daemon 就会在某次静默结束后永远等一条不会来的回复，且没有任何报错
                    self._release()
                    if self._closed:
                        raise NtfyClosed()
                    if self._poll:
                        raise StopIteration
                    raise NtfyError("stream.eof")
                try:
                    line = raw.decode("utf-8").strip()
                except UnicodeDecodeError:
                    self._release()
                    raise NtfyError("stream.not_utf8", n=len(raw)) from None
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    self._release()
                    # 不带行内容：半截的 message 事件里就有 topic 名
                    raise NtfyError("stream.not_json", n=len(line)) from None
                if not isinstance(event, dict):
                    self._release()
                    raise NtfyError("stream.not_object", n=len(line))
                return event

    def close(self) -> None:
        """可从任何线程调用。先 shutdown 叫醒读线程，等它退出 readline 后再释放连接；重复调用无害。"""
        self._closed = True
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass  # 对端已经断了
        with self._lock:
            self._release()

    def _release(self) -> None:
        """只在持有 _lock 时调用；幂等。"""
        if self._finished:
            return
        self._finished = True
        try:
            self._resp.close()
        finally:
            self._conn.close()


class NtfyClient:
    def __init__(self, base_url: str = BASE_URL, *, timeout: float = TIMEOUT, stream_timeout: float = STREAM_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.stream_timeout = stream_timeout

    def topic_url(self, topic: str) -> str:
        """https://ntfy.sh/<topic>——按钮回传的目标，也是用户在 app 里订阅的地址。"""
        return f"{self.base_url}/{_check_name('topic', topic)}"

    # -------- 发布

    def publish(self, topic: str, message: str, *, title: str | None = None, actions: Iterable[dict] | None = None) -> dict:
        """首发：JSON POST 到根路径。返回服务端的消息对象，其中 id 就是之后更新 / 清除用的 sequence ID。

        判据是返回里 actions 有没有回显，不是 HTTP 200：JSON 若被当成纯文本发出去（比如发到了
        /<topic> 而不是根路径），服务端照样返回 200 带 id，整串 JSON 原文会被当正文推给用户，
        标题和按钮全丢——看起来完全成功（实测）。
        """
        _check_name("topic", topic)
        _check_message(message)
        actions = _check_actions(actions)
        body: dict = {"topic": topic, "message": message, "markdown": True}
        if title:
            body["title"] = title
        if actions:
            body["actions"] = actions
        req = urllib.request.Request(self.base_url, method="POST", data=json.dumps(body, ensure_ascii=False).encode("utf-8"))
        req.add_header("Content-Type", "application/json; charset=utf-8")
        resp = self._call(req)
        _check_echo(resp, topic, title, len(actions))
        return resp

    def update(self, topic: str, seq_id: str, message: str, *, title: str | None = None) -> dict:
        """更新：POST /<topic>/<seq>，纯文本 body。同 seq 已存在 ⇒ 客户端原地替换；不存在 ⇒ 就是首发。

        这个端点不解析 JSON——往这里发 JSON 会把整串原文当正文推给用户，且返回 200 带 id（实测）。
        所以正文只能是纯文本，标题走 Title 头（编码见 _header_value）；不带按钮，按钮只在 publish() 里有。
        """
        _check_name("topic", topic)
        _check_name("sequence ID", seq_id)
        data = _check_message(message)
        req = urllib.request.Request(f"{self.base_url}/{topic}/{seq_id}", method="POST", data=data)
        req.add_header("Content-Type", "text/plain; charset=utf-8")
        req.add_header("Markdown", "yes")  # 这个端点不解析 JSON，Markdown 只能靠头开
        if title:
            req.add_header("Title", _header_value(title))
        resp = self._call(req)
        if resp.get("sequence_id") != seq_id:
            raise NtfyError("update.seq_mismatch", seq=repr(seq_id), brief=_brief(resp))
        _check_echo(resp, topic, title, 0)
        return resp

    def clear(self, topic: str, seq_id: str) -> dict:
        """PUT /<topic>/<seq>/clear：让手机通知栏里那条消失。返回 event 为 message_clear 的事件对象。"""
        _check_name("topic", topic)
        _check_name("sequence ID", seq_id)
        req = urllib.request.Request(f"{self.base_url}/{topic}/{seq_id}/clear", method="PUT")
        resp = self._call(req)
        if resp.get("event") != "message_clear":
            raise NtfyError("clear.no_event", brief=_brief(resp))
        if resp.get("sequence_id") != seq_id:
            raise NtfyError("clear.seq_mismatch", seq=repr(seq_id), brief=_brief(resp))
        return resp

    # -------- 订阅

    def subscribe(self, topics: str | Iterable[str], *, since: str | None = None, poll: bool = False) -> Subscription:
        """订阅一个或多个 topic 的 JSON 流，返回逐条给出已解析事件（dict）的 Subscription。

        事件有 event 字段：open（连上了）/ keepalive / message / message_clear / message_delete……，
        message 事件带 id / time / topic / message，可能还有 title / actions / sequence_id；
        多 topic 时靠 topic 字段路由。
        since 取消息 id（回放该条之后的，不含它本身）、时长（10m）、Unix 时间戳或 all；
        不给就只收连上之后的新消息。poll=True 时服务端回放完缓存就关流，迭代随之正常结束。
        长连接（poll=False）只会因故障而结束：断线、超过 stream_timeout 没收到任何字节、
        或对端关掉了连接（不论有没有 chunked 终止块），一律抛 NtfyError——
        调用方据此以最后消费的 id 作 since 重连，已处理的消息不会重放。
        不想再收了就 Subscription.close()（任何线程都可以），正阻塞的迭代立刻以 NtfyClosed 结束。
        参数校验与建连在这里就做（不等第一次 next）。
        ⚠️ 服务端把消息写进缓存有延迟，实测从 1 秒到数分钟不等（同一天里两次测量分别约 1s 与约 3min）：
        发布后立刻 poll 可能还看不到刚发的那条；实时流的投递不受影响，始终即时。
        """
        names = [topics] if isinstance(topics, str) else list(topics)
        if not names:
            raise NtfyError("subscribe.empty")
        for t in names:
            _check_name("topic", t)
        path = f"/{','.join(names)}/json"
        params = {}
        if since:
            params["since"] = since
        if poll:
            params["poll"] = "1"
        if params:
            path += "?" + urllib.parse.urlencode(params)
        conn, sock, resp = self._connect(path, self.stream_timeout)
        return Subscription(conn, sock, resp, poll)

    # -------- 传输

    # ProxyHandler({}) 让发布这一路与订阅那一路（HTTPSConnection，天生不认代理）行为一致：
    # urlopen 默认会读 http(s)_proxy 环境变量，macOS 上还会读系统设置里的代理；
    # 发布认代理、订阅不认，就会出现「发得出、收不到」（或反过来），而两边各自都不报错
    _OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _open(self, req: urllib.request.Request, timeout: float):
        """发布 / 更新 / clear 的唯一出口（测试替身在这里拦）。不认任何代理。"""
        return self._OPENER.open(req, timeout=timeout)

    def _connect(self, path: str, timeout: float) -> tuple[http.client.HTTPConnection, socket.socket, http.client.HTTPResponse]:
        """订阅流的唯一出口：自己建 HTTP 连接，把 socket 握在手里（Subscription.close 要用）。

        timeout 同时是建连超时与两次读之间的最长等待——ntfy 每 45 秒一条 keepalive，
        stream_timeout 取它的两倍，超过就当连接死了。
        """
        u = urllib.parse.urlsplit(self.base_url)
        cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
        conn = cls(u.hostname or "", u.port, timeout=timeout)
        try:
            conn.request("GET", u.path + path, headers={"Accept": "application/x-ndjson"})
            sock = conn.sock  # 建连后立刻拿；getresponse() 之后它可能已经是 None
            if sock is None:
                raise NtfyError("connect.no_socket")
            resp = conn.getresponse()
            if resp.status != 200:
                body = resp.read().decode("utf-8", "replace")
                raise self._status_error(resp.status, resp.reason, body)
        except (OSError, http.client.HTTPException) as e:
            conn.close()
            raise NtfyError("connect.failed", url=self.base_url, error=e) from e
        except NtfyError:
            conn.close()
            raise
        return conn, sock, resp

    def _call(self, req: urllib.request.Request) -> dict:
        try:
            with self._open(req, self.timeout) as resp:
                payload = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            raise self._http_error(e) from e
        except (OSError, http.client.HTTPException) as e:
            raise NtfyError("connect.failed", url=self.base_url, error=e) from e
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as e:
            raise NtfyError("response.not_json", payload=repr(payload[:200])) from e
        if not isinstance(data, dict):
            raise NtfyError("response.not_object", payload=repr(payload[:200]))
        return data

    def _http_error(self, e: urllib.error.HTTPError) -> NtfyError:
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        return self._status_error(e.code, e.reason, body)

    def _status_error(self, status: int, reason: str, body: str) -> NtfyError:
        """ntfy 的错误体是 {"code": 40018, "http": 400, "error": "..."}；带上它的 code，排查时能对官方错误码表。"""
        detail: object
        try:
            err = json.loads(body)
            detail = texts.Ref("ntfy.http.ntfy_code", error=err.get("error", ""), code=err.get("code"))
        except Exception:
            detail = body[:200]
        msg = texts.Ref("ntfy.http.detail", status=status, reason=reason, detail=detail) if detail else texts.Ref("ntfy.http", status=status, reason=reason)
        if status == 429:
            return NtfyRateLimited("http.rate_limited", msg=msg)
        return NtfyError(msg.key.removeprefix("ntfy."), **msg.fmt)
