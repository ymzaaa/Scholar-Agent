# -*- coding: utf-8 -*-
"""正式 generation、最小双模板验收与反馈克隆共用的源码包契约。"""

from __future__ import annotations

import zipfile
import uuid
from pathlib import Path


##### 白名单常量板块 #####


SOURCE_SUFFIXES = {
    ".tex", ".cls", ".sty", ".bst", ".bib",
    ".png", ".jpg", ".jpeg", ".eps",
}
SOURCE_DIRS = {"contents", "includes", "data", "img", "assets"}
FORBIDDEN_ROOT_FILES = {"main.pdf", "latex-source.zip"}


##### 文件选择与打包板块 #####


def allowed_source_path(relative: Path) -> bool:
    """打包和克隆共用同一份必要源码白名单。"""
    return bool(relative.parts) and (
        relative.suffix.lower() in SOURCE_SUFFIXES
        and 'reports' not in relative.parts
        and (relative.name.casefold() not in FORBIDDEN_ROOT_FILES if len(relative.parts) == 1
             else relative.parts[0] in SOURCE_DIRS)
    )


def latex_source_members(root: str | Path) -> list[Path]:
    """只返回反馈克隆器允许的可重建源码和必要资源。"""
    base = Path(root).resolve()
    members: list[Path] = []
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(base)
        if not allowed_source_path(relative):
            continue
        members.append(path)
    return members


def package_latex_source(root: str | Path) -> tuple[Path, list[str]]:
    """按唯一白名单生成不含编译产物和说明文件的源码包。"""
    base = Path(root).resolve()
    archive = base / 'latex-source.zip'
    temporary = base / f'.tmp-{uuid.uuid4().hex[:12]}'
    members = latex_source_members(base)
    if not members:
        raise ValueError('LaTeX 源码包成员不能为空。')
    records: list[str] = []
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as output:
            for path in members:
                relative = path.relative_to(base).as_posix()
                output.write(path, relative)
                records.append(relative)
        temporary.replace(archive)
    except Exception as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError as cleanup:
            exc.add_note(f'源码包临时文件清理失败：{cleanup}')
        raise
    return archive, records
