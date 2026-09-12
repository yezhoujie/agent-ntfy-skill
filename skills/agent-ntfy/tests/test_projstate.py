"""项目级状态文件 `.agent-ntfy/state.json` 与 `away` 子命令：目录落在哪、自忽略、三态切换、各命令的回写。不打真网。"""

import contextlib
import json
import os
import re
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

import projstate
import tests.test_agent_ntfy as ta
from tests.test_daemon import Harness, wait_until
from tests.test_render import SAMPLE

CJK = re.compile(r"[一-鿿]")


@contextlib.contextmanager
def chdir(path):
    old = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(old)


def git_repo(case):
    """一个临时 git 仓：返回 (仓根, 子目录)。"""
    tmp = tempfile.TemporaryDirectory()
    case.addCleanup(tmp.cleanup)
    root = Path(tmp.name).resolve()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    sub = root / "src" / "deep"
    sub.mkdir(parents=True)
    return root, sub


def plain_dir(case):
    tmp = tempfile.TemporaryDirectory()
    case.addCleanup(tmp.cleanup)
    return Path(tmp.name).resolve()


class RootAndDirTest(unittest.TestCase):
    # 在 git 仓的子目录里跑，根是仓根；不在 git 仓里，根就是 cwd
    def test_root_is_git_toplevel_inside_repo_else_cwd(self):
        root, sub = git_repo(self)
        self.assertEqual(projstate.project_root(sub), root)
        plain = plain_dir(self)
        self.assertEqual(projstate.project_root(plain), plain)

    # ensure 建目录 + 自忽略的 .gitignore（内容 `*`），git 看不到它；用户仓的 .gitignore 一个字不动
    def test_ensure_creates_self_ignoring_dir(self):
        root, _ = git_repo(self)
        d = projstate.ensure(root)
        self.assertEqual(d, root / projstate.DIR_NAME)
        self.assertEqual((d / ".gitignore").read_text(encoding="utf-8"), "*\n")
        self.assertFalse((root / ".gitignore").exists())
        status = subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True, check=True).stdout
        self.assertEqual(status, "")
        ignored = subprocess.run(["git", "check-ignore", "-q", str(d / "state.json")], cwd=root)
        self.assertEqual(ignored.returncode, 0)
        projstate.ensure(root)  # 幂等
        self.assertEqual(sorted(p.name for p in d.iterdir()), [".gitignore"])

    # save 合并写：away 与 slot 各自更新互不覆盖；updated 每次刷新；目录里只有两个文件（原子写不留临时文件）
    def test_save_merges_and_is_atomic(self):
        root = plain_dir(self)
        self.assertFalse(projstate.exists(root))
        self.assertEqual(projstate.load(root), {})
        projstate.ensure(root)
        projstate.save(root, away=True, target="wD:p1")
        projstate.save(root, slot="slot2", confirmed=True)
        st = projstate.load(root)
        self.assertEqual((st["away"], st["slot"], st["confirmed"], st["target"]), (True, "slot2", True, "wD:p1"))
        self.assertRegex(st["updated"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
        projstate.save(root, slot=None, confirmed=None)
        self.assertEqual((projstate.load(root)["slot"], projstate.load(root)["away"]), (None, True))
        self.assertEqual(sorted(p.name for p in (root / projstate.DIR_NAME).iterdir()), [".gitignore", "state.json"])

    # 目录不存在时 note 不建目录、不写（没启用的项目不被污染）
    def test_note_does_nothing_without_dir(self):
        root = plain_dir(self)
        with chdir(root):
            projstate.note(slot="slot1", confirmed=True, target="x")
        self.assertFalse((root / projstate.DIR_NAME).exists())

    # 状态文件坏了（非法 UTF-8 / 非 JSON / 目录不可写）：load 当空，note 一声不吭——它不是命令的主事
    def test_broken_state_file_never_raises(self):
        root = plain_dir(self)
        projstate.ensure(root)
        projstate.state_path(root).write_bytes(b"\xff\xfe not json")
        self.assertEqual(projstate.load(root), {})
        with chdir(root):
            projstate.note(slot="slot1", confirmed=True)  # 覆盖坏文件也行
        self.assertEqual(projstate.load(root)["slot"], "slot1")
        os.chmod(root / projstate.DIR_NAME, 0o500)
        try:
            with chdir(root):
                projstate.note(slot="slot2")  # 不可写：吞掉
        finally:
            os.chmod(root / projstate.DIR_NAME, 0o700)
        self.assertEqual(projstate.load(root)["slot"], "slot1")


class AwayCommandTest(unittest.TestCase):
    """away on / off / status 不需要 daemon。"""

    def test_on_off_status(self):
        root, sub = git_repo(self)
        with chdir(sub):
            code, out, err = ta.run(["--home", "/nonexistent/agent-ntfy-home", "away", "on"], env=ta.HERDR)
            self.assertEqual((code, err), (0, ""))
            self.assertIn("开", out)
            st = json.loads((root / projstate.DIR_NAME / "state.json").read_text(encoding="utf-8"))
            self.assertEqual((st["away"], st["target"], st["slot"]), (True, "wD:p1", None))

            code, out, err = ta.run(["--home", "/nonexistent/agent-ntfy-home", "away", "status"], env=ta.HERDR)
            self.assertEqual(code, 0)
            self.assertIn(ta.Z("cli.away.state.on"), out)
            self.assertIn(ta.Z("cli.away.slot.none"), out)

            code, out, err = ta.run(["--home", "/nonexistent/agent-ntfy-home", "away", "off"], env=ta.HERDR)
            self.assertEqual(code, 0)
            self.assertFalse(json.loads((root / projstate.DIR_NAME / "state.json").read_text(encoding="utf-8"))["away"])

            code, out, err = ta.run(["--home", "/nonexistent/agent-ntfy-home", "away", "status", "--json"])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["away"], False)

            code, out, err = ta.run(["--home", "/nonexistent/agent-ntfy-home", "away", "status"], env={"AGENT_NTFY_LANG": "en"})
            self.assertEqual(code, 0)
            self.assertIsNone(CJK.search(out + err), (out, err))
            self.assertIn("off", out)

    # 没启用过的项目：status / off 都退出 0、说明未启用；都不建目录
    def test_status_and_off_when_not_enabled(self):
        root = plain_dir(self)
        with chdir(root):
            for action in ("status", "off"):
                code, out, err = ta.run(["--home", "/nonexistent/agent-ntfy-home", "away", action])
                self.assertEqual((code, err), (0, ""), action)
                self.assertIn(ta.Z("cli.away.not_enabled"), out)
                self.assertFalse((root / projstate.DIR_NAME).exists(), action)
            code, out, err = ta.run(["--home", "/nonexistent/agent-ntfy-home", "away", "status", "--json"])
            self.assertEqual((code, out), (0, "{}\n"))

    # 目录在、文件不在（或坏了）：算已启用，按空状态显示，不说「没有目录」
    def test_status_with_dir_but_no_file(self):
        root = plain_dir(self)
        projstate.ensure(root)
        with chdir(root):
            code, out, err = ta.run(["--home", "/nonexistent/agent-ntfy-home", "away", "status"])
            self.assertEqual(code, 0)
            self.assertNotIn(ta.Z("cli.away.not_enabled"), out)
            self.assertIn(ta.Z("cli.away.state.off"), out)
            self.assertIn(ta.Z("cli.away.slot.none"), out)

    # 状态目录读写不了（.agent-ntfy 是个普通文件）：人读报错、退 3，不是 traceback
    def test_on_reports_io_failure(self):
        root = plain_dir(self)
        (root / projstate.DIR_NAME).write_text("not a dir", encoding="utf-8")
        with chdir(root):
            code, out, err = ta.run(["--home", "/nonexistent/agent-ntfy-home", "away", "on"])
            self.assertEqual((code, out), (3, ""))
            self.assertIn("状态文件读写失败", err)


class HooksTest(unittest.TestCase):
    """ask / confirm-sub / release 只在 `.agent-ntfy/` 已存在时回写。"""

    def state(self, root):
        return json.loads((root / projstate.DIR_NAME / "state.json").read_text(encoding="utf-8"))

    def reply_when_sent(self, h, text):
        def go():
            wait_until(lambda: len(h.client.published) == 1)
            h.client.message(h.topic("slot1"), text)
        threading.Thread(target=go, daemon=True).start()

    def test_ask_sent_then_release(self):
        h = Harness(self)
        root = plain_dir(self)
        projstate.ensure(root)
        with chdir(root):
            self.reply_when_sent(h, "留固定目录")
            code, out, err = ta.run(["--home", str(h.home), "ask"], json.dumps(SAMPLE), ta.HERDR)
            self.assertEqual((code, out), (0, "留固定目录\n"))
            st = self.state(root)
            self.assertEqual((st["slot"], st["confirmed"], st["target"]), ("slot1", True, "wD:p1"))
            wait_until(lambda: len(h.client.clears) == 1)
            code, out, err = ta.run(["--home", str(h.home), "release"], env=ta.HERDR)
            self.assertEqual(code, 0)
            st = self.state(root)
            self.assertEqual((st["slot"], st["confirmed"], st["target"]), (None, None, "wD:p1"))

    # 状态文件是坏的：已发出的 ask 照样退 0、回复照样到 stdout（回写只是顺手，绝不能把主事搞失败）
    def test_ask_survives_broken_state_file(self):
        h = Harness(self)
        root = plain_dir(self)
        projstate.ensure(root)
        projstate.state_path(root).write_bytes(b"\xff")
        with chdir(root):
            self.reply_when_sent(h, "留固定目录")
            code, out, err = ta.run(["--home", str(h.home), "ask"], json.dumps(SAMPLE), ta.HERDR)
            self.assertEqual((code, out, err), (0, "留固定目录\n", ""))
            self.assertEqual(self.state(root)["slot"], "slot1")  # 坏文件被合法内容覆盖

    # release <别的槽位> 不清本项目的记录；confirm-sub <别的槽位> 不把它写成本项目的槽位
    def test_hooks_respect_slot_ownership(self):
        h = Harness(self)
        root = plain_dir(self)
        projstate.ensure(root)
        with chdir(root):
            self.reply_when_sent(h, "答")
            code, out, err = ta.run(["--home", str(h.home), "ask"], json.dumps(SAMPLE), ta.HERDR)
            self.assertEqual(code, 0)
            wait_until(lambda: len(h.client.clears) == 1)
            self.assertEqual(self.state(root)["slot"], "slot1")
            code, out, err = ta.run(["--home", str(h.home), "confirm-sub", "slot3", "--subscribed"], env=ta.HERDR)
            self.assertEqual(code, 0)
            self.assertEqual((self.state(root)["slot"], self.state(root)["confirmed"]), ("slot1", True))
            h.state.acquire("someone-else")  # 让 slot2 有租约可释放
            code, out, err = ta.run(["--home", str(h.home), "release", "slot2"], env=ta.HERDR)
            self.assertEqual(code, 0)
            self.assertEqual(self.state(root)["slot"], "slot1")
            code, out, err = ta.run(["--home", str(h.home), "release", "slot1"], env=ta.HERDR)  # 显式释放的正是本项目那个 ⇒ 清
            self.assertEqual(code, 0)
            self.assertIsNone(self.state(root)["slot"])

    def test_ask_unconfirmed_writes_confirmed_false(self):
        h = Harness(self, subscribed=())
        root = plain_dir(self)
        projstate.ensure(root)
        with chdir(root):
            code, out, err = ta.run(["--home", str(h.home), "ask"], json.dumps(SAMPLE), ta.HERDR)
            self.assertEqual((code, out), (4, ""))
            st = self.state(root)
            self.assertEqual((st["slot"], st["confirmed"]), ("slot1", False))

    def test_confirm_sub_already_confirmed_writes_true(self):
        h = Harness(self)
        root = plain_dir(self)
        projstate.ensure(root)
        projstate.save(root, slot="slot2", confirmed=False)  # ask 退 4 之后的样子
        with chdir(root):
            code, out, err = ta.run(["--home", str(h.home), "confirm-sub", "slot2", "--subscribed"], env=ta.HERDR)
            self.assertEqual(code, 0)
            st = self.state(root)
            self.assertEqual((st["slot"], st["confirmed"]), ("slot2", True))

    def test_no_dir_no_write(self):
        h = Harness(self)
        root = plain_dir(self)
        with chdir(root):
            self.reply_when_sent(h, "答")
            code, out, err = ta.run(["--home", str(h.home), "ask"], json.dumps(SAMPLE), ta.HERDR)
            self.assertEqual(code, 0)
            self.assertFalse((root / projstate.DIR_NAME).exists())


if __name__ == "__main__":
    unittest.main()
