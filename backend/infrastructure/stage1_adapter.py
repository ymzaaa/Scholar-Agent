# -*- coding: utf-8 -*-
"""后端与 Stage1 的唯一适配边界。"""

from __future__ import annotations

import json
import os
import sys
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Any

from infrastructure.runtime_paths import default_project_workspace_root


##### Stage1 装载板块 #####


class Stage1ImportError(RuntimeError):
    """Stage1 路径或依赖不可用。"""


class WorkspaceUnavailableError(RuntimeError):
    """后端项目工作区不可初始化，供 HTTP 层转换为稳定错误。"""


class RegisteredTemplateError(ValueError):
    """模板未注册、版本不可用或注册指纹无效。"""


def _ensure_stage1_on_path() -> None:
    stage1_dir = os.environ.get("STAGE1_DIR")
    if not stage1_dir:
        stage1_dir = str(Path(__file__).resolve().parents[1] / "stage1")
    resolved = str(Path(stage1_dir).resolve())
    if resolved not in sys.path:
        sys.path.insert(0, resolved)


@lru_cache(maxsize=1)
def _get_runner():
    _ensure_stage1_on_path()
    try:
        from pipeline_api import PipelineRunner  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise Stage1ImportError(f"无法导入 Stage1：{exc}") from exc
    return PipelineRunner()


@lru_cache(maxsize=1)
def _get_workspace_manager():
    _ensure_stage1_on_path()
    try:
        from pipeline.workspace import WorkspaceManager
    except Exception as exc:  # noqa: BLE001
        raise Stage1ImportError(f"无法导入工作区模块：{exc}") from exc
    root = os.environ.get("SCHOLAR_WORKSPACE_ROOT") or str(
        default_project_workspace_root()
    )
    return WorkspaceManager(root)


def create_project_workspace():
    """创建不含客户端绝对路径的服务端项目工作区。"""
    try:
        return _get_workspace_manager().create_project()
    except Exception as exc:  # 仅在边界识别工作区异常，不泄露内部绝对路径
        _ensure_stage1_on_path()
        from pipeline.workspace import WorkspaceSafetyError

        if isinstance(exc, WorkspaceSafetyError):
            raise WorkspaceUnavailableError(
                "项目工作区不可用，请检查服务端运行目录配置。"
            ) from exc
        raise


def list_registered_templates() -> list[dict[str, Any]]:
    """返回可公开展示的已注册模板摘要，不暴露服务端源路径。"""
    _ensure_stage1_on_path()
    from template_registry.registry import list_builtin_templates

    templates: list[dict[str, Any]] = []
    for resolved in list_builtin_templates():
        manifest = resolved.manifest
        templates.append({
            "template_id": manifest.template_id,
            "display_name": manifest.display_name,
            "school": manifest.school,
            "education_level": manifest.education_level,
            "capabilities": dict(manifest.capabilities),
            "bibtex_supported": manifest.bibtex_supported,
        })
    return templates


def resolve_registered_template(template_id: str) -> dict[str, Any]:
    """解析当前双 OUC 模板选择。"""
    _ensure_stage1_on_path()
    from template_registry.registry import resolve_builtin_template
    from template_registry.models import TemplateManifestError

    try:
        resolved = resolve_builtin_template(template_id)
    except TemplateManifestError as exc:
        raise RegisteredTemplateError(str(exc)) from exc
    manifest = resolved.manifest
    return {
        "template_id": manifest.template_id,
        "display_name": manifest.display_name,
        "school": manifest.school,
        "education_level": manifest.education_level,
        "capabilities": dict(manifest.capabilities),
        "bibtex_supported": manifest.bibtex_supported,
    }


def clone_parent_latex_source(**values: Any) -> dict[str, Any]:
    _ensure_stage1_on_path()
    from pipeline.source_clone import clone_latex_source

    return clone_latex_source(**values)


##### 结构快照板块 #####


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".tmp-{uuid.uuid4().hex}")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def run_extract_and_recognize(
    *, docx_path: str, project_dir: str, source_sha256: str
) -> dict[str, Any]:
    """只执行提取与识别，并生成可修订、可追溯的结构快照。"""
    _ensure_stage1_on_path()
    from content_extraction.persistence import load_or_extract

    runner = _get_runner()
    extract_result = load_or_extract(
        project_dir,
        source_sha256=source_sha256,
        extractor=lambda: runner.extract(docx_path),
    )
    from pipeline.confirmed_structure import build_structure_snapshot

    artifact = Path(project_dir) / "reports" / "recognition.json"
    if artifact.is_file():
        snapshot = json.loads(artifact.read_text(encoding="utf-8"))
        if snapshot.get("project_source_sha256") != source_sha256 or snapshot.get("schema_version") != extract_result.extraction_report["schema_version"]:
            raise ValueError("已保存结构与当前源文件或内容模型不一致")
        if "regions" not in snapshot:
            raise ValueError("已保存结构缺少完整区域契约，请重新上传建立项目")
    else:
        recognize_result = runner.recognize(extract_result)
        snapshot = build_structure_snapshot(extract_result, recognize_result, source_sha256)
    _write_json_atomic(artifact, snapshot)
    return {
        "artifact": str(artifact), "schema_version": snapshot["schema_version"],
        "counts": snapshot["counts"], "issue_count": len(snapshot["issues"]),
    }


##### generation 适配板块 #####


def run_project_generation(
    *, project_id: str, generation_id: str,
    bib_path: str | None, citation_map_path: str | None,
    reference_source: str, source_sha256: str, structure_revision: int,
    template_id: str,
    confirmed_snapshot_path: str,
    version_context: dict[str, Any] | None = None,
    external_processing_allowed: bool = False,
    allowed_llm_tasks: list[str] | None = None,
    on_agent_state=None,
) -> dict[str, Any]:
    """调用确定性生成调度器，只将实际 Agent 状态回传后端登记。"""
    _ensure_stage1_on_path()
    from pipeline.generation_service import run_confirmed_generation
    from template_registry.registry import resolve_builtin_template
    from pipeline_api import PipelineRunner

    workspace_root = str(_get_workspace_manager().root)
    resolved = resolve_builtin_template(template_id)
    repository_root = Path(__file__).resolve().parents[1]
    template_dir = str(
        repository_root.joinpath(*resolved.manifest.source_path.split("/"))
    )
    return run_confirmed_generation(
        runner=PipelineRunner(resolved_template=resolved),
        workspace_root=workspace_root,
        project_id=project_id, generation_id=generation_id,
        bib_path=bib_path,
        citation_map_path=citation_map_path,
        reference_source=reference_source, template_dir=template_dir,
        template_id=resolved.manifest.template_id,
        source_sha256=source_sha256, structure_revision=structure_revision,
        confirmed_snapshot_path=confirmed_snapshot_path,
        version_context=version_context,
        external_processing_allowed=external_processing_allowed,
        allowed_llm_tasks=list(allowed_llm_tasks or []),
        on_agent_state=on_agent_state,
    )
