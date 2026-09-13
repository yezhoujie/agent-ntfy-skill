"""ipc 模块的单测：unix / tcp 两种传输用同一份用例（mixin）跑两遍，另单独测 transport() 的选择逻辑。"""

import contextlib
import io
import os
import secrets
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import ipc


class _IpcCases:
    """公共用例体，被下面两个具体 TestCase 子类各跑一遍（unix / tcp）。"""

    transport: str

    def setUp(self) -> None:
        assert isinstance(self, unittest.TestCase)
        patcher = mock.patch.dict(os.environ, {ipc.ENV_VAR: self.transport})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.home = Path(tempfile.mkdtemp(prefix="an-"))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)

    # ---- listen ----

    def test_listen_returns_nonblocking_listening_socket(self) -> None:
        assert isinstance(self, unittest.TestCase)
        sock, cleanup = ipc.listen(self.home)
        with self.assertRaises(BlockingIOError):
            sock.accept()
        if self.transport == "unix":
            path = ipc.sock_path(self.home)
            self.assertTrue(path.exists())
            if sys.platform != "win32":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        else:
            path = ipc.port_path(self.home)
            bound_port = sock.getsockname()[1]
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(int(lines[0]), bound_port)
            self.assertRegex(lines[1], r"^[0-9a-f]{32}$")
            if sys.platform != "win32":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        sock.close()
        cleanup()
        self.assertFalse(path.exists())
        cleanup()  # 幂等：文件已经不在也不报错

    # ---- connect ----

    def test_connect_reaches_listener(self) -> None:
        assert isinstance(self, unittest.TestCase)
        sock, cleanup = ipc.listen(self.home)
        self.addCleanup(cleanup)
        self.addCleanup(sock.close)
        client = ipc.connect(self.home)
        self.addCleanup(client.close)
        self.assertTrue(client.getblocking())
        sock.setblocking(True)
        try:
            conn, _addr = sock.accept()
        finally:
            sock.setblocking(False)
        conn.close()

    def test_connect_after_listener_gone_raises(self) -> None:
        assert isinstance(self, unittest.TestCase)
        sock, cleanup = ipc.listen(self.home)
        sock.close()
        cleanup()
        expected: type[OSError] = FileNotFoundError if self.transport == "tcp" else OSError
        with self.assertRaises(expected):
            ipc.connect(self.home)

    # ---- probe ----

    def test_probe_returns_status_event(self) -> None:
        assert isinstance(self, unittest.TestCase)
        listener, cleanup = ipc.listen(self.home)
        self.addCleanup(cleanup)
        listener.setblocking(True)
        self.addCleanup(listener.close)

        def fake_daemon() -> None:
            conn, _addr = listener.accept()
            with conn:
                buf = b""
                while b"\n" not in buf:
                    chunk = conn.recv(4096)
                    if not chunk:
                        return
                    buf += chunk
                conn.sendall(b'{"event":"status","pid":123}\n')

        t = threading.Thread(target=fake_daemon, daemon=True)
        t.start()
        try:
            result = ipc.probe(self.home)
        finally:
            t.join(timeout=2)
        self.assertFalse(t.is_alive())
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.get("pid"), 123)

    def test_probe_returns_none_on_non_status_event(self) -> None:
        assert isinstance(self, unittest.TestCase)
        listener, cleanup = ipc.listen(self.home)
        self.addCleanup(cleanup)
        listener.setblocking(True)
        self.addCleanup(listener.close)

        def fake_daemon() -> None:
            conn, _addr = listener.accept()
            with conn:
                buf = b""
                while b"\n" not in buf:
                    chunk = conn.recv(4096)
                    if not chunk:
                        return
                    buf += chunk
                conn.sendall(b'{"event":"error"}\n')

        t = threading.Thread(target=fake_daemon, daemon=True)
        t.start()
        try:
            result = ipc.probe(self.home)
        finally:
            t.join(timeout=2)
        self.assertFalse(t.is_alive())
        self.assertIsNone(result)

    def test_probe_times_out_when_no_response(self) -> None:
        assert isinstance(self, unittest.TestCase)
        listener, cleanup = ipc.listen(self.home)
        self.addCleanup(cleanup)
        self.addCleanup(listener.close)
        # 监听着（连接能进 backlog），但没有人 accept() 也没有人回话
        started = time.monotonic()
        result = ipc.probe(self.home, timeout=0.3)
        elapsed = time.monotonic() - started
        self.assertIsNone(result)
        self.assertLess(elapsed, 1.5)

    def test_probe_returns_none_without_endpoint_and_silent(self) -> None:
        assert isinstance(self, unittest.TestCase)
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            result = ipc.probe(self.home, timeout=0.3)
        self.assertIsNone(result)
        self.assertEqual(buf.getvalue(), "")

    # ---- 残骸端点文件 ----

    def test_listen_replaces_stale_endpoint_file(self) -> None:
        assert isinstance(self, unittest.TestCase)
        stale_port = None
        if self.transport == "unix":
            path = ipc.sock_path(self.home)
            path.touch()
        else:
            path = ipc.port_path(self.home)
            probe_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            probe_sock.bind(("127.0.0.1", 0))
            stale_port = probe_sock.getsockname()[1]
            probe_sock.close()
            path.write_text(f"{stale_port}\n{secrets.token_hex(16)}\n", encoding="utf-8")

        sock, cleanup = ipc.listen(self.home)
        self.addCleanup(cleanup)
        self.addCleanup(sock.close)
        self.assertTrue(path.exists())
        if self.transport == "tcp":
            new_port = int(path.read_text(encoding="utf-8").splitlines()[0])
            self.assertNotEqual(new_port, stale_port)

    # ---- 已有实例 ----

    def test_listen_again_raises_already_running(self) -> None:
        assert isinstance(self, unittest.TestCase)
        sock1, cleanup1 = ipc.listen(self.home)
        self.addCleanup(cleanup1)
        self.addCleanup(sock1.close)
        path = ipc.endpoint_path(self.home)
        # unix 的端点文件是个 socket，不能当普通文件 read_bytes()；用 inode + mtime 判断它没被换掉/重写
        before_stat = path.stat()

        with self.assertRaises(ipc.AlreadyRunning) as ctx:
            ipc.listen(self.home)
        self.assertEqual(ctx.exception.path, path)

        after_stat = path.stat()
        self.assertEqual(before_stat.st_ino, after_stat.st_ino)
        self.assertEqual(before_stat.st_mtime, after_stat.st_mtime)

    # ---- token 协议 ----

    def test_token_protocol(self) -> None:
        assert isinstance(self, unittest.TestCase)
        sock, cleanup = ipc.listen(self.home)
        self.addCleanup(cleanup)
        self.addCleanup(sock.close)

        if self.transport == "tcp":
            path = ipc.port_path(self.home)
            expected_token = path.read_text(encoding="utf-8").splitlines()[1]
            self.assertEqual(ipc.token_of(self.home), expected_token)

            original = {"cmd": "x"}
            stamped = ipc.stamp(original, self.home)
            self.assertEqual(stamped, {"cmd": "x", "token": expected_token})
            self.assertEqual(original, {"cmd": "x"})
            self.assertIsNot(stamped, original)

            self.assertTrue(ipc.authenticate(stamped, expected_token))
            self.assertFalse(ipc.authenticate({"cmd": "x"}, expected_token))
            self.assertFalse(ipc.authenticate(stamped, "wrong" + expected_token))
            self.assertFalse(ipc.authenticate({"cmd": "x", "token": 123}, expected_token))
        else:
            self.assertIsNone(ipc.token_of(self.home))
            req = {"cmd": "x"}
            self.assertIs(ipc.stamp(req, self.home), req)
            self.assertTrue(ipc.authenticate({"anything": True}, None))

    # ---- wake_pair ----

    def test_wake_pair(self) -> None:
        assert isinstance(self, unittest.TestCase)
        r, w = ipc.wake_pair()
        self.addCleanup(r.close)
        self.addCleanup(w.close)
        self.assertTrue(w.getblocking())
        w.send(b"x")
        self.assertEqual(r.recv(4096), b"x")
        with self.assertRaises(BlockingIOError):
            r.recv(4096)


@unittest.skipIf(sys.platform == "win32", "AF_UNIX sockets are not available on Windows")
class UnixIpcTest(_IpcCases, unittest.TestCase):
    transport = "unix"


class TcpIpcTest(_IpcCases, unittest.TestCase):
    transport = "tcp"

    def test_listen_succeeds_despite_garbage_port_file(self) -> None:
        with mock.patch.dict(os.environ, {ipc.ENV_VAR: "tcp"}):
            path = ipc.port_path(self.home)
            path.write_text("not a port file\nfoo\nbar\n", encoding="utf-8")
            sock, cleanup = ipc.listen(self.home)
            self.addCleanup(cleanup)
            self.addCleanup(sock.close)
            self.assertTrue(path.exists())


class TransportSelectionTest(unittest.TestCase):
    def test_env_unix_returned_as_is(self) -> None:
        with mock.patch.dict(os.environ, {ipc.ENV_VAR: "unix"}), mock.patch.object(ipc, "_platform", return_value="darwin"):  # 真 win32 会拒绝 unix
            self.assertEqual(ipc.transport(), "unix")

    def test_env_tcp_returned_as_is(self) -> None:
        with mock.patch.dict(os.environ, {ipc.ENV_VAR: "tcp"}):
            self.assertEqual(ipc.transport(), "tcp")

    def test_default_darwin_is_unix(self) -> None:
        with mock.patch.dict(os.environ, clear=False):
            os.environ.pop(ipc.ENV_VAR, None)
            with mock.patch("ipc._platform", return_value="darwin"):
                self.assertEqual(ipc.transport(), "unix")

    def test_default_linux_is_unix(self) -> None:
        with mock.patch.dict(os.environ, clear=False):
            os.environ.pop(ipc.ENV_VAR, None)
            with mock.patch("ipc._platform", return_value="linux"):
                self.assertEqual(ipc.transport(), "unix")

    def test_default_win32_is_tcp(self) -> None:
        with mock.patch.dict(os.environ, clear=False):
            os.environ.pop(ipc.ENV_VAR, None)
            with mock.patch("ipc._platform", return_value="win32"):
                self.assertEqual(ipc.transport(), "tcp")

    def test_empty_env_falls_back_to_platform_default(self) -> None:
        with mock.patch.dict(os.environ, {ipc.ENV_VAR: ""}):
            with mock.patch("ipc._platform", return_value="win32"):
                self.assertEqual(ipc.transport(), "tcp")

    def test_unix_on_windows_is_rejected_like_a_bad_value(self) -> None:
        with mock.patch.dict(os.environ, {ipc.ENV_VAR: "unix"}):
            with mock.patch("ipc._platform", return_value="win32"):
                with self.assertRaises(ipc.BadTransport) as cm:
                    ipc.transport()
        self.assertEqual(cm.exception.value, "unix")
        self.assertIsInstance(cm.exception, ValueError)
        self.assertIn("tcp", str(cm.exception))

    def test_bogus_env_raises_value_error(self) -> None:
        with mock.patch.dict(os.environ, {ipc.ENV_VAR: "bogus"}):
            with self.assertRaises(ValueError) as ctx:
                ipc.transport()
            message = str(ctx.exception)
            self.assertIn("bogus", message)
            self.assertIn("unix", message)
            self.assertIn("tcp", message)


if __name__ == "__main__":
    unittest.main()
