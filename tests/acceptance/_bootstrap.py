# -*- coding: utf-8 -*-
"""验收命令共用的仓库路径和 stage1 导入引导。"""

from __future__ import annotations

import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
STAGE1_ROOT = REPOSITORY_ROOT / "stage1"


def ensure_stage1_imports() -> None:
    """使移动后的验收脚本继续导入稳定的 stage1 公共接口。"""
    value = str(STAGE1_ROOT)
    if value not in sys.path:
        sys.path.insert(0, value)
