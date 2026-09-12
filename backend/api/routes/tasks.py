# -*- coding: utf-8 -*-
"""统一任务轮询路由。"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from api.schemas import TaskResponse
from infrastructure.tasks import TaskManager, TaskStatus, get_task_manager


##### 路由模型板块 #####


router = APIRouter(prefix="/api/tasks", tags=["tasks"])


##### 路由定义板块 #####


@router.get("/{task_id}", response_model=TaskResponse)
def get_task(
    task_id: str,
    tasks: TaskManager = Depends(get_task_manager),
):
    task = tasks.get(task_id)
    if task is None:
        return JSONResponse(
            status_code=404,
            content={"error": "task_not_found", "detail": "任务不存在或已清理"},
        )
    message = "后台任务未能完成，请查看生成状态或联系维护人员。" if task.status == TaskStatus.FAILED else None
    return TaskResponse(
        task_id=task.task_id, project_id=task.project_id,
        task_type=task.task_type, status=task.status.value,
        result=task.result if task.status == TaskStatus.DONE else None,
        user_message=message,
    )
