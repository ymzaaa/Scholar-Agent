# -*- coding: utf-8 -*-
"""项目创建与上传路由。"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import JSONResponse

from api.schemas import CreateProjectResponse
from infrastructure.stage1_adapter import WorkspaceUnavailableError, create_project_workspace
from persistence.store import ProjectStore, get_store
from infrastructure.template_catalog import TemplateSelectionError, resolve_template_binding
from infrastructure.uploads import (
    BIB_LIMIT, DOCX_LIMIT, MAP_LIMIT, UploadValidationError,
    save_upload, validate_docx, validate_json_mapping,
)


##### 路由定义板块 #####


router = APIRouter(prefix="/api/projects", tags=["projects"])


async def _close_uploads(*uploads: UploadFile | None) -> None:
    for upload in uploads:
        if upload is not None:
            await upload.close()


@router.post("", response_model=CreateProjectResponse)
async def create_project(
    docx: UploadFile = File(...),
    bib: UploadFile | None = File(default=None),
    citation_map: UploadFile | None = File(default=None),
    reference_source: Literal["auto", "word_list", "bib"] = Form(default="auto"),
    template_id: str = Form(default="ouc-graduate"),
    recognition_review_allowed: bool = Form(default=False),
    store: ProjectStore = Depends(get_store),
):
    """客户端只提交文件；目录名与实际存储路径完全由服务端决定。"""
    try:
        binding = resolve_template_binding(template_id.strip())
    except TemplateSelectionError as exc:
        await _close_uploads(docx, bib, citation_map)
        return JSONResponse(
            status_code=422,
            content={"error": "template_unavailable", "detail": str(exc)},
        )
    if not binding.bibtex_supported and (
        bib is not None or reference_source == "bib"
    ):
        await _close_uploads(docx, bib, citation_map)
        return JSONResponse(
            status_code=422,
            content={
                "error": "template_capability_mismatch",
                "detail": "所选模板不支持 BibTeX，请使用 Word 文末参考文献。",
            },
        )
    try:
        workspace = create_project_workspace()
    except WorkspaceUnavailableError as exc:
        # 工作区失败发生在读取上传内容前，仍需显式关闭所有临时上传句柄。
        await _close_uploads(docx, bib, citation_map)
        return JSONResponse(
            status_code=503,
            content={"error": "workspace_unavailable", "detail": str(exc)},
        )
    docx_path = workspace.inputs_dir / "source.docx"
    bib_path = workspace.inputs_dir / "references.bib" if bib else None
    map_path = workspace.inputs_dir / "citation-map.json" if citation_map else None
    try:
        _, source_hash = await save_upload(docx, docx_path, limit=DOCX_LIMIT)
        validate_docx(docx_path)
        if bib and bib_path:
            await save_upload(bib, bib_path, limit=BIB_LIMIT)
        if citation_map and map_path:
            await save_upload(citation_map, map_path, limit=MAP_LIMIT)
            validate_json_mapping(map_path)
    except UploadValidationError as exc:
        return JSONResponse(
            status_code=422,
            content={"error": "upload_invalid", "detail": str(exc)},
        )

    project = store.create(
        project_id=workspace.project_id, docx_path=str(docx_path),
        bib_path=str(bib_path) if bib_path else None,
        citation_map_path=str(map_path) if map_path else None,
        template_id=binding.template_id,
        reference_source=reference_source,
        workspace_dir=str(workspace.project_dir), source_sha256=source_hash,
    )
    store.update_extra(
        project.project_id,
        recognition_review_allowed=recognition_review_allowed,
    )
    return CreateProjectResponse(
        project_id=project.project_id,
        source_sha256=project.source_sha256,
        template_id=project.template_id,
    )
