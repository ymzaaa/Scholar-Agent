# -*- coding: utf-8 -*-
"""P10 约束表读写与版本接受时的原子激活辅助。"""

from __future__ import annotations

import json
import sqlite3
from typing import Iterable

from constraints.models import PaperConstraint


##### 约束持久化板块 #####


def create_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE IF NOT EXISTS paper_constraints (
        constraint_id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
        template_id TEXT NOT NULL, raw_feedback TEXT NOT NULL,
        constraint_type TEXT NOT NULL, parameters_json TEXT NOT NULL DEFAULT '{}',
        description TEXT NOT NULL, scope_type TEXT NOT NULL, scope_ref TEXT,
        source_feedback_id TEXT NOT NULL, source_session_id TEXT NOT NULL,
        candidate_generation_id TEXT NOT NULL, status TEXT NOT NULL,
        priority INTEGER NOT NULL DEFAULT 50,
        conflict_ids_json TEXT NOT NULL DEFAULT '[]', supersedes_id TEXT,
        verifier_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
        confirmed_at TEXT, updated_at TEXT NOT NULL)"""
    )
    connection.execute(
        """CREATE INDEX IF NOT EXISTS idx_constraints_project_status
        ON paper_constraints(project_id, status, updated_at)"""
    )
    connection.execute(
        """CREATE INDEX IF NOT EXISTS idx_constraints_candidate
        ON paper_constraints(candidate_generation_id, status)"""
    )


def migrate_table(connection: sqlite3.Connection) -> None:
    """移除旧模板版本字段；约束只绑定当前项目选择的模板 ID。"""
    columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(paper_constraints)")
    }
    for name in ("template_version", "template_source_fingerprint"):
        if name in columns:
            connection.execute(f"ALTER TABLE paper_constraints DROP COLUMN {name}")


def from_row(row: sqlite3.Row) -> PaperConstraint:
    return PaperConstraint(
        constraint_id=row["constraint_id"], project_id=row["project_id"],
        template_id=row["template_id"],
        raw_feedback=row["raw_feedback"], constraint_type=row["constraint_type"],
        parameters=json.loads(row["parameters_json"] or "{}"),
        description=row["description"], scope_type=row["scope_type"],
        scope_ref=row["scope_ref"], source_feedback_id=row["source_feedback_id"],
        source_session_id=row["source_session_id"],
        candidate_generation_id=row["candidate_generation_id"], status=row["status"],
        priority=row["priority"],
        conflict_ids=tuple(json.loads(row["conflict_ids_json"] or "[]")),
        supersedes_id=row["supersedes_id"],
        verifier=json.loads(row["verifier_json"] or "{}"),
        created_at=row["created_at"], confirmed_at=row["confirmed_at"],
        updated_at=row["updated_at"],
    )


def insert(connection: sqlite3.Connection, item: PaperConstraint) -> None:
    item.validate()
    connection.execute(
        """INSERT INTO paper_constraints (
        constraint_id, project_id, template_id, raw_feedback, constraint_type,
        parameters_json, description, scope_type, scope_ref, source_feedback_id,
        source_session_id, candidate_generation_id, status, priority,
        conflict_ids_json, supersedes_id, verifier_json, created_at,
        confirmed_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            item.constraint_id, item.project_id, item.template_id,
            item.raw_feedback, item.constraint_type,
            json.dumps(item.parameters, ensure_ascii=False), item.description,
            item.scope_type, item.scope_ref, item.source_feedback_id,
            item.source_session_id, item.candidate_generation_id, item.status,
            item.priority, json.dumps(item.conflict_ids, ensure_ascii=False),
            item.supersedes_id, json.dumps(item.verifier, ensure_ascii=False),
            item.created_at, item.confirmed_at, item.updated_at,
        ),
    )


##### 完成目标与临时保护板块 #####


def insert_completed_protections(connection, *, run, state, session, generation_id, now):
    """已确认目标与实际动作产生临时保护，随候选登记原子写入，接受前不激活。"""
    from pathlib import PureWindowsPath
    agent = state['agent']
    observations = agent.get('observations', [])
    feedbacks = {item['feedback_id']: item for item in json.loads(session['state_json']).get('feedbacks', [])}
    goals = {goal['goal_id']: goal for goal in agent['goals']}
    superseded_by_unit = {}
    for goal in goals.values():
        for previous_id in goal.get('supersedes_constraint_ids', []):
            previous = connection.execute('SELECT * FROM paper_constraints WHERE constraint_id = ?',
                                           (previous_id,)).fetchone()
            if (previous is None or previous['project_id'] != run['project_id']
                or previous['template_id'] != agent['template_id']
                or previous['scope_ref'] not in goal['target_unit_ids']
                or previous['status'] not in {'active', 'confirmed'}
                or (previous['status'] == 'confirmed' and previous['source_session_id'] != session['session_id'])):
                raise ValueError('目标声明替代的保护已经失效。')
            identifiers = superseded_by_unit.setdefault(previous['scope_ref'], [])
            if previous_id not in identifiers:
                identifiers.append(previous_id)
    seen = set()
    for protection in agent.get('protections', []):
        identifier = protection.get('unit_id')
        selected = protection.get('goal_ids', [])
        matches = [item for item in observations if item.get('action_id') == protection.get('action_id')]
        if (not identifier or identifier in seen or not selected or set(selected) - goals.keys()
            or any(identifier not in goals[key]['target_unit_ids'] for key in selected)
            or len(matches) != 1 or matches[0].get('status') != 'ok' or matches[0].get('rolled_back')
            or matches[0].get('unit_id') != identifier
            or matches[0].get('tool') not in {'apply_protected_patch', 'replace_confirmed_text'}):
            raise ValueError('完成目标的临时保护缺少唯一实际动作。')
        files = matches[0].get('changed_files', [])
        if len(files) != 1:
            raise ValueError('保护动作必须对应单一文件区域。')
        relative = PureWindowsPath(files[0])
        if relative.is_absolute() or relative.drive or '..' in relative.parts or relative.suffix != '.tex':
            raise ValueError('保护动作文件不是安全相对路径。')
        related = [goal for goal in goals.values() if identifier in goal['target_unit_ids']]
        feedback_ids = list(dict.fromkeys(key for goal in related
            for key in goal.get('feedback_ids', state['feedback_ids'])))
        if not feedback_ids or set(feedback_ids) - feedbacks.keys():
            raise ValueError('临时保护缺少本批原始反馈。')
        raw = '\n'.join(feedbacks[key].get('text', '') for key in feedback_ids)
        description = '\n'.join(goal.get('description', '') for goal in related).strip() or raw
        superseded = superseded_by_unit.get(identifier, [])
        insert(connection, PaperConstraint(
            constraint_id=f'{generation_id}:{identifier}', project_id=run['project_id'],
            template_id=agent['template_id'], raw_feedback=raw, constraint_type='confirmed_change',
            parameters={'goal_ids': [goal['goal_id'] for goal in related], 'feedback_ids': feedback_ids,
                        'supersedes_constraint_ids': superseded}, conflict_ids=tuple(superseded),
            description=description, scope_type='content_unit', scope_ref=identifier,
            source_feedback_id=feedback_ids[0], source_session_id=session['session_id'],
            candidate_generation_id=generation_id, status='confirmed', created_at=now,
            confirmed_at=now, updated_at=now, verifier={'mode': 'deterministic',
                'checker': 'confirmed_action', 'relative_path': files[0], 'action_id': protection['action_id']}))
        seen.add(identifier)


def list_for_project(
    connection: sqlite3.Connection, project_id: str, statuses: Iterable[str] | None = None,
) -> list[PaperConstraint]:
    values = tuple(statuses or ())
    sql = "SELECT * FROM paper_constraints WHERE project_id = ?"
    params: tuple = (project_id,)
    if values:
        sql += f" AND status IN ({','.join('?' for _ in values)})"
        params += values
    sql += " ORDER BY priority DESC, created_at, constraint_id"
    return [from_row(row) for row in connection.execute(sql, params).fetchall()]


def inherited_for_session(store, run, session) -> list[dict]:
    """继承接受快照和本会话可信父链上的临时保护，不读取其他候选的要求。"""
    with store._connect() as connection:
        connection.execute('BEGIN')
        project = connection.execute('SELECT * FROM projects WHERE project_id = ?', (run.project_id,)).fetchone()
        parent = connection.execute('SELECT * FROM generations WHERE generation_id = ?',
                                    (run.parent_generation_id,)).fetchone()
        if (project is None or parent is None or session.project_id != run.project_id
            or run.state.get('session_id') != session.session_id
            or project['accepted_generation_id'] != session.base_generation_id
            or run.parent_generation_id != (session.head_generation_id or session.base_generation_id)):
            raise ValueError('继承保护的修订会话或父版本已经变化。')
        identifiers = set()
        ancestry = []
        current = parent
        while True:
            if (current is None or current['generation_id'] in identifiers
                or current['project_id'] != run.project_id or not current['trusted']
                or current['source_sha256'] != project['source_sha256']
                or current['status'] not in {'success', 'degraded'}):
                raise ValueError('继承保护的可信父版本链不完整。')
            if current['generation_id'] == session.base_generation_id:
                if current['review_status'] != 'accepted':
                    raise ValueError('继承保护的接受基线已失效。')
                break
            owners = connection.execute('SELECT * FROM agent_runs WHERE candidate_generation_id = ?',
                                         (current['generation_id'],)).fetchall()
            if (current['review_status'] != 'pending' or len(owners) != 1
                or owners[0]['mode'] != 'user_feedback' or owners[0]['project_id'] != run.project_id
                or owners[0]['parent_generation_id'] != current['parent_generation_id']):
                raise ValueError('临时保护候选缺少唯一的可信反馈归属。')
            owner_state = json.loads(owners[0]['state_json'])
            if not isinstance(owner_state, dict) or owner_state.get('session_id') != session.session_id:
                raise ValueError('临时保护不能跨修订会话继承。')
            identifiers.add(current['generation_id'])
            ancestry.append(current['generation_id'])
            current = connection.execute('SELECT * FROM generations WHERE generation_id = ?',
                                         (current['parent_generation_id'],)).fetchone()
        merged = {item['constraint_id']: item for item in json.loads(parent['active_constraints_json'])}
        pending = [item for item in list_for_project(connection, run.project_id, ['confirmed'])
                   if item.candidate_generation_id in identifiers and item.source_session_id == session.session_id]
        for generation_id in reversed(ancestry):
            for item in pending:
                if item.candidate_generation_id == generation_id:
                    for identifier in _superseded_ids(item):
                        merged.pop(identifier, None)
                    merged[item.constraint_id] = item.to_snapshot()
        return list(merged.values())


def _superseded_ids(item: PaperConstraint) -> set[str]:
    identifiers = item.parameters.get('supersedes_constraint_ids', [])
    if not isinstance(identifiers, list) or any(not isinstance(value, str) or not value for value in identifiers):
        raise ValueError('约束替代关系必须引用已有保护。')
    return set(identifiers) | ({item.supersedes_id} if item.supersedes_id else set())


##### 接受与约束激活板块 #####


def activate_for_generation(
    connection: sqlite3.Connection, *, project: sqlite3.Row,
    generation: sqlite3.Row, now: str,
) -> list[dict]:
    pending = connection.execute(
        """SELECT * FROM paper_constraints
        WHERE candidate_generation_id = ? AND status = 'proposed'""",
        (generation["generation_id"],),
    ).fetchall()
    if pending:
        raise ValueError("候选版本仍有未确认的论文约束。")
    confirmed = [from_row(row) for row in connection.execute(
        """SELECT * FROM paper_constraints
        WHERE candidate_generation_id = ? AND status = 'confirmed'
        ORDER BY priority DESC, created_at, constraint_id""",
        (generation["generation_id"],),
    ).fetchall()]
    active = list_for_project(connection, project["project_id"], ["active"])
    active_by_id = {item.constraint_id: item for item in active}
    for item in confirmed:
        if (
            item.template_id != project["template_id"]
        ):
            raise ValueError("论文约束与项目冻结模板身份不一致。")
        conflicts = set(item.conflict_ids) & set(active_by_id)
        superseded = _superseded_ids(item)
        if conflicts - superseded:
            raise ValueError(f"论文约束与当前活动约束冲突：{sorted(conflicts)}")
        if superseded - active_by_id.keys():
            raise ValueError("论文约束声明替代的活动约束不存在。")
        for identifier in sorted(superseded):
            connection.execute(
                "UPDATE paper_constraints SET status='superseded', updated_at=? WHERE constraint_id=?",
                (now, identifier),
            )
            active_by_id.pop(identifier)
        connection.execute(
            """UPDATE paper_constraints SET status='active', confirmed_at=COALESCE(confirmed_at, ?),
            updated_at=? WHERE constraint_id=? AND status='confirmed'""",
            (now, now, item.constraint_id),
        )
        active_by_id[item.constraint_id] = item
    # 重新读取，避免 frozen+slots 模型的复制分支产生状态偏差。
    return [item.to_snapshot() for item in list_for_project(
        connection, project["project_id"], ["active"],
    )]


##### 回退与撤销板块 #####


def rollback_to_generation(
    connection: sqlite3.Connection, *, current_generation_id: str,
    parent: sqlite3.Row, now: str,
) -> None:
    """按父版本冻结快照恢复活动约束，不让回退版本的约束继续生效。"""
    parent_ids = {
        str(item.get("constraint_id"))
        for item in json.loads(parent["active_constraints_json"] or "[]")
        if item.get("constraint_id")
    }
    connection.execute(
        """UPDATE paper_constraints SET status='revoked', updated_at=?
        WHERE candidate_generation_id=? AND status='active'""",
        (now, current_generation_id),
    )
    if parent_ids:
        placeholders = ",".join("?" for _ in parent_ids)
        connection.execute(
            f"""UPDATE paper_constraints SET status='active', updated_at=?
            WHERE constraint_id IN ({placeholders})""",
            (now, *sorted(parent_ids)),
        )


def revoke_candidate(
    connection: sqlite3.Connection, candidate_generation_id: str, now: str,
) -> None:
    connection.execute(
        """UPDATE paper_constraints SET status='revoked', updated_at=?
        WHERE candidate_generation_id=? AND status IN ('proposed','confirmed')""",
        (now, candidate_generation_id),
    )
