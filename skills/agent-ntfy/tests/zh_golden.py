#!/usr/bin/env python3
"""中文固定文案的快照：把 texts.TEXTS["zh"] 全列 dump 成 tests/zh_golden.json。

用例逐 key 比对快照——改任何一个中文字都会红，改字必须显式重写快照（`python3 tests/zh_golden.py --write`），
不能顺手润色。只写快照，不检查。
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import texts  # noqa: E402

GOLDEN = Path(__file__).with_name("zh_golden.json")

if __name__ == "__main__":
    if sys.argv[1:] != ["--write"]:
        sys.exit(f"用法：python3 {sys.argv[0]} --write   （重写 {GOLDEN.name}，之后 git diff 看改了哪些字）")
    GOLDEN.write_text(json.dumps(texts.TEXTS["zh"], ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"已写 {GOLDEN}：{len(texts.TEXTS['zh'])} 个 key")
