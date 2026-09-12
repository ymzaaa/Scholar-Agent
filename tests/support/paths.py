# -*- coding: utf-8 -*-
"""测试统一使用的仓库路径，避免目录迁移后重复计算层级。"""

from pathlib import Path


##### 仓库路径板块 #####


REPO_ROOT = Path(__file__).resolve().parents[2]
STAGE1_DIR = REPO_ROOT / "stage1"
TEMPLATE_ROOT = STAGE1_DIR / "template"
GRADUATE_TEMPLATE = TEMPLATE_ROOT / "OUC研究生" / "source"
BACHELOR_TEMPLATE = TEMPLATE_ROOT / "OUC本科生" / "source"
BACKEND_DIR = REPO_ROOT / "backend"
REAL_WORD_DIR = REPO_ROOT / "tests" / "private-data"
BASELINE_DIR = REPO_ROOT / "tests" / "baselines"
