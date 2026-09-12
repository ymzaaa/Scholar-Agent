# -*- coding: utf-8 -*-
"""generation 版本的确认快照冻结、可信父版本校验与任务提交。"""

from __future__ import annotations

import hashlib
import json
import uuid
import shutil
from pathlib import Path
from typing import Any

from generation.service import execute_generation
from persistence.store import Generation, Project, ProjectStore
from infrastructure.tasks import TaskManager
from infrastructure.template_catalog import resolve_template_binding
from pipeline.workspace import _write_json
from generation.artifacts import validate_delivery_manifest, artifact_path


##### 版本异常板块 #####


class VersionContractError(RuntimeError):
    """父版本、冻结输入或产物不满足可信版本契约。"""


##### 冻结快照板块 #####


def _validate_snapshot(
    payload: dict[str, Any], *, project: Project, structure_revision: int,
) -> None:
    if not payload.get("confirmed"):
        raise VersionContractError("结构快照尚未确认。")
    if payload.get("revision") != structure_revision:
        raise VersionContractError("结构快照修订号与版本不一致。")
    if payload.get("project_source_sha256") != project.source_sha256:
        raise VersionContractError("结构快照源文件哈希与项目不一致。")


def freeze_confirmed_snapshot(
    project: Project, generation_id: str, payload: dict[str, Any],
    *, structure_revision: int,
) -> tuple[str, str]:
    """每个候选版本保存不可变结构输入，避免项目快照后续被覆盖。"""
    identifier = str(uuid.UUID(generation_id))
    _validate_snapshot(
        payload, project=project, structure_revision=structure_revision,
    )
    workspace = Path(project.workspace_dir).resolve()
    target_dir = workspace / "reports" / "version-inputs" / identifier
    target = target_dir / "confirmed_structure.json"
    if workspace not in target.resolve().parents:
        raise VersionContractError("冻结快照路径逃逸项目工作区。")
    target_dir.mkdir(parents=True, exist_ok=False)
    try:
        _write_json(target, payload)
        return str(target), hashlib.sha256(target.read_bytes()).hexdigest()
    except Exception as exc:
        try:
            cleanup_frozen_snapshot(project, identifier)
        except Exception as cleanup:
            exc.add_note(f"冻结快照清理失败：{cleanup}")
        raise


def cleanup_frozen_snapshot(project: Project, generation_id: str) -> None:
    """仅清理本次创建失败的 generation 专属冻结目录。"""
    identifier = str(uuid.UUID(generation_id))
    parent = (Path(project.workspace_dir) / 'reports' / 'version-inputs').resolve()
    target = parent / identifier
    if target.exists():
        if target.resolve().parent != parent or target.is_symlink():
            raise VersionContractError('冻结快照清理路径不安全。')
        shutil.rmtree(target)


def load_parent_snapshot(parent: Generation, project: Project) -> dict[str, Any]:
    """只读取本 generation 的冻结快照，禁止回退到项目最新结构。"""
    expected = Path(project.workspace_dir).resolve() / 'reports' / 'version-inputs' / parent.generation_id / 'confirmed_structure.json'
    path = Path(parent.confirmed_snapshot_path).resolve() if parent.confirmed_snapshot_path else None
    if path != expected or not parent.confirmed_snapshot_sha256:
        raise VersionContractError("版本缺少可验证的冻结结构快照。")
    try:
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != parent.confirmed_snapshot_sha256:
            raise VersionContractError("冻结结构快照哈希校验失败。")
        payload = json.loads(content.decode('utf-8'))
        if not isinstance(payload, dict):
            raise VersionContractError("冻结结构快照不是对象。")
        _validate_snapshot(payload, project=project, structure_revision=parent.structure_revision)
        return payload
    except (OSError, UnicodeError, ValueError) as exc:
        raise VersionContractError("冻结结构快照损坏或不可读。") from exc


##### 可信父版本板块 #####


def validate_trusted_generation(generation: Generation, project: Project) -> None:
    """验证准备采用的可信版本及其冻结输入，PDF 哈希在此边界核对一次。"""
    if generation.project_id != project.project_id:
        raise VersionContractError("版本不属于当前项目。")
    if not generation.trusted or generation.status not in {"success", "degraded"}:
        raise VersionContractError("只能采用已发布的可信版本。")
    if generation.source_sha256 != project.source_sha256:
        raise VersionContractError("版本源文件哈希与项目不一致。")
    try:
        validate_delivery_manifest(workspace_dir=project.workspace_dir,
            generation_id=generation.generation_id, output_dir=generation.output_dir,
            report_dir=generation.report_dir, manifest=generation.artifact_manifest, check_files=False)
        for artifact in generation.artifact_manifest['artifacts']:
            path = artifact_path(artifact, output_dir=generation.output_dir, report_dir=generation.report_dir,
                                 check_file=artifact['kind'] != 'latex_source')
            # 父源码包大小和整体哈希留在克隆入口唯一核对；这里仅检查登记路径和存在性。
            if not path.is_file():
                raise VersionContractError("版本产物缺失。")
        item = next(item for item in generation.artifact_manifest['artifacts'] if item['kind'] == 'pdf')
        pdf = artifact_path(item, output_dir=generation.output_dir, report_dir=generation.report_dir)
        if hashlib.sha256(pdf.read_bytes()).hexdigest() != item['sha256']:
            raise VersionContractError("版本 PDF 哈希与产物清单不一致。")
        load_parent_snapshot(generation, project)
    except (OSError, ValueError) as exc:
        raise VersionContractError(str(exc)) from exc


def generation_response_payload(
    generation: Generation, *, accepted_generation_id: str | None,
) -> dict[str, Any]:
    """generation 与 version 共用同一不可变记录，统一对外字段。"""
    return {
        "generation_id": generation.generation_id,
        "project_id": generation.project_id,
        "structure_revision": generation.structure_revision,
        "source_sha256": generation.source_sha256,
        "task_id": generation.task_id,
        "status": generation.status,
        "quality_status": generation.quality_status,
        "detail": generation.detail,
        "artifacts": generation.artifact_manifest.get("artifacts", []),
        "parent_version_id": generation.parent_generation_id,
        "version_number": generation.version_number,
        "change_origin": generation.change_origin,
        "created_by": generation.created_by,
        "trusted": generation.trusted,
        "review_status": generation.review_status,
        "accepted": accepted_generation_id == generation.generation_id,
        "rollback_target_id": generation.rollback_target_id,
        "active_constraints": generation.active_constraints,
        "gate_snapshot": generation.gate_snapshot,
        "created_at": generation.created_at,
        "updated_at": generation.updated_at,
    }


##### 任务提交板块 #####


def submit_generation_version(
    *, store: ProjectStore, tasks: TaskManager,
    project: Project, generation: Generation,
    external_processing_allowed: bool = False,
    allowed_llm_tasks: list[str] | None = None,
) -> str:
    resolve_template_binding(project.template_id)
    task_id = tasks.submit(
        execute_generation,
        database_path=str(store.db_path), generation_id=generation.generation_id,
        external_processing_allowed=external_processing_allowed,
        allowed_llm_tasks=list(allowed_llm_tasks or []),
        project_id=project.project_id, task_type="generation",
        linked_generation_id=generation.generation_id,
    )
    return task_id
