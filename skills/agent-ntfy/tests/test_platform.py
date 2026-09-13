"""platform_ 模块的用例。

POSIX 分支跑真实调用（当前运行环境即 POSIX，不需要切平台）；win32 分支用
mock.patch.object(platform_, "_platform", return_value="win32") 切换目标平台，
子进程 / 信号安装 / 目录权限这些有副作用的调用一律用替身，不真起进程、不真装信号处理器。
"""

import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import platform_


class IsWindowsTests(unittest.TestCase):
    def test_win32_is_windows(self):
        with mock.patch.object(platform_, "_platform", return_value="win32"):
            self.assertTrue(platform_.is_windows())

    def test_darwin_is_not_windows(self):
        with mock.patch.object(platform_, "_platform", return_value="darwin"):
            self.assertFalse(platform_.is_windows())


class SpawnDetachedTests(unittest.TestCase):
    @staticmethod
    def _fake_popen(recorded: dict):
        class FakePopen:
            def __init__(self, argv, **kw):
                recorded["argv"] = argv
                recorded["kw"] = kw
                recorded["instance"] = self

        return FakePopen

    def test_posix_uses_start_new_session(self):
        recorded: dict = {}
        fake_popen = self._fake_popen(recorded)
        with mock.patch.object(platform_, "_platform", return_value="darwin"), \
                mock.patch.object(subprocess, "Popen", fake_popen):
            result = platform_.spawn_detached(["prog", "--flag"])
        self.assertIs(result, recorded["instance"])
        self.assertEqual(recorded["argv"], ["prog", "--flag"])
        kw = recorded["kw"]
        self.assertIs(kw["start_new_session"], True)
        self.assertEqual(kw["stdin"], subprocess.DEVNULL)
        self.assertEqual(kw["stdout"], subprocess.DEVNULL)
        self.assertEqual(kw["stderr"], subprocess.DEVNULL)
        self.assertNotIn("creationflags", kw)

    def test_win32_uses_creationflags_not_start_new_session(self):
        recorded: dict = {}
        fake_popen = self._fake_popen(recorded)
        with mock.patch.object(platform_, "_platform", return_value="win32"), \
                mock.patch.object(subprocess, "Popen", fake_popen):
            result = platform_.spawn_detached(["prog", "--flag"])
        self.assertIs(result, recorded["instance"])
        self.assertEqual(recorded["argv"], ["prog", "--flag"])
        kw = recorded["kw"]
        self.assertEqual(kw["creationflags"], 0x208)
        self.assertNotIn("start_new_session", kw)
        self.assertEqual(kw["stdin"], subprocess.DEVNULL)
        self.assertEqual(kw["stdout"], subprocess.DEVNULL)
        self.assertEqual(kw["stderr"], subprocess.DEVNULL)


@unittest.skipIf(sys.platform == "win32", "POSIX 权限位在 Windows 上没有对应物")
class RestrictPrivateDirPosixTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="platform-restrict-"))
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)

    def test_new_dir_gets_0700(self):
        target = self.base / "priv"
        with mock.patch.object(platform_, "_platform", return_value="darwin"):
            platform_.restrict_private_dir(target)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o700)

    def test_existing_loose_dir_gets_tightened(self):
        target = self.base / "priv"
        target.mkdir()
        os.chmod(target, 0o755)  # mkdir 的 mode 受 umask 影响，显式再设一次保证起点确实是 0o755
        with mock.patch.object(platform_, "_platform", return_value="darwin"):
            platform_.restrict_private_dir(target)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o700)

    def test_creates_missing_parents(self):
        target = self.base / "a" / "b" / "priv"
        with mock.patch.object(platform_, "_platform", return_value="darwin"):
            platform_.restrict_private_dir(target)
        self.assertTrue(target.is_dir())
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o700)


class RestrictPrivateDirWindowsTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="platform-restrict-win-"))
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)

    def test_py312_invokes_icacls_once(self):
        target = self.base / "priv"
        calls = []

        def fake_run(argv, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0)

        with mock.patch.object(platform_, "_platform", return_value="win32"), \
                mock.patch.object(platform_, "_python_version", return_value=(3, 12)):
            platform_.restrict_private_dir(target, run=fake_run)
        self.assertTrue(target.is_dir())
        self.assertEqual(len(calls), 1)
        argv = calls[0]
        self.assertEqual(argv[:4], ["icacls", str(target), "/inheritance:r", "/grant:r"])
        self.assertTrue(argv[4].endswith(":(OI)(CI)F"))

    def test_py312_nonzero_returncode_warns_and_does_not_raise(self):
        target = self.base / "priv"

        def fake_run(argv, **kw):
            return subprocess.CompletedProcess(argv, 1)

        with mock.patch.object(platform_, "_platform", return_value="win32"), \
                mock.patch.object(platform_, "_python_version", return_value=(3, 12)), \
                self.assertLogs("agent-ntfy.platform", "WARNING"):
            platform_.restrict_private_dir(target, run=fake_run)

    def test_py312_run_raises_oserror_warns_and_does_not_raise(self):
        target = self.base / "priv"

        def fake_run(argv, **kw):
            raise OSError("icacls not found")

        with mock.patch.object(platform_, "_platform", return_value="win32"), \
                mock.patch.object(platform_, "_python_version", return_value=(3, 12)), \
                self.assertLogs("agent-ntfy.platform", "WARNING"):
            platform_.restrict_private_dir(target, run=fake_run)

    def test_py313_skips_icacls(self):
        target = self.base / "priv"
        calls = []

        def fake_run(argv, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0)

        with mock.patch.object(platform_, "_platform", return_value="win32"), \
                mock.patch.object(platform_, "_python_version", return_value=(3, 13)):
            platform_.restrict_private_dir(target, run=fake_run)
        self.assertTrue(target.is_dir())
        self.assertEqual(calls, [])


@unittest.skipIf(sys.platform == "win32", "POSIX 权限位在 Windows 上没有对应物")
class OpenPrivateTests(unittest.TestCase):
    def test_new_file_mode_0600_and_content_roundtrips(self):
        with tempfile.TemporaryDirectory(prefix="platform-open-") as d:
            target = Path(d) / "secret"
            with mock.patch.object(platform_, "_platform", return_value="darwin"):
                fd = platform_.open_private(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write("hello")
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            self.assertEqual(target.read_text(encoding="utf-8"), "hello")


class StopSignalNamesTests(unittest.TestCase):
    def test_posix_names(self):
        with mock.patch.object(platform_, "_platform", return_value="darwin"):
            self.assertEqual(platform_.stop_signal_names(), ("SIGTERM", "SIGINT", "SIGHUP"))

    def test_windows_names(self):
        with mock.patch.object(platform_, "_platform", return_value="win32"):
            self.assertEqual(platform_.stop_signal_names(), ("SIGINT", "SIGBREAK"))


class InstallStopSignalsTests(unittest.TestCase):
    @staticmethod
    def _fn(*_a):
        pass

    @unittest.skipIf(sys.platform == "win32", "signal.SIGHUP 在 Windows 上不存在")
    def test_posix_installs_all_three(self):
        installed = []

        def fake_signal(sig, handler):
            installed.append(sig)

        with mock.patch.object(platform_, "_platform", return_value="darwin"), \
                mock.patch.object(signal, "signal", fake_signal):
            result = platform_.install_stop_signals(self._fn)
        sighup = getattr(signal, "SIGHUP")  # 按 Windows 平台分析时 signal 模块没有它；用例本身已 skipIf(win32)
        self.assertEqual(set(installed), {signal.SIGTERM, signal.SIGINT, sighup})
        self.assertEqual(set(result), {signal.SIGTERM, signal.SIGINT, sighup})

    def test_windows_names_install_only_the_signals_this_platform_has(self):
        installed = []

        def fake_signal(sig, handler):
            installed.append(sig)

        with mock.patch.object(platform_, "_platform", return_value="win32"), \
                mock.patch.object(signal, "signal", fake_signal):
            result = platform_.install_stop_signals(self._fn)
        expected = [signal.SIGINT] + ([getattr(signal, "SIGBREAK")] if hasattr(signal, "SIGBREAK") else [])  # SIGBREAK 只有真 Windows 有
        self.assertEqual(installed, expected)
        self.assertEqual(result, expected)

    def test_windows_names_with_sigbreak_present_installs_both(self):
        installed = []

        def fake_signal(sig, handler):
            installed.append(sig)

        with mock.patch.object(platform_, "_platform", return_value="win32"), \
                mock.patch.object(signal, "signal", fake_signal), \
                mock.patch.object(signal, "SIGBREAK", 21, create=True):
            result = platform_.install_stop_signals(self._fn)
        self.assertEqual(installed, [signal.SIGINT, 21])
        self.assertEqual(result, [signal.SIGINT, 21])


class Utf8StdioTests(unittest.TestCase):
    def test_windows_reconfigures_all_three_streams(self):
        fake_stdin, fake_stdout, fake_stderr = mock.Mock(), mock.Mock(), mock.Mock()
        with mock.patch.object(platform_, "_platform", return_value="win32"), \
                mock.patch.object(sys, "stdin", fake_stdin), \
                mock.patch.object(sys, "stdout", fake_stdout), \
                mock.patch.object(sys, "stderr", fake_stderr):
            platform_.utf8_stdio()
        for fake in (fake_stdin, fake_stdout, fake_stderr):
            fake.reconfigure.assert_called_once_with(encoding="utf-8", errors="replace")

    def test_windows_skips_stream_without_reconfigure(self):
        fake_stdin = mock.Mock(spec=[])
        fake_stdout, fake_stderr = mock.Mock(), mock.Mock()
        with mock.patch.object(platform_, "_platform", return_value="win32"), \
                mock.patch.object(sys, "stdin", fake_stdin), \
                mock.patch.object(sys, "stdout", fake_stdout), \
                mock.patch.object(sys, "stderr", fake_stderr):
            platform_.utf8_stdio()  # 不抛
        fake_stdout.reconfigure.assert_called_once_with(encoding="utf-8", errors="replace")
        fake_stderr.reconfigure.assert_called_once_with(encoding="utf-8", errors="replace")

    def test_windows_skips_none_stream(self):
        fake_stdout, fake_stderr = mock.Mock(), mock.Mock()
        with mock.patch.object(platform_, "_platform", return_value="win32"), \
                mock.patch.object(sys, "stdin", None), \
                mock.patch.object(sys, "stdout", fake_stdout), \
                mock.patch.object(sys, "stderr", fake_stderr):
            platform_.utf8_stdio()  # 不抛
        fake_stdout.reconfigure.assert_called_once_with(encoding="utf-8", errors="replace")
        fake_stderr.reconfigure.assert_called_once_with(encoding="utf-8", errors="replace")

    def test_posix_is_a_no_op(self):
        fake_stdout = mock.Mock()
        with mock.patch.object(platform_, "_platform", return_value="darwin"), \
                mock.patch.object(sys, "stdout", fake_stdout):
            platform_.utf8_stdio()
        fake_stdout.reconfigure.assert_not_called()


if __name__ == "__main__":
    unittest.main()
