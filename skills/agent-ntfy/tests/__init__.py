"""让 scripts/ 下的模块能被测试直接 import（它们是以脚本形态运行的，不是包）；同时把 AGENT_NTFY_STORE 缺省钉为 file。"""

import os
import sys

_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

# 测试进程缺省不碰真钥匙串 / DPAPI：不注入 store 就起 daemon 的用例（如真起子进程的）按平台缺省会走 KeychainStore / DpapiStore，
# 这里把缺省钉成文件存储（Harness 自己注入 MemoryStore，不经这条）。显式设了的用例不受影响；
# 测「平台缺省」的用例自己从 environ 里删掉这个变量。
os.environ.setdefault("AGENT_NTFY_STORE", "file")
