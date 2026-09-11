"""让 scripts/ 下的模块能被测试直接 import（它们是以脚本形态运行的，不是包）。"""

import os
import sys

_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)
