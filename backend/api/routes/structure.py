# -*- coding: utf-8 -*-
"""结构预览、乐观锁修订与人工确认路由。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from api.schemas import StructureConfirmationRequest, StructureResponse
from persistence.store import Project, ProjectStore, get_store
from recognition.structure import (
    StructureValidationError, commit_structure_confirmation,
    read_structure_snapshot,
)


##### 快照读写板块 #####


router = APIRouter(prefix="/api/projects", tags=["structure"])


def _public_snapshot(project: Project, snapshot: dict[str, Any]) -> StructureResponse:
    return StructureResponse(
        project_id=project.project_id, source_sha256=project.source_sha256,
        schema_version=snapshot["schema_version"], revision=snapshot["revision"],
        confirmed=snapshot["confirmed"], headings=snapshot["headings"],
        object_bindings=snapshot["object_bindings"],
        formula_review=snapshot.get("formula_review", {}), counts=snapshot["counts"],
        citation_review=snapshot.get("citation_review", {}),
        issues=snapshot.get("issues", []),
    )


##### 路由定义板块 #####


@router.get("/{project_id}/structure", response_model=StructureResponse)
def get_structure(project_id: str, store: ProjectStore = Depends(get_store)):
    project = store.get(project_id)
    if project is None:
        return JSONResponse(
            status_code=404,
            content={"error": "project_not_found", "detail": "项目不存在"},
        )
    snapshot = read_structure_snapshot(project)
    if snapshot is None:
        return JSONResponse(
            status_code=409,
            content={"error": "structure_not_ready", "detail": "提取尚未完成"},
        )
    if snapshot.get("project_source_sha256") != project.source_sha256:
        return JSONResponse(
            status_code=409,
            content={"error": "source_changed", "detail": "结构快照与源文件不匹配"},
        )
    return _public_snapshot(project, snapshot)


@router.put("/{project_id}/structure", response_model=StructureResponse)
def confirm_structure(
    project_id: str, body: StructureConfirmationRequest,
    store: ProjectStore = Depends(get_store),
):
    project = store.get(project_id)
    if project is None:
        return JSONResponse(
            status_code=404,
            content={"error": "project_not_found", "detail": "项目不存在"},
        )
    try:
        snapshot = commit_structure_confirmation(
            project=project, store=store, base_revision=body.base_revision,
            heading_overrides=[item.model_dump() for item in body.heading_overrides],
            object_binding_overrides=[
                item.model_dump() for item in body.object_binding_overrides
            ],
            confirmed=body.confirmed,
            citation_overrides=[item.model_dump() for item in body.citation_overrides],
        )
    except StructureValidationError as exc:
        status = 409 if exc.code in {"structure_not_ready", "revision_conflict", "source_changed"} else 422
        content: dict[str, Any] = {"error": exc.code, "detail": exc.detail}
        if exc.unit_ids:
            content["unit_ids"] = exc.unit_ids
        return JSONResponse(status_code=status, content=content)
    updated = store.get(project_id)
    assert updated is not None
    return _public_snapshot(updated, snapshot)
