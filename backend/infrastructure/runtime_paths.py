# -*- coding: utf-8 -*-
"""后端运行时数据的默认目录，确保数据库与项目工作区互不嵌套。"""

from pathlib import Path


##### 默认路径板块 #####


def runtime_root() -> Path:
    return Path(__file__).resolve().parents[1] / "runtime"


def default_database_path() -> Path:
    return runtime_root() / "scholar.db"


def default_project_workspace_root() -> Path:
    return runtime_root() / "project-workspace"
