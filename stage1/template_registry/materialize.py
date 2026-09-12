# -*- coding: utf-8 -*-
"""把当前固定模板复制到独立 staging。"""

from __future__ import annotations

import shutil
from pathlib import Path

from .models import TemplateManifest, TemplateManifestError


##### 模板复制板块 #####


def materialize_template(
    manifest: TemplateManifest, repository_root: str | Path, destination: str | Path,
) -> Path:
    repository = Path(repository_root).resolve()
    source = repository.joinpath(*manifest.source_path.split("/")).resolve()
    target = Path(destination).resolve()
    if repository not in source.parents or not source.is_dir():
        raise TemplateManifestError("当前模板源目录不存在或越界。")
    existing = list(target.iterdir()) if target.is_dir() else []
    unexpected = [item for item in existing if item.name != ".scholar-generation.json"]
    if target.exists() and (not target.is_dir() or unexpected):
        raise TemplateManifestError("模板只能复制到新的或空的 staging 目录。")
    shutil.copytree(source, target, dirs_exist_ok=True)
    return target
