# -*- coding: utf-8 -*-
"""有界线程池任务管理器，任务状态持久化到 SQLite。"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from infrastructure.runtime_paths import default_database_path


##### 状态模型板块 #####


class TaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass(slots=True)
class Task:
    task_id: str
    task_type: str
    project_id: str
    status: TaskStatus = TaskStatus.QUEUED
    result: dict[str, Any] | None = None
    error: str | None = None
    traceback: str | None = None


##### 任务持久化板块 #####


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _close_initial_agents(connection, *, task_id=None) -> None:
    """仅收尾中断任务关联的首版执行；不改变结构审查、反馈或已完成运行。"""
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE name = 'agent_runs'").fetchone():
        return
    rows = connection.execute("""SELECT a.run_id, a.state_json FROM agent_runs a
        JOIN generations g ON a.candidate_generation_id = g.generation_id AND a.project_id = g.project_id
        JOIN tasks t ON g.task_id = t.task_id
        WHERE a.mode = 'initial_generation' AND a.status IN ('running', 'validating', 'waiting_user')
        AND g.status IN ('queued', 'running') AND g.trusted = 0
        AND ((? IS NULL AND t.status IN ('queued', 'running')) OR t.task_id = ?)""",
        (task_id, task_id)).fetchall()
    for row in rows:
        try:
            state = json.loads(row['state_json'])
        except (TypeError, ValueError):
            state = {}
        if not isinstance(state, dict):
            state = {}
        agent = state.get('agent', {})
        state['agent'] = {**(agent if isinstance(agent, dict) else {}), 'status': 'failed',
            'stop_reason': 'initial_generation_interrupted', 'detail': '后台任务中断，未登记为可信版本。'}
        connection.execute("""UPDATE agent_runs SET status = 'failed', state_json = ?,
            state_revision = state_revision + 1, updated_at = ? WHERE run_id = ?""",
            (json.dumps(state, ensure_ascii=False), _utc_now(), row['run_id']))


def _link_revision_task(connection, *, task_id, project_id, run_id, now):
    """线程启动前关联当前反馈运行；同一批次不能同时占用两个后台任务。"""
    run = connection.execute('SELECT * FROM agent_runs WHERE run_id = ?', (run_id,)).fetchone()
    if (run is None or run['project_id'] != project_id or run['mode'] != 'user_feedback'
        or run['candidate_generation_id'] is not None or run['status'] not in {'running', 'waiting_user', 'validating'}):
        raise ValueError('任务关联的反馈运行无效。')
    state = json.loads(run['state_json'])
    session = connection.execute('SELECT * FROM revision_sessions WHERE session_id = ?',
                                 (state.get('session_id'),)).fetchone()
    if session is None or session['project_id'] != project_id or session['status'] != 'queued':
        raise ValueError('反馈会话不处于本次排队状态。')
    session_state = json.loads(session['state_json'])
    if session_state.get('current_run_id') != run_id:
        raise ValueError('任务关联的反馈批次已经变化。')
    previous = connection.execute('SELECT status FROM tasks WHERE task_id = ?',
                                  (session_state.get('current_task_id'),)).fetchone()
    if previous is not None and previous['status'] in {'queued', 'running'}:
        raise ValueError('反馈批次已有运行中的任务。')
    session_state['current_task_id'] = task_id
    connection.execute("""UPDATE revision_sessions SET state_json = ?, state_revision = state_revision + 1,
        updated_at = ? WHERE session_id = ?""", (json.dumps(session_state, ensure_ascii=False), now, session['session_id']))


def _close_revision_agents(connection, *, task_id=None):
    """只关闭中断任务所属的当前未交付反馈运行，已完成暂停和可信候选保持不变。"""
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE name = 'revision_sessions'").fetchone():
        return
    sessions = connection.execute("SELECT * FROM revision_sessions WHERE status IN ('queued', 'running')").fetchall()
    for session in sessions:
        state = json.loads(session['state_json'])
        linked_id = state.get('current_task_id')
        if task_id is not None and task_id != linked_id:
            continue
        task = connection.execute('SELECT * FROM tasks WHERE task_id = ?', (linked_id,)).fetchone()
        if (task is None or task['project_id'] != session['project_id']
            or (task_id is None and task['status'] not in {'queued', 'running'})):
            continue
        run = connection.execute('SELECT * FROM agent_runs WHERE run_id = ?', (state.get('current_run_id'),)).fetchone()
        if (run is None or run['project_id'] != session['project_id'] or run['mode'] != 'user_feedback'
            or run['candidate_generation_id'] is not None):
            continue
        run_state = json.loads(run['state_json'])
        if run_state.get('session_id') != session['session_id']:
            continue
        detail = '后台反馈任务中断，未登记可信候选。'
        run_state.update(stop_reason='feedback_task_interrupted', detail=detail)
        if isinstance(run_state.get('agent'), dict):
            run_state['agent'] = {**run_state['agent'], 'status': 'failed', 'pending_action': None,
                                  'stop_reason': 'feedback_task_interrupted', 'detail': detail}
        state.update(detail=detail, current_run_revision=run['state_revision'] + 1)
        now = _utc_now()
        connection.execute("""UPDATE agent_runs SET status = 'failed', state_json = ?,
            state_revision = state_revision + 1, updated_at = ? WHERE run_id = ?""",
            (json.dumps(run_state, ensure_ascii=False), now, run['run_id']))
        connection.execute("""UPDATE revision_sessions SET status = 'failed', state_json = ?,
            state_revision = state_revision + 1, updated_at = ? WHERE session_id = ?""",
            (json.dumps(state, ensure_ascii=False), now, session['session_id']))


class TaskManager:
    """限制并发数，避免大 Word 同时解析耗尽本机内存。"""

    def __init__(self, db_path: str | Path | None = None, max_workers: int = 2) -> None:
        configured = db_path or os.environ.get("SCHOLAR_DATABASE_PATH")
        self.db_path = Path(configured or default_database_path())
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="scholar")
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY, task_type TEXT NOT NULL,
                    project_id TEXT NOT NULL, status TEXT NOT NULL,
                    result_json TEXT, error TEXT, traceback TEXT,
                    progress_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                )
                """
            )
            if connection.execute("SELECT 1 FROM sqlite_master WHERE name = 'generations'").fetchone():
                _close_initial_agents(connection)
                connection.execute("""UPDATE generations SET status = CASE WHEN parent_generation_id IS NULL THEN 'failed' ELSE 'reverted' END,
                    quality_status = 'internal_error', trusted = 0, review_status = 'not_reviewable',
                    output_dir = NULL, artifact_manifest_json = '{}',
                    detail = '后台服务重启，本次生成未完成。', updated_at = ?
                    WHERE status IN ('queued', 'running') AND task_id IN
                    (SELECT task_id FROM tasks WHERE status IN ('queued', 'running'))""", (_utc_now(),))
            _close_revision_agents(connection)
            connection.execute(
                """UPDATE tasks SET status = 'failed',
                error = 'backend_restarted', updated_at = ?
                WHERE status IN ('queued', 'running')""",
                (_utc_now(),),
            )

    def submit(
        self, fn: Callable[..., Any], *args: Any,
        project_id: str = "", task_type: str = "generic",
        linked_generation_id: str | None = None, linked_revision_run_id: str | None = None, **kwargs: Any
    ) -> str:
        task_id = str(uuid.uuid4())
        now = _utc_now()
        with self._write_lock, self._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            connection.execute(
                "INSERT INTO tasks VALUES (?, ?, ?, ?, NULL, NULL, NULL, '{}', ?, ?)",
                (task_id, task_type, project_id, TaskStatus.QUEUED.value, now, now),
            )
            if linked_generation_id is not None:
                updated = connection.execute(
                    "UPDATE generations SET task_id = ?, updated_at = ? WHERE generation_id = ? AND project_id = ?",
                    (task_id, now, linked_generation_id, project_id))
                if updated.rowcount != 1:
                    raise ValueError("任务关联的 generation 不存在或不属于项目。")
            if linked_revision_run_id is not None:
                _link_revision_task(connection, task_id=task_id, project_id=project_id,
                                    run_id=linked_revision_run_id, now=now)
        try:
            self._executor.submit(self._run, task_id, fn, args, kwargs)
        except Exception as exc:
            self._fail(task_id, exc)
            raise
        return task_id

    def _run(
        self, task_id: str, fn: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> None:
        try:
            self._set(task_id, status=TaskStatus.RUNNING)
            result = fn(*args, **kwargs)
            if result is not None and not isinstance(result, dict):
                raise TypeError("后台任务结果必须为 JSON 对象。")
            self._set(task_id, status=TaskStatus.DONE, result=result)
        except Exception as exc:
            self._fail(task_id, exc)

    def _fail(self, task_id: str, exc: Exception) -> None:
        """终态写入失败也走独立的最小失败事务，内部堆栈不进入公共响应。"""
        with self._write_lock, self._connect() as connection:
            updated = connection.execute(
                """UPDATE tasks SET status = 'failed', result_json = NULL, error = ?,
                traceback = ?, updated_at = ? WHERE task_id = ?""",
                (str(exc), traceback.format_exc(), _utc_now(), task_id))
            if updated.rowcount != 1:
                raise KeyError(task_id)
            _close_revision_agents(connection, task_id=task_id)
            if connection.execute("SELECT 1 FROM sqlite_master WHERE name = 'generations'").fetchone():
                _close_initial_agents(connection, task_id=task_id)
                connection.execute("""UPDATE generations SET status = CASE WHEN parent_generation_id IS NULL THEN 'failed' ELSE 'reverted' END,
                    quality_status = 'internal_error', trusted = 0, review_status = 'not_reviewable',
                    output_dir = NULL, artifact_manifest_json = '{}',
                    detail = '后台任务异常终止，请查看生成报告。', updated_at = ?
                    WHERE task_id = ? AND status IN ('queued', 'running')""", (_utc_now(), task_id))

    def _set(
        self, task_id: str, *, status: TaskStatus, result: Any = None,
        error: str | None = None, traceback_text: str | None = None,
    ) -> None:
        result_json = json.dumps(result, ensure_ascii=False) if result is not None else None
        with self._write_lock, self._connect() as connection:
            updated = connection.execute(
                """UPDATE tasks SET status = ?, result_json = ?, error = ?,
                traceback = ?, updated_at = ? WHERE task_id = ?""",
                (status.value, result_json, error, traceback_text, _utc_now(), task_id),
            )

            if updated.rowcount != 1:
                raise KeyError(task_id)

    def get(self, task_id: str) -> Task | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        if row is None:
            return None
        return Task(
            task_id=row["task_id"], task_type=row["task_type"],
            project_id=row["project_id"], status=TaskStatus(row["status"]),
            result=json.loads(row["result_json"]) if row["result_json"] else None,
            error=row["error"], traceback=row["traceback"],
        )


##### 依赖注入板块 #####


_manager: TaskManager | None = None
_manager_key: str | None = None


def get_task_manager() -> TaskManager:
    global _manager, _manager_key
    key = os.environ.get("SCHOLAR_DATABASE_PATH", "")
    if _manager is None or _manager_key != key:
        _manager = TaskManager(key or None)
        _manager_key = key
    return _manager
