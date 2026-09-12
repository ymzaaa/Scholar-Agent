# -*- coding: utf-8 -*-
"""让管理员命令在模块调用和直接文件调用下使用同一 stage1 导入根。"""

from __future__ import annotations

import sys
from pathlib import Path


STAGE1_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = STAGE1_ROOT.parent


def ensure_stage1_imports() -> None:
    """只补充确定的 stage1 根目录，不读取或修改外部环境。"""
    value = str(STAGE1_ROOT)
    if value not in sys.path:
        sys.path.insert(0, value)
