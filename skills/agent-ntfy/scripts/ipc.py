"""CLI（客户端）与 daemon（监听端）之间的 IPC 传输：unix domain socket 一种、回环 TCP 一种，
由 transport() 选定。

AGENT_NTFY_IPC 只在这里读，代码库别处不读这个环境变量。取值 "unix" / "tcp" 原样使用；未设或
空串按平台给缺省（Windows 上 "tcp"，因为 socket.AF_UNIX 在那不存在；其余平台 "unix"）；别的值
raise ValueError，不静默回退。

端点文件放在调用方给的 home 目录下：unix 传输是 daemon.sock，tcp 传输是 daemon.port（两行：
绑定的端口 + 一个 hex token）。两者都以 0600 写（与 daemon 其它私有文件同一语义）；Windows 上
mode 位不起作用，安全性靠目录 ACL。

listen() 把已存在的端点文件当成上次没清理干净的残骸——除非对它发起一次连接真的连上了，那种
情况下 raise AlreadyRunning 而不是抢占端点。daemon.port 解析不了同样按残骸处理。

tcp 传输在 socket 层不认证（只监听回环地址，但本机其它进程仍能连上），所以每个请求都带一个从
daemon.port 读出的 token：客户端侧 stamp() 加上它，daemon 侧 authenticate() 校验它。unix 传输
靠文件系统权限，不用 token（token_of() 返回 None，stamp() 原样返回）。

probe() 是静默探活：任何失败（没有端点、连接被拒、超时、回复解析不了）都报告为 None，不写
stderr——调用方拿它做分支，不是当错误处理。
"""

import errno
import json
import os
import secrets
import socket
import sys
from collections.abc import Callable
from pathlib import Path

import platform_

TRANSPORTS = ("unix", "tcp")
ENV_VAR = "AGENT_NTFY_IPC"
PROBE_TIMEOUT = 1.0
AF_UNIX: int | None = getattr(socket, "AF_UNIX", None)  # Windows 的 socket 模块没有这个常量：unix 传输在那不可用


class BadTransport(ValueError):
    """AGENT_NTFY_IPC 的值不可用：不是 unix / tcp，或本平台没有 unix 传输。.value 是那个值。"""

    def __init__(self, value: str, why: str):
        super().__init__(f"{ENV_VAR}={value!r} {why}")  # 会被嵌进按语言取的文案里：保持 ASCII
        self.value = value


class AlreadyRunning(OSError):
    """监听端点上已有一个活着的实例（能 connect 上）。.path 是那个 sock / port 文件。"""

    def __init__(self, path: Path):
        super().__init__(f"another instance is listening on {path}")  # 程序间的异常文案不进 texts 表，保持 ASCII
        self.path = path


def _platform() -> str:
    return sys.platform


def transport(home: Path | None = None) -> str:
    value = os.environ.get(ENV_VAR, "")
    if value:
        if value not in TRANSPORTS:
            raise BadTransport(value, f"is not one of {TRANSPORTS}")
        if value == "unix" and _platform() == "win32":
            raise BadTransport(value, "is not available on Windows (no AF_UNIX); use tcp")
        return value
    return "tcp" if _platform() == "win32" else "unix"


def _unix_socket() -> socket.socket:
    assert AF_UNIX is not None, "unix transport on a platform without AF_UNIX"  # transport() 已把这种组合拦在前面
    return socket.socket(AF_UNIX, socket.SOCK_STREAM)


def sock_path(home: Path) -> Path:
    return home / "daemon.sock"


def port_path(home: Path) -> Path:
    return home / "daemon.port"


def endpoint_path(home: Path) -> Path:
    return port_path(home) if transport(home) == "tcp" else sock_path(home)


def _parse_port_file(path: Path) -> tuple[int, str] | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    non_empty = [line for line in lines if line]
    if len(non_empty) != 2:
        return None
    port_str, token = non_empty
    try:
        port = int(port_str)
    except ValueError:
        return None
    if not (1 <= port <= 65535):
        return None
    return port, token


def _write_port_file(path: Path, port: int, token: str) -> None:
    # 与 daemon 其它私有文件同一个 0600 语义（平台层负责：Windows 上 mode 无效、安全靠目录 ACL）。
    # 先写临时文件再 os.replace：读的一方永远看不到半个文件——空文件会被当成残骸，并发启动的第二个实例会把活实例的端点删掉
    tmp = path.with_name(path.name + ".tmp")
    fd = platform_.open_private(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(f"{port}\n{token}\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _listen_unix(home: Path) -> tuple[socket.socket, Callable[[], None]]:
    path = sock_path(home)
    if path.exists():
        # 文件在：要么另一个实例活着（能连上 ⇒ 拒绝），要么是上次没清干净的残骸（连不上 ⇒ 清掉重来）
        probe_sock = _unix_socket()
        try:
            probe_sock.settimeout(1.0)
            probe_sock.connect(str(path))
        except OSError:
            path.unlink()
        else:
            raise AlreadyRunning(path)
        finally:
            probe_sock.close()

    listener = _unix_socket()
    try:
        listener.bind(str(path))
        os.chmod(path, 0o600)
        listener.listen(16)
    except OSError:
        listener.close()
        raise
    listener.setblocking(False)

    def cleanup() -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    return listener, cleanup


def _listen_tcp(home: Path) -> tuple[socket.socket, Callable[[], None]]:
    path = port_path(home)
    if path.exists():
        parsed = _parse_port_file(path)
        if parsed is not None:
            port, _token = parsed
            probe_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                probe_sock.settimeout(1.0)
                probe_sock.connect(("127.0.0.1", port))
            except OSError:
                pass
            else:
                raise AlreadyRunning(path)
            finally:
                probe_sock.close()
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(("127.0.0.1", 0))
        listener.listen(16)
        port = listener.getsockname()[1]
        token = secrets.token_hex(16)
        _write_port_file(path, port, token)
    except OSError:
        listener.close()
        raise
    listener.setblocking(False)

    def cleanup() -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    return listener, cleanup


def listen(home: Path) -> tuple[socket.socket, Callable[[], None]]:
    return _listen_unix(home) if transport(home) == "unix" else _listen_tcp(home)


def connect(home: Path) -> socket.socket:
    if transport(home) == "unix":
        s = _unix_socket()
        try:
            s.connect(str(sock_path(home)))
        except OSError:
            s.close()
            raise
        return s

    path = port_path(home)
    parsed = _parse_port_file(path)
    if parsed is None:
        # 与 unix 传输连不上 sock 文件时同形（errno + 系统的英文 strerror + 路径）：CLI 会把 strerror 拼进按语言取的文案里
        raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), str(path))
    port, _token = parsed
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.connect(("127.0.0.1", port))
    except OSError:
        s.close()
        raise
    return s


def token_of(home: Path) -> str | None:
    if transport(home) != "tcp":
        return None
    parsed = _parse_port_file(port_path(home))
    return parsed[1] if parsed is not None else None


def stamp(req: dict, home: Path) -> dict:
    token = token_of(home)
    if token is None:
        return req
    return {**req, "token": token}


def authenticate(req: dict, expected: str | None) -> bool:
    if expected is None:
        return True
    token = req.get("token")
    return isinstance(token, str) and secrets.compare_digest(token, expected)


def wake_pair() -> tuple[socket.socket, socket.socket]:
    r, w = socket.socketpair()
    r.setblocking(False)
    return r, w


def probe(home: Path, timeout: float = PROBE_TIMEOUT) -> dict | None:
    try:
        s = connect(home)
    except OSError:
        return None
    try:
        s.settimeout(timeout)
        line = json.dumps(stamp({"cmd": "status"}, home), ensure_ascii=False) + "\n"
        s.sendall(line.encode("utf-8"))
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        first_line, _, _ = buf.partition(b"\n")
        ev = json.loads(first_line.decode("utf-8"))
    except (OSError, ValueError):
        return None
    finally:
        s.close()
    return ev if isinstance(ev, dict) and ev.get("event") == "status" else None
