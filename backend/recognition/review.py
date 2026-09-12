# -*- coding: utf-8 -*-
"""结构识别审查的运行准备、统一图执行与集中确认事务。"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from infrastructure.stage1_adapter import _ensure_stage1_on_path
from persistence.store import AgentRun, Project, ProjectStore
from recognition.structure import commit_structure_confirmation, read_structure_snapshot


##### 证据与运行依赖板块 #####

class RecognitionReviewError(ValueError):
    pass


def _contracts():
    _ensure_stage1_on_path()
    from agents.quality_repair.runtime import AuthorizedPlanningModel
    from agents.quality_repair.models import AgentState
    from agents.quality_repair.graph_executor import build_quality_graph
    return AuthorizedPlanningModel, AgentState, build_quality_graph


def _checkpoint_path(project: Project) -> Path:
    target = Path(project.workspace_dir) / "reports" / "agent-checkpoints.sqlite"
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def _pending_candidates(snapshot):
    return [item for item in snapshot.get("headings", [])
            if item.get("requires_review") and item.get("review_status") != "resolved"]


def _bounded_evidence(snapshot):
    """只保留候选及前后两段，不发送全篇结构、文件路径或引用决定。"""
    paragraphs = snapshot.get("paragraph_evidence", [])
    positions = {item.get("unit_id"): index for index, item in enumerate(paragraphs)}
    result = []
    for candidate in _pending_candidates(snapshot):
        center = positions.get(candidate["unit_id"])
        neighbors = [] if center is None else [
            {"unit_id": item.get("unit_id"), "text": str(item.get("text", ""))[:300],
             "style": str(item.get("style", ""))[:80]}
            for item in paragraphs[max(0, center - 2):center + 3]]
        result.append({"unit_id": candidate["unit_id"], "role": "heading_candidate",
            "text": str(candidate.get("text", ""))[:200], "requires_review": True,
            "evidence": [{"level": candidate.get("level"), "suggested_level": candidate.get("suggested_level"),
                          "signals": list(candidate.get("evidence", []))[:10], "neighbors": neighbors}]})
    return result


def _runtime(snapshot, *, project, store, model):
    from agents.quality_repair.runtime import RepairRuntime
    from agents.quality_repair.tools import QualityTools
    evidence = _bounded_evidence(snapshot)
    tools = QualityTools(root=None, generation_id="", regions=[], evidence=evidence,
                         template_context={"template_id": project.template_id})
    def forbidden(*args):
        raise RecognitionReviewError("结构审查不能修改文件或执行发布门禁。")
    def parent_identity():
        current = store.get(project.project_id)
        return None, current.source_sha256 if current else ""
    return RepairRuntime(None, model, tools,
        lambda state: {"goals": state.goals, "evidence": evidence,
                       "planning_round": state.planning_round,
                       "last_observation": state.observations[-1] if state.observations else None},
        forbidden, forbidden, lambda: None, parent_identity,
        authorized=lambda state: model is not None,
        journal_root=Path(project.workspace_dir) / "reports" / "agent-actions")


def _configured_model(project):
    if not project.extra.get("recognition_review_allowed"):
        return None
    _ensure_stage1_on_path()
    from config import get_settings
    from llm.client import LLMClient
    from llm.policy import LLMAuthorization, RECOGNITION_REVIEW_TASK
    settings = get_settings()
    if not settings.llm_configured:
        return None
    adapter, _state, _builder = _contracts()
    authorization = LLMAuthorization.for_tasks(
        [RECOGNITION_REVIEW_TASK], external_processing_allowed=True,
        source="project_upload_consent")
    return adapter(LLMClient(settings), authorization, RECOGNITION_REVIEW_TASK)


def _snapshot(project, run=None):
    snapshot = read_structure_snapshot(project)
    if snapshot is None:
        raise RecognitionReviewError("结构快照不存在。")
    if run and (
        snapshot.get("revision") != run.state["base_revision"]
        or snapshot.get("schema_version") != run.state["schema_version"]
        or project.source_sha256 != run.state["agent"]["parent_source_sha256"]
    ):
        raise RecognitionReviewError("源文件或结构版本已变化，不能恢复旧审查。")
    return snapshot


##### 统一图执行板块 #####

def prepare_recognition_review(store: ProjectStore, project_id: str) -> AgentRun:
    project = store.get(project_id)
    if project is None:
        raise KeyError(project_id)
    snapshot = _snapshot(project)
    if snapshot.get("confirmed"):
        raise RecognitionReviewError("结构已经确认，无需重复启动识别审查。")
    if not _pending_candidates(snapshot):
        raise RecognitionReviewError("没有需要审查的低置信度标题。")
    existing = next((run for run in store.list_agent_runs(project_id)
        if run.mode == "recognition_review" and run.status in {"running", "waiting_user"}), None)
    if existing:
        return existing
    _adapter, state_type, _builder = _contracts()
    run_id = str(uuid.uuid4())
    agent = state_type(run_id=run_id, project_id=project_id, mode="recognition_review",
        template_id=project.template_id, parent_source_sha256=project.source_sha256,
        goals=[{"goal_id": item["unit_id"], "target_unit_ids": [item["unit_id"]],
                "status": "pending", "description": "审查现有标题证据并提出层级建议"}
               for item in _pending_candidates(snapshot)], detail="结构审查已进入队列。")
    return store.create_agent_run(run_id=run_id, project_id=project_id, parent_generation_id="",
        status="running", mode="recognition_review", state={
            "agent": agent.to_dict(), "schema_version": snapshot.get("schema_version", ""),
            "base_revision": snapshot.get("revision", 0), "model_status": "pending"})


def execute_recognition_review(store: ProjectStore, run_id: str, *, model: Any = None) -> dict:
    run = store.get_agent_run(run_id)
    if run is None or run.mode != "recognition_review":
        raise RecognitionReviewError("结构审查运行不存在。")
    if run.status != "running":
        return recognition_review_payload(run)
    project = store.get(run.project_id)
    if project is None:
        raise RecognitionReviewError("项目不存在。")
    try:
        snapshot = _snapshot(project, run)
        selected = model if model is not None else _configured_model(project)
        _adapter, _state, builder = _contracts()
        config = {"configurable": {"thread_id": run_id}}
        with SqliteSaver.from_conn_string(str(_checkpoint_path(project))) as saver:
            graph = builder(runtime=_runtime(snapshot, project=project, store=store, model=selected),
                            checkpointer=saver)
            saved = graph.get_state(config)
            result = graph.invoke(None if saved.values else {"agent": run.state["agent"]}, config=config)
        fresh = store.get(project.project_id)
        if fresh is None:
            raise RecognitionReviewError("项目不存在。")
        _snapshot(fresh, run)
        agent = result["agent"]
        model_status = ("not_configured" if selected is None else
                        "failed" if agent["status"] in {"failed", "stopped"} else "succeeded")
        updated = store.update_agent_run(run_id, expected_revision=run.state_revision,
            status=agent["status"], state={**run.state, "agent": agent, "model_status": model_status},
            candidate_generation_id=None)
        return recognition_review_payload(updated)
    except Exception as exc:
        agent = {**run.state["agent"], "status": "failed", "stop_reason": "internal_error",
                 "detail": "结构审查未完成，原确认结构未改变。"}
        try:
            store.update_agent_run(run_id, expected_revision=run.state_revision, status="failed",
                state={**run.state, "agent": agent, "model_status": "failed"}, candidate_generation_id=None)
        except Exception as persistence_error:
            exc.add_note(f"结构审查失败状态写入异常：{type(persistence_error).__name__}")
        raise


##### 集中确认与公开结果板块 #####

def resume_recognition_review(
    store: ProjectStore, run_id: str, *, expected_state_revision: int,
    decisions: dict[str, str], citation_overrides: list[dict] | None = None,
    object_binding_overrides: list[dict] | None = None,
) -> tuple[AgentRun, dict]:
    run = store.get_agent_run(run_id)
    if run is None or run.mode != "recognition_review":
        raise KeyError(run_id)
    if run.state_revision != expected_state_revision:
        raise RecognitionReviewError("Agent 审查状态已变化，请刷新后重试。")
    if run.status != "waiting_user":
        raise RecognitionReviewError("当前运行不处于等待用户确认状态。")
    project = store.get(run.project_id)
    if project is None:
        raise RecognitionReviewError("项目不存在。")
    snapshot = _snapshot(project, run)
    parameters = dict(project=project, store=store, base_revision=int(run.state["base_revision"]),
        heading_overrides=[{"unit_id": key, "level": value} for key, value in decisions.items()],
        citation_overrides=citation_overrides, object_binding_overrides=object_binding_overrides)
    # 引用、题注与标题决定只进入确定性事务，错误输入不能推进 checkpoint。
    commit_structure_confirmation(**parameters, validate_only=True)
    _adapter, _state, builder = _contracts()
    with SqliteSaver.from_conn_string(str(_checkpoint_path(project))) as saver:
        graph = builder(runtime=_runtime(snapshot, project=project, store=store, model=None), checkpointer=saver)
        result = graph.invoke(Command(resume={"continue": True, "structure_confirmed": True}),
                              config={"configurable": {"thread_id": run_id}})
    if result["agent"]["status"] != "ready":
        raise RecognitionReviewError("结构审查没有完成确认恢复。")
    confirmed = commit_structure_confirmation(**parameters, confirmed=True)
    updated = store.update_agent_run(run_id, expected_revision=run.state_revision, status="ready",
        state={**run.state, "agent": result["agent"]}, candidate_generation_id=None)
    return updated, confirmed


def recognition_review_payload(run: AgentRun) -> dict:
    agent = run.state["agent"]
    return {
        "run_id": run.run_id, "project_id": run.project_id, "mode": run.mode,
        "status": run.status, "state_revision": run.state_revision,
        "base_revision": run.state.get("base_revision", 0),
        "suggestions": agent.get("suggestions", []),
        "model_status": run.state.get("model_status", "pending"),
        "detail": agent.get("detail", ""),
        "created_at": run.created_at, "updated_at": run.updated_at,
    }
