# -*- coding: utf-8 -*-
"""G9-C 修订会话、反馈队列与批量处理接口。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from api.schemas import (
    AddRevisionFeedbackRequest, ProcessRevisionRequest,
    ProcessRevisionResponse, ResumeRevisionRequest, RevisionSessionResponse,
)
from revision.feedback import FeedbackContractError
from revision.feedback_goals import fail_feedback_submission, revision_context
from revision.service import (
    accept_revision, add_feedback, discard_revision, execute_revision_run,
    prepare_revision_run, remove_pending_feedback, revision_session_payload,
)
from persistence.store import ProjectStore, get_store
from infrastructure.tasks import TaskManager, get_task_manager
from generation.versions import VersionContractError


router = APIRouter(tags=["revision-sessions"])
logger = logging.getLogger(__name__)


@router.get('/api/projects/{project_id}/revision-context')
def get_revision_context(project_id: str, store: ProjectStore = Depends(get_store)):
    """提供正文和图表选择及脱敏模型说明，不包含内部源码路径或模型凭据。"""
    try:
        return revision_context(store, project_id)
    except KeyError:
        return JSONResponse(status_code=404, content={'detail': '项目不存在。'})
    except (ValueError, OSError, FeedbackContractError, VersionContractError):
        return JSONResponse(status_code=409, content={'detail': '当前可信版本的反馈来源暂不可读取，请刷新后重试。'})


##### 查询与反馈板块 #####


@router.get(
    "/api/projects/{project_id}/revision-session",
    response_model=RevisionSessionResponse,
)
def get_active_session(project_id: str, store: ProjectStore = Depends(get_store)):
    session = store.get_active_revision_session(project_id)
    if session is None:
        return JSONResponse(
            status_code=404,
            content={"error": "revision_session_not_found", "detail": "项目暂无进行中的修订会话。"},
        )
    return RevisionSessionResponse(**revision_session_payload(session, store))


@router.post(
    "/api/projects/{project_id}/revision-session/feedbacks",
    response_model=RevisionSessionResponse,
)
def create_feedback(
    project_id: str, body: AddRevisionFeedbackRequest,
    store: ProjectStore = Depends(get_store),
):
    try:
        session = add_feedback(
            store, project_id=project_id, feedback_text=body.feedback_text,
            selected_unit_ids=body.selected_unit_ids, page_number=body.page_number,
        )
    except KeyError:
        return JSONResponse(status_code=404, content={"detail": "项目不存在。"})
    except (FeedbackContractError, VersionContractError, ValueError) as exc:
        return JSONResponse(status_code=409, content={"detail": str(exc)})
    return RevisionSessionResponse(**revision_session_payload(session, store))


@router.delete(
    "/api/revision-sessions/{session_id}/feedbacks/{feedback_id}",
    response_model=RevisionSessionResponse,
)
def delete_feedback(
    session_id: str, feedback_id: str, store: ProjectStore = Depends(get_store),
):
    try:
        session = remove_pending_feedback(
            store, session_id=session_id, feedback_id=feedback_id,
        )
    except KeyError:
        return JSONResponse(status_code=404, content={"detail": "修订会话或反馈不存在。"})
    except FeedbackContractError as exc:
        return JSONResponse(status_code=409, content={"detail": str(exc)})
    return RevisionSessionResponse(**revision_session_payload(session, store))


##### 异步处理板块 #####


@router.post(
    "/api/revision-sessions/{session_id}/process",
    response_model=ProcessRevisionResponse,
    status_code=202,
)
def process_feedbacks(
    session_id: str, body: ProcessRevisionRequest,
    store: ProjectStore = Depends(get_store),
    tasks: TaskManager = Depends(get_task_manager),
):
    # 本轮已确认的表格工具保持离线；通用模型补丁接入时复用这里的授权字段。
    if body.allowed_llm_tasks and not body.external_processing_allowed:
        return JSONResponse(status_code=409, content={"detail": "外部模型任务必须显式授权外部处理。"})
    try:
        session, run = prepare_revision_run(
            store, session_id,
            external_processing_allowed=body.external_processing_allowed,
            allowed_llm_tasks=body.allowed_llm_tasks,
        )
        try:
            task_id = tasks.submit(
                execute_revision_run, store, session.session_id, run.run_id,
                project_id=session.project_id, task_type="revision_batch",
                linked_revision_run_id=run.run_id,
            )
        except Exception:
            logger.exception('反馈任务提交失败')
            fail_feedback_submission(store, run.run_id)
            return JSONResponse(status_code=500, content={"detail": "反馈任务提交失败，论文未修改，请刷新查看状态。"})
    except KeyError:
        return JSONResponse(status_code=404, content={"detail": "修订会话不存在。"})
    except (FeedbackContractError, VersionContractError, ValueError) as exc:
        return JSONResponse(status_code=409, content={"detail": str(exc)})
    return ProcessRevisionResponse(
        session_id=session.session_id, run_id=run.run_id, task_id=task_id,
    )


@router.post(
    "/api/revision-sessions/{session_id}/resume",
    response_model=ProcessRevisionResponse, status_code=202,
)
def resume_feedbacks(
    session_id: str, body: ResumeRevisionRequest,
    store: ProjectStore = Depends(get_store),
    tasks: TaskManager = Depends(get_task_manager),
):
    session = store.get_revision_session(session_id)
    if session is None:
        return JSONResponse(status_code=404, content={"detail": "修订会话不存在。"})
    run_id = session.state.get("current_run_id")
    run = store.get_agent_run(str(run_id)) if run_id else None
    if run is None or run.status != "waiting_user":
        return JSONResponse(status_code=409, content={"detail": "当前 Agent 不在等待补充状态。"})
    if run.state_revision != body.expected_run_revision:
        return JSONResponse(status_code=409, content={"detail": "Agent 状态已变化，请刷新后重试。"})
    resume_payload = {
        "clarifications": body.clarifications,
        "external_processing_allowed": body.external_processing_allowed,
        "allowed_llm_tasks": body.allowed_llm_tasks,
        "goal_decisions": body.goal_decisions,
    }
    store.update_revision_session(
        session_id, expected_revision=session.state_revision,
        status="queued",
        state={**session.state, "current_task_id": None, "detail": "补充信息已提交，正在恢复同一 Agent 运行。"},
        head_generation_id=session.head_generation_id,
    )
    try:
        task_id = tasks.submit(
            execute_revision_run, store, session_id, run.run_id,
            resume_payload=resume_payload,
            project_id=session.project_id, task_type="revision_resume",
            linked_revision_run_id=run.run_id,
        )
    except Exception:
        logger.exception('反馈恢复任务提交失败')
        fail_feedback_submission(store, run.run_id)
        return JSONResponse(status_code=500, content={"detail": "反馈恢复任务提交失败，论文未修改，请刷新查看状态。"})
    return ProcessRevisionResponse(
        session_id=session_id, run_id=run.run_id, task_id=task_id,
    )


##### 接受与放弃板块 #####


@router.post(
    "/api/revision-sessions/{session_id}/accept",
    response_model=RevisionSessionResponse,
)
def accept_current(session_id: str, store: ProjectStore = Depends(get_store)):
    try:
        session = accept_revision(store, session_id)
    except KeyError:
        return JSONResponse(status_code=404, content={"detail": "修订会话不存在。"})
    except (FeedbackContractError, VersionContractError, ValueError) as exc:
        return JSONResponse(status_code=409, content={"detail": str(exc)})
    return RevisionSessionResponse(**revision_session_payload(session, store))


@router.post(
    "/api/revision-sessions/{session_id}/discard",
    response_model=RevisionSessionResponse,
)
def discard_current(session_id: str, store: ProjectStore = Depends(get_store)):
    try:
        session = discard_revision(store, session_id)
    except KeyError:
        return JSONResponse(status_code=404, content={"detail": "修订会话不存在。"})
    except (FeedbackContractError, ValueError) as exc:
        return JSONResponse(status_code=409, content={"detail": str(exc)})
    return RevisionSessionResponse(**revision_session_payload(session, store))
