# -*- coding: utf-8 -*-
"""修订会话、单次反馈理解、目标确认与统一质量修复调度。"""

from __future__ import annotations

import uuid
from typing import Any


from revision.feedback import FeedbackContractError
from revision.feedback_goals import prepare_revision_run
from persistence.store import AgentRun, ProjectStore, RevisionSession
from generation.versions import validate_trusted_generation


##### 会话模型板块 #####


TERMINAL_SESSION_STATES = {"accepted", "discarded"}
BUSY_SESSION_STATES = {"queued", "running"}


def revision_session_payload(session: RevisionSession, store: ProjectStore | None = None) -> dict[str, Any]:
    state = session.state
    run = store.get_agent_run(state['current_run_id']) if store and state.get('current_run_id') else None
    return {
        "session_id": session.session_id,
        "project_id": session.project_id,
        "base_version_id": session.base_generation_id,
        "current_version_id": session.head_generation_id,
        "status": session.status,
        "state_revision": session.state_revision,
        "feedbacks": state.get("feedbacks", []),
        "current_run_id": state.get("current_run_id"),
        "current_run_revision": state.get("current_run_revision"),
        "unresolved": state.get("unresolved", []),
        "current_task_id": state.get("current_task_id"),
        "evaluation_summary": state.get("evaluation_summary", {}),
        "constraint_candidates": state.get("constraint_candidates", []),
        "goal_drafts": run.state.get('goal_drafts', []) if run else [],
        "goals": run.state.get('goals', []) if run else [],
        "detail": state.get("detail", ""),
        "created_at": session.created_at,
        "updated_at": session.updated_at,
    }


def _new_feedback(
    text: str, selected_unit_ids: list[str], page_number: int | None,
) -> dict[str, Any]:
    return {
        "feedback_id": str(uuid.uuid4()),
        "text": text.strip(),
        "selected_unit_ids": list(dict.fromkeys(selected_unit_ids)),
        "page_number": page_number,
        "status": "pending",
        "detail": "等待批量处理。",
        "target_references": [],
    }


##### 反馈收集板块 #####


def add_feedback(
    store: ProjectStore, *, project_id: str, feedback_text: str,
    selected_unit_ids: list[str], page_number: int | None,
) -> RevisionSession:
    project = store.get(project_id)
    if project is None:
        raise KeyError(project_id)
    if not project.accepted_generation_id:
        raise FeedbackContractError("项目尚无用户接受版本，不能开始修订。")
    session = store.get_active_revision_session(project_id)
    if session is None:
        base = store.get_generation(project.accepted_generation_id)
        if base is None:
            raise FeedbackContractError("项目接受版本不存在。")
        validate_trusted_generation(base, project)
        session = store.create_revision_session(
            session_id=str(uuid.uuid4()), project_id=project_id,
            base_generation_id=base.generation_id,
            state={"feedbacks": [], "detail": "可以继续添加反馈。"},
        )
    if session.status in BUSY_SESSION_STATES:
        raise FeedbackContractError("Agent 正在处理当前批次，请完成后再添加反馈。")
    state = dict(session.state)
    feedbacks = list(state.get("feedbacks", []))
    feedbacks.append(_new_feedback(
        feedback_text, selected_unit_ids, page_number,
    ))
    state.update({
        "feedbacks": feedbacks,
        "detail": f"已收集 {sum(item['status'] == 'pending' for item in feedbacks)} 条待处理反馈。",
    })
    return store.update_revision_session(
        session.session_id, expected_revision=session.state_revision,
        status="collecting", state=state,
        head_generation_id=session.head_generation_id,
    )


def remove_pending_feedback(
    store: ProjectStore, *, session_id: str, feedback_id: str,
) -> RevisionSession:
    session = store.get_revision_session(session_id)
    if session is None:
        raise KeyError(session_id)
    if session.status in BUSY_SESSION_STATES:
        raise FeedbackContractError("Agent 运行期间不能删除反馈。")
    state = dict(session.state)
    feedbacks = list(state.get("feedbacks", []))
    matched = next((item for item in feedbacks if item["feedback_id"] == feedback_id), None)
    if matched is None:
        raise KeyError(feedback_id)
    if matched.get("status") != "pending":
        raise FeedbackContractError("只能删除尚未处理的反馈。")
    state["feedbacks"] = [
        item for item in feedbacks if item["feedback_id"] != feedback_id
    ]
    state["detail"] = "待处理反馈已更新。"
    return store.update_revision_session(
        session_id, expected_revision=session.state_revision,
        status="collecting", state=state,
        head_generation_id=session.head_generation_id,
    )


##### 批量执行板块 #####




def _feedback_model(run: AgentRun, task: str):
    """只绑定本次已授权的具体任务，不继承父版本或上一批的模型权限。"""
    from infrastructure.stage1_adapter import _ensure_stage1_on_path
    _ensure_stage1_on_path()
    from config import get_settings
    from llm.client import LLMClient
    from llm.policy import LLMAuthorization
    from agents.quality_repair.runtime import AuthorizedPlanningModel
    values = run.state.get('authorization', {})
    authorization = LLMAuthorization.for_tasks(values.get('allowed_llm_tasks', []),
        external_processing_allowed=values.get('external_processing_allowed') is True,
        source='confirmed_revision_request')
    if not authorization.allows_task(task):
        return None
    settings = get_settings()
    return AuthorizedPlanningModel(LLMClient(settings), authorization, task) if settings.llm_configured else None


def _sync_feedback_session(store, run):
    """会话只保存阶段和展示摘要，目标草稿与运行事实仍从同一 AgentRun 读取。"""
    session = store.get_revision_session(run.state['session_id'])
    if session is None or session.state.get('current_run_id') != run.run_id:
        raise FeedbackContractError('反馈会话或当前批次已经变化。')
    if run.candidate_generation_id:
        return revision_session_payload(session, store)
    status = 'needs_user_input' if run.status == 'waiting_user' else 'failed'
    if run.status == 'running':
        status = 'running'
    if run.status == 'stopped' and run.state.get('stop_reason') == 'user_discarded_goals':
        status = 'collecting'
    state = {**session.state, 'current_run_revision': run.state_revision,
             'detail': run.state.get('detail', ''), 'unresolved': []}
    if status == 'collecting':
        ids = set(run.state.get('feedback_ids', []))
        state['feedbacks'] = [{**item, 'status': 'resolved', 'detail': '已由用户放弃，论文未修改。'}
                             if item['feedback_id'] in ids else item for item in state.get('feedbacks', [])]
    session = store.update_revision_session(session.session_id, expected_revision=session.state_revision,
        status=status, state=state, head_generation_id=session.head_generation_id)
    return revision_session_payload(session, store)


def execute_revision_run(
    store: ProjectStore, session_id: str, run_id: str,
    *, resume_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """每批先理解一次并确认目标；已确认目标才进入统一图和私有候选终验。"""
    from revision.feedback_goals import (
        _context, _persist_goal_state, load_feedback_evidence,
        normalize_feedback_goals, confirm_feedback_goals,
    )
    from revision.feedback import execute_confirmed_feedback
    from llm.policy import FEEDBACK_NORMALIZATION_TASK, USER_FEEDBACK_PATCH_TASK

    run = store.get_agent_run(run_id)
    if run is None or run.state.get('session_id') != session_id:
        raise FeedbackContractError('修订会话或反馈运行不存在。')
    session = store.get_revision_session(session_id)
    if run.candidate_generation_id or (run.status == 'waiting_user' and resume_payload is None):
        return revision_session_payload(session, store)
    run, session = _context(store, run_id, run.state_revision)
    if run.status not in {'running', 'waiting_user'}:
        raise FeedbackContractError('当前反馈运行状态不能重新执行。')
    try:
        if resume_payload is not None:
            if run.status != 'waiting_user':
                raise FeedbackContractError('仅能恢复正在等待用户的同一运行。')
            authorization = {'external_processing_allowed': resume_payload.get('external_processing_allowed') is True,
                             'allowed_llm_tasks': list(resume_payload.get('allowed_llm_tasks', []))}
            selections = resume_payload.get('clarifications', {})
            if (not isinstance(selections, dict) or set(selections) - set(run.state['feedback_ids'])
                or any(not isinstance(ids, list) or not 1 <= len(ids) <= 20
                       or any(not isinstance(identifier, str) or not identifier for identifier in ids)
                       or len(ids) != len(set(ids)) for ids in selections.values())):
                raise FeedbackContractError('补充目标必须对应本批已有反馈和明确内容单元。')
            run = _persist_goal_state(store, run, status=run.status,
                state={**run.state, 'authorization': authorization,
                       'target_selections': {**run.state.get('target_selections', {}), **selections}})
        session = store.get_revision_session(session_id)
        store.update_revision_session(session_id, expected_revision=session.state_revision,
            status='running', state={**session.state, 'detail': '正在处理当前已提交反馈。'},
            head_generation_id=session.head_generation_id)
        if run.state.get('normalization_status') != 'completed':
            authorization = run.state.get('authorization', {})
            if (authorization.get('external_processing_allowed') is not True
                or FEEDBACK_NORMALIZATION_TASK not in authorization.get('allowed_llm_tasks', [])):
                run = _persist_goal_state(store, run, status='waiting_user',
                    state={**run.state, 'stop_reason': 'normalization_authorization',
                        'detail': '理解本批反馈需要一次模型调用，仅发送反馈和所选内容片段，请确认本次处理。'})
                return _sync_feedback_session(store, run)
            evidence = load_feedback_evidence(store, run_id, expected_revision=run.state_revision)
            run = normalize_feedback_goals(store, run_id, expected_revision=run.state_revision,
                model=_feedback_model(run, FEEDBACK_NORMALIZATION_TASK), evidence=evidence)
            return _sync_feedback_session(store, run)
        evidence = load_feedback_evidence(store, run_id, expected_revision=run.state_revision)
        if run.state.get('stop_reason') == 'goal_confirmation':
            decisions = (resume_payload or {}).get('goal_decisions')
            if not decisions:
                return _sync_feedback_session(store, run)
            run = confirm_feedback_goals(store, run_id, expected_revision=run.state_revision,
                decisions=decisions, evidence=evidence)
        if run.status == 'stopped':
            return _sync_feedback_session(store, run)
        if not run.state.get('goals'):
            raise FeedbackContractError('本批尚无用户确认的执行目标。')
        authorization = run.state.get('authorization', {})
        needs_model = any(goal['kind'] == 'format' for goal in run.state['goals'])
        model = None
        if (needs_model and authorization.get('external_processing_allowed') is True
            and USER_FEEDBACK_PATCH_TASK in authorization.get('allowed_llm_tasks', [])):
            model = _feedback_model(run, USER_FEEDBACK_PATCH_TASK)
        result = execute_confirmed_feedback(store, run_id, model=model, resume='agent' in run.state)
        return _sync_feedback_session(store, result)
    except Exception:
        latest = store.get_agent_run(run_id)
        if latest is not None and latest.candidate_generation_id is None:
            if latest.status != 'failed':
                latest = store.update_agent_run(run_id, expected_revision=latest.state_revision, status='failed',
                    state={**latest.state, 'stop_reason': 'feedback_execution_failed',
                        'detail': '本批反馈处理未完成，未交付候选，用户接受版本保持不变。'},
                    candidate_generation_id=None)
            _sync_feedback_session(store, latest)
        raise


##### 接受与放弃板块 #####


def accept_revision(store: ProjectStore, session_id: str) -> RevisionSession:
    session = store.get_revision_session(session_id)
    if session is None:
        raise KeyError(session_id)
    if session.status not in {"reviewable", "accepted"} or not session.head_generation_id:
        raise FeedbackContractError("当前修订尚无可接受的技术可信版本。")
    store.accept_generation(session.head_generation_id, session_id=session_id,
                            expected_session_revision=session.state_revision)
    updated = store.get_revision_session(session_id)
    if updated is None:
        raise RuntimeError('接受事务提交后无法读取修订会话。')
    return updated


def discard_revision(store: ProjectStore, session_id: str) -> RevisionSession:
    from generation.transactions import discard_revision_session
    from revision.feedback import discard_private_feedback

    session = store.get_revision_session(session_id)
    if session is None:
        raise KeyError(session_id)
    if session.status in BUSY_SESSION_STATES:
        raise FeedbackContractError("Agent 运行期间不能放弃修订。")
    discarded = discard_revision_session(store, session_id, expected_revision=session.state_revision)
    discard_private_feedback(store, discarded)
    return discarded
