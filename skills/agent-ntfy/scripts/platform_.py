"""POSIX / Windows 分支收拢：起脱离终端的子进程、收紧目录权限、以 0600 打开私密文件、
装停止信号、把标准流切到 UTF-8。

每个函数在**调用时**通过 `_platform()` / `_python_version()` 判断当前平台与解释器版本，
不在 import 时把平台冻成常量——这样测试能用 mock.patch 逐条切换分支跑到。

平台差异一览：
- `spawn_detached`：POSIX 用 `start_new_session=True` 让子进程脱离当前会话；Windows 没有
  会话的概念，改用 `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP` 创建标志达到同样效果。
- `restrict_private_dir`：POSIX 直接 `chmod 0o700`；Windows 3.13 以前 `Path.mkdir` 的
  mode 参数不映射到 ACL，只能尽力再跑一次 `icacls` 把目录锁给当前用户，跑不动也不能让
  daemon 因为这一步起不来，只记警告；3.13+ CPython 自己会据 mode 设 ACL，不必再跑外部命令。
- `open_private`：两平台都调 `os.open(path, flags, 0o600)`；Windows 的文件权限位只映射
  只读标记，真正的私密性靠上一条把所在目录的 ACL 收紧。
- `stop_signal_names` / `install_stop_signals`：Windows 没有 SIGTERM / SIGHUP，能捕获的
  只有 SIGINT（以及部分场景下的 SIGBREAK，不是所有解释器构建都提供）；装不上的信号名跳过，
  不因为某个名字在本机 `signal` 模块里不存在就报错。
- `utf8_stdio`：POSIX 下标准流的编码已经是运行环境的 locale，多数情况就是 UTF-8，不用动；
  Windows 控制台默认代码页不是 UTF-8，需要显式 `reconfigure` 三个标准流。
"""

import getpass
import logging
import os
import signal
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

LOG = logging.getLogger("agent-ntfy.platform")

STOP_SIGNALS_POSIX = ("SIGTERM", "SIGINT", "SIGHUP")
STOP_SIGNALS_WINDOWS = ("SIGINT", "SIGBREAK")
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200


def _platform() -> str:
    return sys.platform


def _python_version() -> tuple[int, int]:
    return sys.version_info[:2]


def is_windows() -> bool:
    """当前解释器是否跑在 Windows 上。"""
    return _platform() == "win32"


def spawn_detached(argv: list[str]) -> subprocess.Popen:
    """起一个脱离当前会话 / 控制台的子进程，三路标准流全接 DEVNULL。"""
    if is_windows():
        return subprocess.Popen(
            argv,
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    return subprocess.Popen(
        argv,
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def restrict_private_dir(path: Path, *, run: Callable[..., Any] = subprocess.run) -> None:
    """建目录（含缺失的上级目录）并让它只对当前用户可见；安全收紧失败只记警告，不抛异常。"""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not is_windows():
        os.chmod(path, 0o700)  # 目录早就存在时 mkdir 的 mode 不生效，需要再收紧一次
        return
    if _python_version() >= (3, 13):
        return  # 3.13+ 的 Path.mkdir(mode=...) 在 Windows 上会据此设 ACL，不必再跑外部命令
    user = os.environ.get("USERNAME") or getpass.getuser()
    argv = ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:(OI)(CI)F"]
    try:
        result = run(argv, capture_output=True, text=True)
    except OSError as e:
        LOG.warning("icacls 收紧目录权限失败（%s: %s），%s 的私密性仅靠应用层保证", type(e).__name__, e, path)
        return
    if result.returncode != 0:
        LOG.warning("icacls 收紧目录权限返回非零（rc=%s），%s 的私密性仅靠应用层保证", result.returncode, path)


def open_private(path: Path, flags: int) -> int:
    """以仅当前用户可读写的权限打开文件，返回文件描述符。

    Windows 上文件权限位只映射只读标记，不提供 POSIX 那种按用户区分的访问控制，
    真正的私密性要靠 restrict_private_dir 收紧所在目录的 ACL。
    """
    return os.open(path, flags, 0o600)


def stop_signal_names() -> tuple[str, ...]:
    """当前平台上表示「请停止」的信号名。"""
    return STOP_SIGNALS_WINDOWS if is_windows() else STOP_SIGNALS_POSIX


def install_stop_signals(fn: Callable[..., Any]) -> list[int]:
    """给 stop_signal_names() 里每个在本机 signal 模块中存在的信号装上 fn，返回实际装上的信号号。

    只在主线程里调用才有意义，这个判断由调用方做，本函数不做线程检查。
    """
    installed: list[int] = []
    for name in stop_signal_names():
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        signal.signal(sig, fn)
        installed.append(sig)
    return installed


def utf8_stdio() -> None:
    """Windows 上把标准流切到 UTF-8（控制台默认代码页通常不是）；其余平台不做任何事。"""
    if not is_windows():
        return
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        reconfigure(encoding="utf-8", errors="replace")
