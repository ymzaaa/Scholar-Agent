# -*- coding: utf-8 -*-
"""每批一次反馈理解、目标草稿和用户确认；不创建候选或修改论文。"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import re
import uuid

from infrastructure.stage1_adapter import _ensure_stage1_on_path
from persistence.store import ProjectStore


##### 运行与父版本校验板块 #####

def prepare_revision_run(store: ProjectStore, session_id: str, *, external_processing_allowed=False,
                         allowed_llm_tasks=None):
    """一次事务建立批次运行与会话关联，授权仅来自本次请求。"""
    from generation.versions import validate_trusted_generation
    session = store.get_revision_session(session_id)
    if session is None:
        raise KeyError(session_id)
    if session.status not in {'collecting', 'reviewable', 'failed'}:
        raise ValueError('当前修订会话不能创建新的反馈批次。')
    pending = [item for item in session.state.get('feedbacks', []) if item.get('status') == 'pending']
    if not pending:
        raise ValueError('没有待处理反馈。')
    identifiers = [item['feedback_id'] for item in pending]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError('反馈批次包含重复编号。')
    parent_id = session.head_generation_id or session.base_generation_id
    parent, project = store.get_generation(parent_id), store.get(session.project_id)
    if parent is None or project is None:
        raise ValueError('修订会话的当前版本不存在。')
    validate_trusted_generation(parent, project)
    run_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    run_state = {'session_id': session_id, 'feedback_ids': identifiers,
        'authorization': {'external_processing_allowed': external_processing_allowed,
                          'allowed_llm_tasks': list(allowed_llm_tasks or [])},
        'target_references': [], 'local_evaluation': {}, 'active_constraints': list(parent.active_constraints),
        'detail': '反馈批次已进入后台队列。'}
    session_state = {**session.state, 'current_run_id': run_id, 'current_run_revision': 0, 'current_task_id': None,
                     'detail': f'正在处理 {len(pending)} 条反馈。'}
    with store._write_lock, store._connect() as connection:
        connection.execute('BEGIN IMMEDIATE')
        current = connection.execute('SELECT * FROM revision_sessions WHERE session_id = ?', (session_id,)).fetchone()
        current_project = connection.execute('SELECT * FROM projects WHERE project_id = ?', (session.project_id,)).fetchone()
        current_parent = connection.execute('SELECT * FROM generations WHERE generation_id = ?', (parent_id,)).fetchone()
        if (current is None or current_project is None or current_parent is None
            or current['state_revision'] != session.state_revision or current['status'] != session.status
            or current_project['accepted_generation_id'] != session.base_generation_id
            or current_project['source_sha256'] != parent.source_sha256
            or not current_parent['trusted'] or current_parent['status'] not in {'success', 'degraded'}
            or current_parent['review_status'] not in {'accepted', 'pending'}):
            raise ValueError('修订会话或可信父版本已经变化，不能创建旧批次。')
        store._validate_trusted_row(connection, current_parent)
        connection.execute("""INSERT INTO agent_runs (run_id, project_id, mode, parent_generation_id,
            candidate_generation_id, status, state_json, state_revision, created_at, updated_at)
            VALUES (?, ?, 'user_feedback', ?, NULL, 'running', ?, 0, ?, ?)""",
            (run_id, session.project_id, parent_id, json.dumps(run_state, ensure_ascii=False), now, now))
        connection.execute("""UPDATE revision_sessions SET status = 'queued', state_json = ?,
            state_revision = state_revision + 1, updated_at = ? WHERE session_id = ?""",
            (json.dumps(session_state, ensure_ascii=False), now, session_id))
    return store.get_revision_session(session_id), store.get_agent_run(run_id)


def fail_feedback_submission(store: ProjectStore, run_id: str) -> None:
    """任务尚未提交时关闭本批准备记录；不覆盖其他批次、已运行任务或已交付候选。"""
    with store._write_lock, store._connect() as connection:
        connection.execute('BEGIN IMMEDIATE')
        run = connection.execute('SELECT * FROM agent_runs WHERE run_id = ?', (run_id,)).fetchone()
        if run is None or run['candidate_generation_id'] is not None or run['status'] in {'ready', 'failed'}:
            return
        state = json.loads(run['state_json'])
        session = connection.execute('SELECT * FROM revision_sessions WHERE session_id = ?',
                                     (state.get('session_id'),)).fetchone()
        if session is None or session['project_id'] != run['project_id'] or session['status'] != 'queued':
            return
        session_state = json.loads(session['state_json'])
        if session_state.get('current_run_id') != run_id:
            return
        if session_state.get('current_task_id'):
            return  # 已关联任务由任务管理器收尾，不能把真实运行标为提交失败。
        detail = '反馈任务提交失败，未修改论文或登记可信候选。'
        state.update(stop_reason='feedback_submission_failed', detail=detail)
        session_state.update(detail=detail, current_run_revision=run['state_revision'] + 1)
        now = datetime.now(timezone.utc).isoformat()
        connection.execute("""UPDATE agent_runs SET status = 'failed', state_json = ?,
            state_revision = state_revision + 1, updated_at = ? WHERE run_id = ?""",
            (json.dumps(state, ensure_ascii=False), now, run_id))
        connection.execute("""UPDATE revision_sessions SET status = 'failed', state_json = ?,
            state_revision = state_revision + 1, updated_at = ? WHERE session_id = ?""",
            (json.dumps(session_state, ensure_ascii=False), now, session['session_id']))


def _context(store: ProjectStore, run_id: str, expected_revision: int):
    """目标确认只能继续原会话与可信父版本，不能继承后来接受的版本。"""
    run = store.get_agent_run(run_id)
    if run is None or run.mode != "user_feedback":
        raise ValueError("反馈运行不存在。")
    if run.state_revision != expected_revision:
        raise ValueError("反馈运行状态已经变化，请刷新后重试。")
    if run.candidate_generation_id:
        raise ValueError("本批已有交付候选，不能重新理解或确认目标。")
    session = store.get_revision_session(str(run.state.get("session_id", "")))
    project = store.get(run.project_id)
    parent = store.get_generation(run.parent_generation_id)
    if (session is None or project is None or parent is None
        or session.project_id != project.project_id or parent.project_id != project.project_id
        or session.status in {"accepted", "discarded"}
        or session.state.get('current_run_id') != run.run_id
        or project.accepted_generation_id != session.base_generation_id
        or (session.head_generation_id or session.base_generation_id) != run.parent_generation_id
        or not parent.trusted or parent.status not in {"success", "degraded"}
        or parent.review_status not in {'accepted', 'pending'}
        or parent.source_sha256 != project.source_sha256):
        raise ValueError("反馈父版本或会话已经变化，不能继续原批次。")
    return run, session


def _feedbacks(run, session):
    identifiers = run.state.get("feedback_ids", [])
    if not identifiers or len(identifiers) != len(set(identifiers)):
        raise ValueError("反馈批次编号无效。")
    by_id = {item["feedback_id"]: item for item in session.state.get("feedbacks", [])}
    if set(identifiers) - by_id.keys():
        raise ValueError("本批原始反馈已不存在。")
    selections = run.state.get('target_selections', {})
    return [{**deepcopy(by_id[identifier]), 'selected_unit_ids': deepcopy(
        selections.get(identifier, by_id[identifier].get('selected_unit_ids', [])))} for identifier in identifiers]


def _persist_goal_state(store, run, *, status, state):
    """写锁内重查父版本和当前批次，不能把读取后失效的授权写成执行目标。"""
    with store._write_lock, store._connect() as connection:
        connection.execute('BEGIN IMMEDIATE')
        current, _session = _context(store, run.run_id, run.state_revision)
        connection.execute("""UPDATE agent_runs SET status = ?, state_json = ?,
            state_revision = state_revision + 1, updated_at = ? WHERE run_id = ?""",
            (status, json.dumps(state, ensure_ascii=False), datetime.now(timezone.utc).isoformat(), current.run_id))
    return store.get_agent_run(run.run_id)


##### 可信反馈证据板块 #####

def revision_context(store: ProjectStore, project_id: str) -> dict:
    """页面读取当前可信版本的对象选择和公开模型信息，不创建 Agent 或发送论文。"""
    from revision.feedback import _verified_artifact
    from generation.versions import validate_trusted_generation
    _ensure_stage1_on_path()
    from content_extraction.persistence import load_extraction
    from config import get_settings
    project = store.get(project_id)
    if project is None:
        raise KeyError(project_id)
    session = store.get_active_revision_session(project_id)
    parent_id = (session.head_generation_id if session else None) or project.accepted_generation_id
    parent = store.get_generation(parent_id) if parent_id else None
    if parent is None:
        raise ValueError('当前项目没有可用于反馈的可信版本。')
    validate_trusted_generation(parent, project)
    trace_path, _ = _verified_artifact(parent, kind='report', name='render_trace.json')
    trace = json.loads(trace_path.read_text(encoding='utf-8'))
    extracted = load_extraction(project.workspace_dir, source_sha256=project.source_sha256)
    identifiers = {item['unit_id'] for item in extracted.content_units}
    selected = list(dict.fromkeys(item['marker_unit_id'] for item in trace['records']
                                 if item.get('marker_unit_id') in identifiers))
    evidence = _evidence_from_sources(extracted.content_units, trace, selected)
    settings = get_settings()
    model_name = settings.llm_model
    if not isinstance(model_name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_./-]{0,119}', model_name):
        model_name = '已配置模型' if settings.llm_configured else '尚未配置'
    return {'targets': [{'unit_id': item['unit_id'], 'text': item['text'],
        'label': ' '.join(item['text'].split())[:90] or '图表对象',
        'body_replace_allowed': item['role'] == 'body_paragraph' and not item['has_semantic_objects']}
        for item in evidence], 'model': {'provider': {'openai': 'OpenAI 兼容服务', 'anthropic': 'Anthropic'}.get(
            settings.llm_provider, '模型服务'), 'name': model_name, 'configured': bool(settings.llm_configured)}}


def load_feedback_evidence(store: ProjectStore, run_id: str, *, expected_revision: int) -> list[dict]:
    """只从冻结父版本和持久化抽取读取选择范围，不打开 Word 或发送整篇材料。"""
    from revision.feedback import _verified_artifact
    from generation.versions import validate_trusted_generation
    _ensure_stage1_on_path()
    from content_extraction.persistence import load_extraction

    run, session = _context(store, run_id, expected_revision)
    project = store.get(run.project_id)
    parent = store.get_generation(run.parent_generation_id)
    validate_trusted_generation(parent, project)
    trace_path, _ = _verified_artifact(parent, kind='report', name='render_trace.json')
    try:
        trace = json.loads(trace_path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError('父版本渲染追踪不可读取。') from exc
    extracted = load_extraction(project.workspace_dir, source_sha256=project.source_sha256)
    selected = list(dict.fromkeys(identifier for feedback in _feedbacks(run, session)
                                 for identifier in feedback.get('selected_unit_ids', [])))
    return _evidence_from_sources(extracted.content_units, trace, selected)


def _evidence_from_sources(content_units: list[dict], trace: dict, selected: list[str]) -> list[dict]:
    """已确认正文替换按历史顺序应用到证据副本，不能将旧 Word 原文当作当前正文。"""
    units = {item['unit_id']: item for item in content_units}
    if len(units) != len(content_units) or not isinstance(trace, dict):
        raise ValueError('反馈来源单元无效或重复。')
    records = trace.get('records')
    if not isinstance(records, list) or any(not isinstance(item, dict) for item in records):
        raise ValueError('父版本渲染追踪缺少有效记录。')
    by_marker = {item.get('marker_unit_id'): item for item in records}
    if len(by_marker) != len(records) or None in by_marker:
        raise ValueError('反馈来源标记缺失或重复。')
    children = {item.get('relations', {}).get('parent_unit_id') for item in content_units}

    def ordinary(identifier):
        unit = units[identifier]
        tokens = unit.get('payload', {}).get('inline_tokens', [])
        return (unit.get('unit_type') == 'paragraph' and identifier not in children
                and by_marker.get(identifier, {}).get('role') == 'body_paragraph'
                and all(token.get('kind') == 'text' for token in tokens))

    current = {key: value.get('text', '') for key, value in units.items()}
    replacements = trace.get('body_replacements', [])
    if not isinstance(replacements, list):
        raise ValueError('已确认正文替换历史无效。')
    seen = set()
    for goal in replacements:
        if not isinstance(goal, dict):
            raise ValueError('已确认正文替换历史无效。')
        targets, replacement = goal.get('target_unit_ids'), goal.get('replacement')
        if (not goal.get('goal_id') or goal['goal_id'] in seen or goal.get('confirmed') is not True
            or goal.get('kind') != 'body_replace' or not isinstance(targets, list) or len(targets) != 1
            or targets[0] not in units or not ordinary(targets[0]) or not isinstance(replacement, dict)
            or set(replacement) != {'old_text', 'new_text'}
            or replacement['old_text'] != current[targets[0]]
            or not isinstance(replacement['new_text'], str) or not replacement['new_text'].strip()):
            raise ValueError('父版本正文替换缺少连续、已确认的普通正文来源。')
        seen.add(goal['goal_id'])
        current[targets[0]] = replacement['new_text']

    result, emitted = [], set()
    for identifier in selected:
        if identifier not in units:
            raise ValueError('所选反馈单元不存在。')
        matches = [item for item in records if identifier == item.get('marker_unit_id')
                   or identifier in item.get('source_unit_ids', [])]
        if len(matches) != 1 or matches[0]['marker_unit_id'] not in units:
            raise ValueError('所选反馈单元没有唯一、可追踪的生成区域。')
        record = matches[0]
        marker = record['marker_unit_id']
        if marker not in emitted:
            emitted.add(marker)
            result.append({'unit_id': marker, 'role': record['role'], 'text': current[marker],
                'has_semantic_objects': not ordinary(marker),
                'source_unit_ids': list(record.get('source_unit_ids', [])),
                'relative_path': record['target_file']})
    return result


##### 单次理解板块 #####

def normalize_feedback_goals(
    store: ProjectStore, run_id: str, *, expected_revision: int, model, evidence: list[dict],
):
    """请求前持久化占用；结果未知、失败或恢复均不隐式重新调用模型。"""
    run, session = _context(store, run_id, expected_revision)
    normalization = run.state.get("normalization_status")
    if normalization == "completed":
        return run
    if normalization is not None:
        raise ValueError("本批已经请求过反馈理解，不能自动重新调用模型。")
    authorization = run.state.get("authorization", {})
    _ensure_stage1_on_path()
    from llm.policy import FEEDBACK_NORMALIZATION_TASK
    if (authorization.get("external_processing_allowed") is not True
        or FEEDBACK_NORMALIZATION_TASK not in authorization.get("allowed_llm_tasks", [])):
        raise ValueError("本批反馈理解尚未获得具体任务授权。")
    if model is None:
        raise ValueError("反馈理解客户端不可用。")
    feedbacks = _feedbacks(run, session)
    claimed = _persist_goal_state(store, run, status="running",
        state={**run.state, "normalization_status": "requested", "goals": [], "goal_drafts": [],
               "detail": "正在理解本批反馈。"})
    try:
        _ensure_stage1_on_path()
        from agents.quality_repair.feedback_normalizer import normalize_feedback
        drafts = normalize_feedback(model, feedbacks=feedbacks, evidence=evidence)
        current, current_session = _context(store, run_id, claimed.state_revision)
        if _feedbacks(current, current_session) != feedbacks:
            raise ValueError("本批原始反馈已经变化，不能使用旧理解结果。")
        from constraints.repository import inherited_for_session
        selected = {item['unit_id'] for item in evidence}
        protections = [{'constraint_id': item['constraint_id'], 'unit_id': item.get('scope_ref'),
                        'description': item['description']}
                       for item in inherited_for_session(store, current, current_session)
                       if item.get('scope_ref') in selected]
        for draft in drafts:
            draft['protections'] = deepcopy(protections)
        return _persist_goal_state(store, current, status="waiting_user",
            state={**current.state, "normalization_status": "completed", "goal_drafts": drafts,
                   "stop_reason": "goal_confirmation", "detail": "请核对本批目标，确认后才会修改候选。"})
    except Exception as exc:
        try:
            store.update_agent_run(run_id, expected_revision=claimed.state_revision, status="failed",
                state={**claimed.state, "normalization_status": "failed", "stop_reason": "normalization_failed",
                       "detail": "反馈理解未完成，未修改论文，也未自动重试。"}, candidate_generation_id=None)
        except Exception as persistence_error:
            exc.add_note(f"反馈理解失败状态写入异常：{type(persistence_error).__name__}")
        raise


##### 用户目标确认板块 #####

def confirm_feedback_goals(
    store: ProjectStore, run_id: str, *, expected_revision: int, decisions: list[dict], evidence: list[dict],
):
    """只确认现存目标，复用乐观锁；论文原文和反馈记录保持不变。"""
    run, session = _context(store, run_id, expected_revision)
    if (run.status != "waiting_user" or run.state.get("normalization_status") != "completed"
        or run.state.get("stop_reason") != "goal_confirmation"):
        raise ValueError("当前状态不能确认反馈目标。")
    _ensure_stage1_on_path()
    from agents.quality_repair.feedback_normalizer import confirm_goal_drafts
    goals = confirm_goal_drafts(run.state["goal_drafts"], decisions=decisions, evidence=evidence)
    from constraints.repository import inherited_for_session
    inherited = {item['constraint_id']: item for item in inherited_for_session(store, run, session)}
    for goal in goals:
        shown = {item['constraint_id'] for item in goal.get('protections', [])}
        for identifier in goal.get('supersedes_constraint_ids', []):
            if (identifier not in shown or identifier not in inherited
                or inherited[identifier].get('scope_ref') not in goal['target_unit_ids']):
                raise ValueError('只能替代已展示且属于当前目标的已有保护。')
    return _persist_goal_state(store, run,
        status="running" if goals else "stopped",
        state={**run.state, "goals": goals, "stop_reason": "" if goals else "user_discarded_goals",
               "detail": "目标已确认，等待执行。" if goals else "已放弃本批全部目标，论文未修改。"})
