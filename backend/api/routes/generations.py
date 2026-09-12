# -*- coding: utf-8 -*-
"""确认结构后的 generation 提交、状态与安全产物下载。"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse, JSONResponse

from api.schemas import (
    GenerateRequest, GenerationResponse, TriggerGenerationResponse,
)
from persistence.store import Generation, ProjectStore, get_store
from infrastructure.tasks import TaskManager, get_task_manager
from generation.artifacts import artifact_path, validate_artifact_roots
from generation.versions import (
    VersionContractError, freeze_confirmed_snapshot,
    generation_response_payload, submit_generation_version, cleanup_frozen_snapshot,
)


##### generation 提交板块 #####


router = APIRouter(tags=["generations"])


def _confirmed_path(workspace_dir: str) -> Path:
    return Path(workspace_dir) / "reports" / "confirmed_structure.json"


@router.post(
    "/api/projects/{project_id}/generate",
    response_model=TriggerGenerationResponse,
)
def trigger_generation(
    project_id: str, body: GenerateRequest,
    store: ProjectStore = Depends(get_store),
    tasks: TaskManager = Depends(get_task_manager),
):
    if body.allowed_llm_tasks and not body.external_processing_allowed:
        return JSONResponse(
            status_code=409,
            content={"error": "agent_consent_required", "detail": "格式 Agent 模型任务需要一次项目级外部处理授权。"},
        )
    project = store.get(project_id)
    if project is None:
        return JSONResponse(
            status_code=404,
            content={"error": "project_not_found", "detail": "项目不存在"},
        )
    if not project.structure_confirmed:
        return JSONResponse(
            status_code=409,
            content={"error": "structure_not_confirmed", "detail": "请先确认结构。"},
        )
    if body.structure_revision != project.structure_revision:
        return JSONResponse(
            status_code=409,
            content={"error": "revision_conflict", "detail": "结构修订号已变化。"},
        )
    generation_id = str(uuid.uuid4())
    frozen_path = None
    created = False
    try:
        confirmed_path = _confirmed_path(project.workspace_dir)
        try:
            snapshot = json.loads(confirmed_path.read_text(encoding="utf-8"))
            if not isinstance(snapshot, dict):
                raise ValueError("确认快照不是对象。")
        except (OSError, UnicodeError, ValueError) as exc:
            raise VersionContractError("确认快照缺失、损坏或不可读。") from exc
        frozen_path, frozen_hash = freeze_confirmed_snapshot(
            project, generation_id, snapshot, structure_revision=body.structure_revision)
        generation = store.create_generation(
            generation_id=generation_id, project_id=project_id,
            structure_revision=body.structure_revision, source_sha256=project.source_sha256,
            confirmed_snapshot_path=frozen_path, confirmed_snapshot_sha256=frozen_hash)
        created = True
        task_id = submit_generation_version(
            store=store, tasks=tasks, project=project, generation=generation,
            external_processing_allowed=body.external_processing_allowed,
            allowed_llm_tasks=list(body.allowed_llm_tasks))
    except Exception as exc:
        try:
            if created or store.get_generation(generation_id) is not None:
                store.fail_generation(generation_id, detail="生成任务提交失败，已保留审计记录。")
            elif frozen_path:
                cleanup_frozen_snapshot(project, generation_id)
        except Exception as cleanup:
            exc.add_note(f"生成创建补偿失败：{cleanup}")
        contract = isinstance(exc, (VersionContractError, ValueError))
        return JSONResponse(status_code=409 if contract else 500, content={
            "error": "version_contract_failed" if contract else "generation_submission_failed",
            "detail": str(exc) if contract else "生成任务暂时无法提交，请稍后重试或联系维护人员。",
        })
    return TriggerGenerationResponse(generation_id=generation_id, task_id=task_id)


##### generation 查询板块 #####


@router.get(
    "/api/generations/{generation_id}", response_model=GenerationResponse
)
def get_generation(
    generation_id: str, store: ProjectStore = Depends(get_store)
):
    generation = store.get_generation(generation_id)
    if generation is None:
        return JSONResponse(
            status_code=404,
            content={"error": "generation_not_found", "detail": "生成版本不存在"},
        )
    project = store.get(generation.project_id)
    if project is None:
        return JSONResponse(status_code=404, content={"error": "project_not_found", "detail": "项目不存在"})
    return GenerationResponse(**generation_response_payload(
        generation, accepted_generation_id=project.accepted_generation_id))


##### 安全下载板块 #####


def _resolve_artifact(
    generation: Generation, *, workspace_dir: str, kind: str, name: str | None = None
) -> Path | None:
    try:
        validate_artifact_roots(workspace_dir=workspace_dir, generation_id=generation.generation_id,
                                output_dir=generation.output_dir, report_dir=generation.report_dir)
        items = generation.artifact_manifest.get("artifacts", [])
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            return None
        matches = [item for item in items if item.get("kind") == kind and (name is None or item.get("name") == name)]
        if len(matches) != 1:
            return None
        return artifact_path(matches[0], output_dir=generation.output_dir, report_dir=generation.report_dir)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None



def _download(
    generation_id: str, kind: str, store: ProjectStore, name: str | None = None
):
    generation = store.get_generation(generation_id)
    if generation is None:
        return JSONResponse(
            status_code=404,
            content={"error": "generation_not_found", "detail": "生成版本不存在"},
        )
    if kind in {"pdf", "latex_source"} and (
        not generation.trusted or generation.status not in {"success", "degraded"}
    ):
        return JSONResponse(status_code=404, content={"error": "artifact_not_found", "detail": "该版本没有可交付产物。"})
    project = store.get(generation.project_id)
    artifact = (_resolve_artifact(generation, workspace_dir=project.workspace_dir, kind=kind, name=name)
                if project is not None else None)
    if artifact is None:
        return JSONResponse(
            status_code=404,
            content={"error": "artifact_not_found", "detail": "产物不存在或路径校验失败"},
        )
    media = {
        "pdf": "application/pdf", "latex_source": "application/zip",
        "report": "application/json",
    }[kind]
    # PDF 需要在浏览器内预览；其余产物维持附件下载语义。
    if kind == "pdf":
        return FileResponse(
            artifact, media_type=media,
            headers={"Content-Disposition": f'inline; filename="{artifact.name}"'},
        )
    return FileResponse(artifact, media_type=media, filename=artifact.name)


@router.get("/api/generations/{generation_id}/pdf")
def download_pdf(generation_id: str, store: ProjectStore = Depends(get_store)):
    return _download(generation_id, "pdf", store)


@router.get("/api/generations/{generation_id}/latex-source")
def download_latex_source(
    generation_id: str, store: ProjectStore = Depends(get_store)
):
    return _download(generation_id, "latex_source", store)


@router.get("/api/generations/{generation_id}/reports/{report_name}")
def download_report(
    generation_id: str, report_name: str,
    store: ProjectStore = Depends(get_store),
):
    return _download(generation_id, "report", store, report_name)
