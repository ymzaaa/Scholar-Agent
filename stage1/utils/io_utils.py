# -*- coding: utf-8 -*-
"""utils/io_utils.py —— 通用文件读写工具（所有中间产物 JSON 均经由此处落盘）。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def read_text(path: Path | str) -> str:
    return Path(path).read_text(encoding="utf-8", errors="replace")


def write_text(path: Path | str, content: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def save_json(obj: Any, path: Path | str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def load_json(path: Path | str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def ensure_dir(path: Path | str) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p
