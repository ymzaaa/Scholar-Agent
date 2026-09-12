# -*- coding: utf-8 -*-
"""双 OUC 当前模板真正参与运行的数据模型。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any


##### 数据模型板块 #####


class TemplateManifestError(ValueError):
    """当前模板清单缺失或包含不可运行配置。"""


@dataclass(frozen=True, slots=True)
class CompileRecipe:
    entrypoint: str
    engine: str
    engine_runs: int
    bibliography_backend: str
    bibliography_policy: str
    artifact_stem: str
    auxiliary_directories: tuple[str, ...]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CompileRecipe":
        recipe = cls(
            entrypoint=str(data["entrypoint"]), engine=str(data["engine"]),
            engine_runs=int(data["engine_runs"]),
            bibliography_backend=str(data["bibliography_backend"]),
            bibliography_policy=str(data["bibliography_policy"]),
            artifact_stem=str(data["artifact_stem"]),
            auxiliary_directories=tuple(map(str, data.get("auxiliary_directories", []))),
        )
        entrypoint = PurePosixPath(recipe.entrypoint.replace("\\", "/"))
        if entrypoint.is_absolute() or ".." in entrypoint.parts or entrypoint.suffix != ".tex":
            raise TemplateManifestError("编译入口必须是模板内的相对 tex 路径。")
        if recipe.engine not in {"xelatex", "lualatex", "pdflatex"}:
            raise TemplateManifestError(f"不支持的 LaTeX 引擎：{recipe.engine}")
        if not 1 <= recipe.engine_runs <= 5:
            raise TemplateManifestError("LaTeX 编译轮次必须在 1 到 5 之间。")
        if recipe.bibliography_backend not in {"none", "bibtex", "biber"}:
            raise TemplateManifestError("参考文献后端无效。")
        if recipe.bibliography_policy not in {
            "auto_if_cited", "always_if_declared", "disabled",
        }:
            raise TemplateManifestError("参考文献编译策略无效。")
        disabled = recipe.bibliography_policy == "disabled"
        if disabled != (recipe.bibliography_backend == "none"):
            raise TemplateManifestError("参考文献后端与编译策略不一致。")
        if not recipe.artifact_stem or not all(
            char.isalnum() or char in "._-" for char in recipe.artifact_stem
        ):
            raise TemplateManifestError("编译产物名称无效。")
        for value in recipe.auxiliary_directories:
            relative = PurePosixPath(value.replace("\\", "/"))
            if relative.is_absolute() or ".." in relative.parts or len(relative.parts) != 1:
                raise TemplateManifestError("辅助目录必须是模板根目录下的单层目录。")
        return recipe


@dataclass(frozen=True, slots=True)
class TemplateManifest:
    manifest_path: Path
    template_id: str
    display_name: str
    school: str
    education_level: str
    source_path: str
    adapter_name: str
    compile_recipe: CompileRecipe
    content_directory: str
    capabilities: dict[str, str]
    bibtex_supported: bool
    format_profile_path: str | None

    @classmethod
    def load(cls, path: str | Path, repository_root: str | Path) -> "TemplateManifest":
        manifest_path = Path(path).resolve()
        repository = Path(repository_root).resolve()
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        if data.get("schema_version") != "1.0.0":
            raise TemplateManifestError("模板 Manifest 版本不受支持。")
        relative = PurePosixPath(str(data["source_path"]).replace("\\", "/"))
        if relative.is_absolute() or ".." in relative.parts:
            raise TemplateManifestError("模板源必须是仓库内相对路径。")
        source = repository.joinpath(*relative.parts).resolve()
        if repository not in source.parents or not source.is_dir():
            raise TemplateManifestError(f"模板源目录不存在或越界：{relative}")
        return cls(
            manifest_path=manifest_path, template_id=str(data["template_id"]),
            display_name=str(data["display_name"]), school=str(data["school"]),
            education_level=str(data["education_level"]), source_path=relative.as_posix(),
            adapter_name=str(data["adapter"]),
            compile_recipe=CompileRecipe.from_dict(data["compile_recipe"]),
            content_directory=str(data["content_directory"]),
            capabilities=dict(data.get("capabilities", {})),
            bibtex_supported=bool(data.get("bibtex_supported", False)),
            format_profile_path=data.get("format_profile_path"),
        )
