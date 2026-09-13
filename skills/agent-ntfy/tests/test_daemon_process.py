"""daemon 作为真子进程：起得来、探活能答、`--stop` / SIGTERM 后端点文件与 pid 文件删干净。

子进程不碰钥匙串（`AGENT_NTFY_STORE=file`，池子落在临时 home 里）、不出网（`AGENT_NTFY_URL` 指向本机一个没人听的端口，
订阅只会在后台一直重连）。每个用例的子进程都在 tearDown 里保证退出，不留孤儿。
"""

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import ipc
import state

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "agent_ntfy.py"
START_TIMEOUT = 15.0  # 子进程解释器起动 + 建池 + bind
STOP_TIMEOUT = 10.0


def wait_until(cond, timeout, what):
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            raise AssertionError(f"{timeout}s 内未满足：{what}")
        time.sleep(0.05)


@unittest.skipUnless(hasattr(state, "default_store"), "子进程 daemon 需要文件存储后端（AGENT_NTFY_STORE=file），否则会碰真钥匙串")
class DaemonProcessTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="an-")) / "h"  # 短路径：unix 传输下 socket 路径有长度上限
        self.addCleanup(shutil.rmtree, self.home.parent, ignore_errors=True)
        self.env = {k: v for k, v in os.environ.items() if k != "AGENT_NTFY_SMOKE"}
        self.env.update({"AGENT_NTFY_STORE": "file", "AGENT_NTFY_URL": "http://127.0.0.1:1", "AGENT_NTFY_LANG": "zh"})
        self.procs: list[subprocess.Popen] = []
        self.stderr_path = self.home.parent / "daemon.stderr"

    def tearDown(self):
        """不留孤儿：先经 IPC 停（三平台通用、也管 --detach 起的孙进程），再对自己起的子进程 terminate / kill 兜底，最后等 pid 文件消失。"""
        if ipc.probe(self.home, timeout=0.5) is not None:
            self.cli("daemon", "--stop")
        for p in self.procs:
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(STOP_TIMEOUT)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait(STOP_TIMEOUT)
        pid = self._pid_file()  # 还有 pid 文件：多半是不应答的孙进程，POSIX 上再发一次 SIGTERM
        if pid is not None and pid not in {p.pid for p in self.procs} and sys.platform != "win32":
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        try:
            wait_until(lambda: not (self.home / "daemon.pid").exists(), STOP_TIMEOUT, "daemon 退出并删 pid 文件")
        except AssertionError:
            # 兜底失败：下面的 rmtree 照做，但要在日志里看得见——留下的是一个真进程，得有人去 ps
            print(f"WARNING: daemon 子进程可能还活着（pid 文件 {self._pid_file()}，home {self.home}），请用 ps 复核并手动清理", file=sys.stderr)

    def _pid_file(self):
        try:
            return int((self.home / "daemon.pid").read_text().strip())
        except (OSError, ValueError):
            return None

    def start(self):
        """前台形态起一个 daemon 子进程，等到它在 IPC 上报出自己的 pid。"""
        with open(self.stderr_path, "ab") as err:
            p = subprocess.Popen([sys.executable, str(SCRIPT), "--home", str(self.home), "daemon"], env=self.env,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err)
        self.procs.append(p)
        wait_until(lambda: p.poll() is not None or (ipc.probe(self.home, timeout=0.5) or {}).get("pid") == p.pid, START_TIMEOUT, "子进程在 IPC 上应答")
        self.assertIsNone(p.poll(), f"daemon 子进程退出了：\n{self.stderr_path.read_text(encoding='utf-8', errors='replace')}")
        return p

    def cli(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), "--home", str(self.home), *args], env=self.env, capture_output=True, text=True, timeout=60)

    def assert_clean(self):
        self.assertFalse(ipc.endpoint_path(self.home).exists())
        self.assertFalse((self.home / "daemon.pid").exists())
        self.assertIsNone(ipc.probe(self.home, timeout=0.5))

    def test_status_and_stop_over_ipc(self):
        p = self.start()
        ev = ipc.probe(self.home)
        assert ev is not None
        self.assertEqual((ev["pid"], ev["transport"]), (p.pid, ipc.transport(self.home)))
        r = self.cli("daemon", "--status")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"pid {p.pid}", r.stdout)
        self.assertIn(ipc.transport(self.home), r.stdout)  # 行尾的传输类型
        r = self.cli("daemon", "--stop")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(str(p.pid), r.stdout)
        self.assertEqual(p.wait(STOP_TIMEOUT), 0)
        self.assert_clean()
        r = self.cli("daemon", "--status")
        self.assertEqual(r.returncode, 1)
        self.assertIn("未运行", r.stdout)

    def test_detach_then_stop(self):
        r = self.cli("daemon", "--detach")
        self.assertEqual(r.returncode, 0, r.stderr)
        pid = self._pid_file()
        self.assertIsNotNone(pid)
        assert pid is not None
        self.assertIn(f"pid {pid}", r.stdout)
        ev = ipc.probe(self.home)
        assert ev is not None
        self.assertEqual(ev["pid"], pid)
        r = self.cli("daemon", "--detach")  # 已在跑：不起第二个
        self.assertEqual(r.returncode, 3)
        r = self.cli("daemon", "--stop")
        self.assertEqual(r.returncode, 0, r.stderr)
        wait_until(lambda: not (self.home / "daemon.pid").exists(), STOP_TIMEOUT, "脱离会话的 daemon 退出并删 pid 文件")
        self.assert_clean()

    @unittest.skipIf(sys.platform == "win32", "Windows 没有外部可发的 SIGTERM")
    def test_sigterm_shuts_down_cleanly(self):
        p = self.start()
        os.kill(p.pid, signal.SIGTERM)
        self.assertEqual(p.wait(STOP_TIMEOUT), 0)
        self.assert_clean()


if __name__ == "__main__":
    unittest.main()
