# -*- coding: utf-8 -*-
"""统一 Agent 的结构识别审查启动、查询与恢复接口。"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from api.schemas import (
    RecognitionReviewResponse, ResumeRecognitionReviewRequest,
)
from recognition.review import (
    RecognitionReviewError, execute_recognition_review,
    prepare_recognition_review, recognition_review_payload,
    resume_recognition_review,
)
from api.routes.structure import _public_snapshot
from persistence.store import ProjectStore, get_store
from recognition.structure import StructureValidationError
from infrastructure.tasks import TaskManager, get_task_manager


router = APIRouter(tags=["recognition-reviews"])


@router.post(
    "/api/projects/{project_id}/recognition-review",
    response_model=RecognitionReviewResponse, status_code=202,
)
def start_review(
    project_id: str, store: ProjectStore = Depends(get_store),
    tasks: TaskManager = Depends(get_task_manager),
):
    try:
        existing = next((
            item for item in store.list_agent_runs(project_id)
            if item.mode == "recognition_review"
            and item.status in {"running", "waiting_user"}
        ), None)
        if existing is not None:
            return RecognitionReviewResponse(**recognition_review_payload(existing))
        run = prepare_recognition_review(store, project_id)
        task_id = tasks.submit(
            execute_recognition_review, store, run.run_id,
            project_id=project_id, task_type="recognition_review",
        )
    except KeyError:
        return JSONResponse(status_code=404, content={"detail": "项目不存在。"})
    except RecognitionReviewError as exc:
        return JSONResponse(status_code=409, content={"detail": str(exc)})
    return RecognitionReviewResponse(**recognition_review_payload(run), task_id=task_id)


@router.get(
    "/api/recognition-reviews/{run_id}", response_model=RecognitionReviewResponse,
)
def get_review(run_id: str, store: ProjectStore = Depends(get_store)):
    run = store.get_agent_run(run_id)
    if run is None or run.mode != "recognition_review":
        return JSONResponse(status_code=404, content={"detail": "结构审查运行不存在。"})
    return RecognitionReviewResponse(**recognition_review_payload(run))


@router.post(
    "/api/recognition-reviews/{run_id}/resume",
    response_model=RecognitionReviewResponse,
)
def resume_review(
    run_id: str, body: ResumeRecognitionReviewRequest,
    store: ProjectStore = Depends(get_store),
):
    try:
        run, snapshot = resume_recognition_review(
            store, run_id, expected_state_revision=body.expected_state_revision,
            decisions=body.decisions,
            object_binding_overrides=[item.model_dump() for item in body.object_binding_overrides],
            citation_overrides=[item.model_dump() for item in body.citation_overrides],
        )
        project = store.get(run.project_id)
        assert project is not None
    except KeyError:
        return JSONResponse(status_code=404, content={"detail": "结构审查运行不存在。"})
    except (RecognitionReviewError, StructureValidationError, ValueError) as exc:
        return JSONResponse(status_code=409, content={"detail": str(exc)})
    return RecognitionReviewResponse(
        **recognition_review_payload(run), structure=_public_snapshot(project, snapshot)
    )
