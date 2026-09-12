# -*- coding: utf-8 -*-
"""生成任务提升为可信版本的单一事务边界，不重新计算产物哈希。"""

from datetime import datetime, timezone
import json
from pathlib import Path

from generation.artifacts import validate_delivery_manifest
from constraints import repository as constraint_repository


##### 完整交付校验板块 #####

def validate_publication(project, generation_id, *, status, quality_status, output_dir,
                         report_dir, artifact_manifest, gate_snapshot, detail=""):
    """结构化门禁、实际发布标记与清单必须同时成立。"""
    if (status, quality_status) not in {("success", "passed"), ("degraded", "degraded")}:
        raise ValueError("可信状态与最终质量状态不一致。")
    if not isinstance(gate_snapshot, dict) or not isinstance(gate_snapshot.get("gate_statuses"), dict):
        raise ValueError("完整终验快照无效。")
    gates = gate_snapshot["gate_statuses"]
    issues = gate_snapshot.get("issues", [])
    if (set(gates) != {"content-fidelity", "structure", "compile", "format"}
        or not isinstance(issues, list) or any(not isinstance(item, dict) for item in issues)):
        raise ValueError("终验状态或问题清单不符合完整交付契约。")
    if quality_status == "passed" and "degraded" in gates.values():
        raise ValueError("质量通过状态不能隐藏实际门禁的降级。")
    if (gate_snapshot.get("published") is not True
        or gates.get("content-fidelity") not in {"passed", "degraded"}
        or gates.get("structure") != "passed" or gates.get("compile") != "passed"
        or gates.get("format") not in {"passed", "degraded"}
        or any(item.get("severity") == "block" for item in issues)):
        raise ValueError("完整终验和实际发布尚未通过。")
    if not report_dir:
        raise ValueError("可信交付缺少报告目录。")
    validate_delivery_manifest(workspace_dir=project["workspace_dir"], generation_id=generation_id,
        output_dir=output_dir, report_dir=report_dir, manifest=artifact_manifest)
    marker_path = Path(output_dir) / ".scholar-generation.json"
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        valid = (marker.get("status") == "published" and marker.get("project_id") == project["project_id"]
                 and marker.get("generation_id") == generation_id)
    except (OSError, ValueError, AttributeError) as exc:
        raise ValueError("正式产物所有权标记不可读。") from exc
    if not valid:
        raise ValueError("正式产物标记与项目或 generation 不一致。")


##### 原子可信提升板块 #####

def publish_trusted_generation(store, generation_id, *, status, quality_status, output_dir,
                               report_dir, artifact_manifest, gate_snapshot, detail=""):
    """只提升未可信的排队或运行记录；异常由 SQLite 回滚全部字段。"""
    with store._write_lock, store._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT * FROM generations WHERE generation_id = ?", (generation_id,)).fetchone()
        if row is None:
            raise KeyError(generation_id)
        if row["trusted"] or row["review_status"] != "not_reviewable" or row["status"] not in {"queued", "running"}:
            raise ValueError("只能提升尚未完成的生成任务记录。")
        project = connection.execute("SELECT * FROM projects WHERE project_id = ?", (row["project_id"],)).fetchone()
        if project is None or row["source_sha256"] != project["source_sha256"]:
            raise ValueError("生成任务所属项目或源哈希不一致。")
        validate_publication(project, generation_id, status=status, quality_status=quality_status,
                             output_dir=output_dir, report_dir=report_dir, artifact_manifest=artifact_manifest,
                             gate_snapshot=gate_snapshot, detail=detail)
        _write_trusted_row(connection, generation_id, status=status, quality_status=quality_status,
                           output_dir=output_dir, report_dir=report_dir, artifact_manifest=artifact_manifest,
                           gate_snapshot=gate_snapshot, detail=detail)
    return store.get_generation(generation_id)


def _write_trusted_row(connection, generation_id, *, status, quality_status, output_dir,
                       report_dir, artifact_manifest, gate_snapshot, detail=""):
    connection.execute("""UPDATE generations SET status = ?, quality_status = ?, output_dir = ?,
            report_dir = ?, artifact_manifest_json = ?, gate_snapshot_json = ?, trusted = 1,
            review_status = 'pending', rollback_target_id = NULL, detail = ?, updated_at = ?
            WHERE generation_id = ?""", (status, quality_status, output_dir, report_dir,
            json.dumps(artifact_manifest, ensure_ascii=False), json.dumps(gate_snapshot, ensure_ascii=False),
            detail, datetime.now(timezone.utc).isoformat(), generation_id))


##### 反馈候选登记板块 #####

def _validate_feedback_context(connection, run, project):
    """在授信事务内重查父版本与当前会话，拒绝已经失效的晚到结果。"""
    parent = connection.execute('SELECT * FROM generations WHERE generation_id = ?',
                                (run['parent_generation_id'],)).fetchone()
    if (parent is None or parent['project_id'] != project['project_id']
        or parent['source_sha256'] != project['source_sha256'] or not parent['trusted']
        or parent['status'] not in {'success', 'degraded'}
        or parent['review_status'] not in {'accepted', 'pending'}):
        raise ValueError('反馈父版本已经失效，不能登记候选。')
    state = json.loads(run['state_json'])
    if not isinstance(state, dict) or not state.get('session_id'):
        raise ValueError('反馈运行缺少有效会话状态。')
    session = connection.execute('SELECT * FROM revision_sessions WHERE session_id = ?',
                                 (state['session_id'],)).fetchone()
    session_state = json.loads(session['state_json']) if session else None
    if (session is None or session['project_id'] != project['project_id']
        or session['status'] != 'running' or not isinstance(session_state, dict)
        or session_state.get('current_run_id') != run['run_id']
        or project['accepted_generation_id'] != session['base_generation_id']
        or (session['head_generation_id'] or session['base_generation_id']) != parent['generation_id']):
        raise ValueError('反馈会话或当前批次已经变化，不能登记旧候选。')


def _complete_confirmed_session(connection, run, generation_id, delivery):
    """统一图的终态与会话头随候选授信同时落地；接受指针仍由用户评审事务更新。"""
    state = json.loads(run['state_json'])
    agent = state.get('agent')
    if (not isinstance(agent, dict) or agent.get('status') != 'ready'
        or agent.get('mode') != 'user_feedback' or agent.get('run_id') != run['run_id']
        or agent.get('project_id') != run['project_id']
        or agent.get('parent_generation_id') != run['parent_generation_id']
        or not agent.get('goals')
        or any(goal.get('confirmed') is not True or goal.get('status') != 'satisfied' for goal in agent['goals'])
        or agent.get('gate_snapshots', {}).get('final', {}).get('gate_statuses') != delivery['gate_snapshot']['gate_statuses']):
        raise ValueError('反馈统一运行尚未满足确认目标与完整终验，不能提交会话头。')
    session = connection.execute('SELECT * FROM revision_sessions WHERE session_id = ?',
                                 (state.get('session_id'),)).fetchone()
    if session is None:
        raise ValueError('反馈统一运行缺少所属修订会话。')
    session_state = json.loads(session['state_json'])
    feedback_ids = state.get('feedback_ids', [])
    feedbacks = session_state.get('feedbacks', [])
    if not feedback_ids or set(feedback_ids) - {item['feedback_id'] for item in feedbacks}:
        raise ValueError('已完成目标缺少本批原始反馈来源。')
    constraint_repository.insert_completed_protections(connection, run=run, state=state,
        session=session, generation_id=generation_id, now=datetime.now(timezone.utc).isoformat())
    session_state.update(current_run_revision=run['state_revision'] + 1, unresolved=[],
        detail='本批目标已执行并通过完整终验，请核对候选 PDF。',
        feedbacks=[{**item, 'status': 'resolved', 'detail': '已随候选通过完整终验。'}
                   if item['feedback_id'] in feedback_ids else item for item in feedbacks])
    connection.execute("""UPDATE revision_sessions SET status = 'reviewable', head_generation_id = ?,
        state_json = ?, state_revision = state_revision + 1, updated_at = ? WHERE session_id = ?""",
        (generation_id, json.dumps(session_state, ensure_ascii=False), datetime.now(timezone.utc).isoformat(),
         session['session_id']))


def register_trusted_feedback(store, *, identity: dict, delivery: dict, run_id: str):
    """生成编号、候选授信与运行关联在同一事务内完成，失败时没有候选行。"""
    if identity.get("change_origin") != "user_feedback" or not identity.get("parent_generation_id"):
        raise ValueError("反馈候选必须指定可信父版本和反馈来源。")
    generation_id = identity["generation_id"]
    with store._write_lock, store._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        run = connection.execute("SELECT * FROM agent_runs WHERE run_id = ?", (run_id,)).fetchone()
        if (run is None or run["project_id"] != identity["project_id"]
            or run["parent_generation_id"] != identity["parent_generation_id"]
            or run["mode"] != "user_feedback" or run["candidate_generation_id"]
            or run["status"] not in {"running", "validating", "ready"}):
            raise ValueError("反馈运行与待登记候选不一致。")
        project = connection.execute("SELECT * FROM projects WHERE project_id = ?", (identity["project_id"],)).fetchone()
        if project is None or project["source_sha256"] != identity["source_sha256"]:
            raise ValueError("反馈候选所属项目或源哈希不一致。")
        _validate_feedback_context(connection, run, project)
        validate_publication(project, generation_id, **delivery)
        store._insert_generation(connection, **identity)
        _write_trusted_row(connection, generation_id, **delivery)
        connection.execute("UPDATE agent_runs SET candidate_generation_id = ?, status = 'ready', state_revision = state_revision + 1, "
                           "updated_at = ? WHERE run_id = ?", (generation_id, datetime.now(timezone.utc).isoformat(), run_id))
        _complete_confirmed_session(connection, run, generation_id, delivery)
    return store.get_generation(generation_id)
