# -*- coding: utf-8 -*-
"""提取识别任务提交路由。"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from api.schemas import TriggerExtractResponse
from infrastructure.stage1_adapter import run_extract_and_recognize
from persistence.store import ProjectStore, get_store
from infrastructure.tasks import TaskManager, get_task_manager


##### 路由定义板块 #####


router = APIRouter(prefix="/api/projects", tags=["extract"])


@router.post("/{project_id}/extract", response_model=TriggerExtractResponse)
def trigger_extract(
    project_id: str,
    store: ProjectStore = Depends(get_store),
    tasks: TaskManager = Depends(get_task_manager),
):
    project = store.get(project_id)
    if project is None:
        return JSONResponse(
            status_code=404,
            content={"error": "project_not_found", "detail": "project_id 不存在"},
        )
    task_id = tasks.submit(
        run_extract_and_recognize,
        docx_path=project.docx_path,
        project_dir=project.workspace_dir,
        source_sha256=project.source_sha256,
        project_id=project_id,
        task_type="extract",
    )
    store.update_extra(project_id, extract_task_id=task_id)
    return TriggerExtractResponse(task_id=task_id)
