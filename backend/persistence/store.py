# -*- coding: utf-8 -*-
"""SQLite 项目元数据仓储。论文正文与结构快照仍保存在受控工作区。"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from infrastructure.runtime_paths import default_database_path
from constraints.models import PaperConstraint
from constraints import repository as constraint_repository
from generation.artifacts import validate_delivery_manifest


##### 数据模型板块 #####


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class Project:
    project_id: str
    docx_path: str
    bib_path: str | None
    citation_map_path: str | None
    template_id: str
    reference_source: str
    workspace_dir: str
    source_sha256: str
    structure_revision: int = 0
    structure_confirmed: bool = False
    accepted_generation_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def output_dir(self) -> str:
        return self.workspace_dir


@dataclass(slots=True)
class Generation:
    generation_id: str
    project_id: str
    structure_revision: int
    source_sha256: str
    task_id: str | None = None
    status: str = "queued"
    quality_status: str = "pending"
    output_dir: str | None = None
    report_dir: str | None = None
    artifact_manifest: dict[str, Any] = field(default_factory=dict)
    detail: str = ""
    parent_generation_id: str | None = None
    version_number: int = 1
    change_origin: str = "initial_generation"
    created_by: str = "pipeline"
    active_constraints: list[dict[str, Any]] = field(default_factory=list)
    trusted: bool = False
    review_status: str = "not_reviewable"
    rollback_target_id: str | None = None
    gate_snapshot: dict[str, Any] = field(default_factory=dict)
    confirmed_snapshot_path: str = ""
    confirmed_snapshot_sha256: str = ""
    created_at: str = ""
    updated_at: str = ""


@dataclass(slots=True)
class AgentRun:
    """实际启动的统一 Agent 运行；只保存恢复和审计所需事实。"""

    run_id: str
    project_id: str
    mode: str
    parent_generation_id: str
    candidate_generation_id: str | None
    status: str
    state: dict[str, Any] = field(default_factory=dict)
    state_revision: int = 0
    created_at: str = ""
    updated_at: str = ""


@dataclass(slots=True)
class RevisionSession:
    """一次用户修订分支；反馈可累积，接受前不移动父版本指针。"""

    session_id: str
    project_id: str
    base_generation_id: str
    head_generation_id: str | None
    status: str
    state: dict[str, Any] = field(default_factory=dict)
    state_revision: int = 0
    created_at: str = ""
    updated_at: str = ""


##### SQLite 仓储板块 #####


class ProjectStore:
    """使用短连接和写锁实现可跨重启恢复的本地项目仓储。"""

    def __init__(self, db_path: str | Path | None = None) -> None:
        configured = db_path or os.environ.get("SCHOLAR_DATABASE_PATH")
        self.db_path = Path(configured or default_database_path())
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    project_id TEXT PRIMARY KEY,
                    docx_path TEXT NOT NULL,
                    bib_path TEXT,
                    citation_map_path TEXT,
                    template_id TEXT NOT NULL,
                    reference_source TEXT NOT NULL,
                    workspace_dir TEXT NOT NULL,
                    source_sha256 TEXT NOT NULL,
                    structure_revision INTEGER NOT NULL DEFAULT 0,
                    structure_confirmed INTEGER NOT NULL DEFAULT 0,
                    accepted_generation_id TEXT,
                    extra_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            constraint_repository.create_table(connection)
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS generations (
                    generation_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    structure_revision INTEGER NOT NULL,
                    source_sha256 TEXT NOT NULL,
                    task_id TEXT,
                    status TEXT NOT NULL,
                    quality_status TEXT NOT NULL,
                    output_dir TEXT,
                    report_dir TEXT,
                    artifact_manifest_json TEXT NOT NULL DEFAULT '{}',
                    detail TEXT NOT NULL DEFAULT '',
                    parent_generation_id TEXT,
                    version_number INTEGER NOT NULL DEFAULT 1,
                    change_origin TEXT NOT NULL DEFAULT 'initial_generation',
                    created_by TEXT NOT NULL DEFAULT 'pipeline',
                    active_constraints_json TEXT NOT NULL DEFAULT '[]',
                    trusted INTEGER NOT NULL DEFAULT 0,
                    review_status TEXT NOT NULL DEFAULT 'not_reviewable',
                    rollback_target_id TEXT,
                    gate_snapshot_json TEXT NOT NULL DEFAULT '{}',
                    confirmed_snapshot_path TEXT NOT NULL DEFAULT '',
                    confirmed_snapshot_sha256 TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_runs (
                    run_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    parent_generation_id TEXT NOT NULL,
                    candidate_generation_id TEXT,
                    status TEXT NOT NULL,
                    state_json TEXT NOT NULL DEFAULT '{}',
                    state_revision INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS revision_sessions (
                    session_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    base_generation_id TEXT NOT NULL,
                    head_generation_id TEXT,
                    status TEXT NOT NULL,
                    state_json TEXT NOT NULL DEFAULT '{}',
                    state_revision INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """CREATE INDEX IF NOT EXISTS idx_revision_sessions_project
                ON revision_sessions(project_id, updated_at DESC)"""
            )
            self._migrate_project_columns(connection)
            constraint_repository.migrate_table(connection)
            self._migrate_generation_columns(connection)

    def _migrate_project_columns(self, connection: sqlite3.Connection) -> None:
        """为旧项目增加模板冻结字段；旧值在下一次生成前受控补全。"""
        existing = {
            row["name"] for row in connection.execute("PRAGMA table_info(projects)")
        }
        definitions = {
            "accepted_generation_id": "TEXT",
        }
        for name, definition in definitions.items():
            if name not in existing:
                connection.execute(
                    f"ALTER TABLE projects ADD COLUMN {name} {definition}"
                )
        for name in ("template_version", "template_source_fingerprint"):
            if name in existing:
                connection.execute(f"ALTER TABLE projects DROP COLUMN {name}")

    def _migrate_generation_columns(self, connection: sqlite3.Connection) -> None:
        """为 G8 数据库原位增加版本字段，不破坏既有 generation。"""
        existing = {
            row["name"] for row in connection.execute("PRAGMA table_info(generations)")
        }
        definitions = {
            "parent_generation_id": "TEXT",
            "version_number": "INTEGER NOT NULL DEFAULT 1",
            "change_origin": "TEXT NOT NULL DEFAULT 'initial_generation'",
            "created_by": "TEXT NOT NULL DEFAULT 'pipeline'",
            "active_constraints_json": "TEXT NOT NULL DEFAULT '[]'",
            "trusted": "INTEGER NOT NULL DEFAULT 0",
            "review_status": "TEXT NOT NULL DEFAULT 'not_reviewable'",
            "rollback_target_id": "TEXT",
            "gate_snapshot_json": "TEXT NOT NULL DEFAULT '{}'",
            "confirmed_snapshot_path": "TEXT NOT NULL DEFAULT ''",
            "confirmed_snapshot_sha256": "TEXT NOT NULL DEFAULT ''",
        }
        version_column_added = "version_number" not in existing
        for name, definition in definitions.items():
            if name not in existing:
                connection.execute(
                    f"ALTER TABLE generations ADD COLUMN {name} {definition}"
                )
        if version_column_added:
            rows = connection.execute(
                "SELECT generation_id, project_id FROM generations "
                "ORDER BY project_id, created_at, generation_id"
            ).fetchall()
            counters: dict[str, int] = {}
            for row in rows:
                counters[row["project_id"]] = counters.get(row["project_id"], 0) + 1
                connection.execute(
                    "UPDATE generations SET version_number = ? WHERE generation_id = ?",
                    (counters[row["project_id"]], row["generation_id"]),
                )

    def create(self, **values: Any) -> Project:
        project = Project(**values)
        now = _utc_now()
        with self._write_lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO projects (
                    project_id, docx_path, bib_path, citation_map_path,
                    template_id, reference_source, workspace_dir, source_sha256,
                    structure_revision, structure_confirmed,
                    accepted_generation_id, extra_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project.project_id, project.docx_path, project.bib_path,
                    project.citation_map_path, project.template_id,
                    project.reference_source, project.workspace_dir,
                    project.source_sha256, project.structure_revision,
                    int(project.structure_confirmed),
                    project.accepted_generation_id,
                    json.dumps(project.extra, ensure_ascii=False), now, now,
                ),
            )
        return project

    def get(self, project_id: str) -> Project | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM projects WHERE project_id = ?", (project_id,)
            ).fetchone()
        if row is None:
            return None
        return Project(
            project_id=row["project_id"], docx_path=row["docx_path"],
            bib_path=row["bib_path"], citation_map_path=row["citation_map_path"],
            template_id=row["template_id"], reference_source=row["reference_source"],
            workspace_dir=row["workspace_dir"], source_sha256=row["source_sha256"],
            structure_revision=row["structure_revision"],
            structure_confirmed=bool(row["structure_confirmed"]),
            accepted_generation_id=row["accepted_generation_id"],
            extra=json.loads(row["extra_json"] or "{}"),
        )

    def update_extra(self, project_id: str, **values: Any) -> Project:
        project = self.get(project_id)
        if project is None:
            raise KeyError(project_id)
        project.extra.update(values)
        self._update_state(project)
        return project

    def update_structure_state(
        self, project_id: str, *, revision: int, confirmed: bool
    ) -> Project:
        project = self.get(project_id)
        if project is None:
            raise KeyError(project_id)
        project.structure_revision = revision
        project.structure_confirmed = confirmed
        self._update_state(project)
        return project

    def _update_state(self, project: Project) -> None:
        with self._write_lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE projects SET extra_json = ?, structure_revision = ?,
                    structure_confirmed = ?, accepted_generation_id = ?,
                    template_id = ?, updated_at = ?
                    WHERE project_id = ?
                """,
                (
                    json.dumps(project.extra, ensure_ascii=False),
                    project.structure_revision, int(project.structure_confirmed),
                    project.accepted_generation_id, project.template_id,
                    _utc_now(), project.project_id,
                ),
            )

    def exists(self, project_id: str) -> bool:
        return self.get(project_id) is not None


    ##### generation 持久化板块 #####

    def create_generation(
        self, *, generation_id: str, project_id: str,
        structure_revision: int, source_sha256: str,
        parent_generation_id: str | None = None,
        change_origin: str = "initial_generation",
        created_by: str = "pipeline",
        active_constraints: list[dict[str, Any]] | None = None,
        confirmed_snapshot_path: str = "",
        confirmed_snapshot_sha256: str = "",
    ) -> Generation:
        """首次生成可以预登记；反馈只能经完整交付事务登记。"""
        if change_origin == "user_feedback":
            raise ValueError("反馈候选必须在完整终验后登记。")
        with self._write_lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._insert_generation(connection,
                generation_id=generation_id,
                project_id=project_id,
                structure_revision=structure_revision,
                source_sha256=source_sha256,
                parent_generation_id=parent_generation_id,
                change_origin=change_origin,
                created_by=created_by,
                active_constraints=active_constraints,
                confirmed_snapshot_path=confirmed_snapshot_path,
                confirmed_snapshot_sha256=confirmed_snapshot_sha256,
            )

    def _insert_generation(
        self, connection, *, generation_id: str, project_id: str,
        structure_revision: int, source_sha256: str,
        parent_generation_id: str | None = None,
        change_origin: str = "initial_generation",
        created_by: str = "pipeline",
        active_constraints: list[dict[str, Any]] | None = None,
        confirmed_snapshot_path: str = "",
        confirmed_snapshot_sha256: str = "",
    ) -> Generation:
        now = _utc_now()
        if not connection.execute("SELECT 1 FROM projects WHERE project_id = ?", (project_id,)).fetchone():
            raise ValueError("版本所属项目不存在。")
        parent = None
        if parent_generation_id:
            parent = connection.execute(
                "SELECT * FROM generations WHERE generation_id = ?",
                (parent_generation_id,),
            ).fetchone()
            if parent is None:
                raise ValueError("父版本不存在。")
            if parent["project_id"] != project_id:
                raise ValueError("父版本不属于当前项目。")
            if not bool(parent["trusted"]) or parent["status"] not in {"success", "degraded"}:
                raise ValueError("父版本不是可信版本。")
            if parent["source_sha256"] != source_sha256:
                raise ValueError("父版本与候选版本的源文件哈希不一致。")
            if parent["structure_revision"] != structure_revision:
                raise ValueError("父版本与候选版本的结构修订不一致。")
        next_number = int(connection.execute(
            "SELECT COALESCE(MAX(version_number), 0) + 1 FROM generations "
            "WHERE project_id = ?", (project_id,),
        ).fetchone()[0])
        generation = Generation(
            generation_id=generation_id, project_id=project_id,
            structure_revision=structure_revision, source_sha256=source_sha256,
            parent_generation_id=parent_generation_id,
            version_number=next_number, change_origin=change_origin,
            created_by=created_by,
            active_constraints=list(active_constraints or []),
            confirmed_snapshot_path=confirmed_snapshot_path,
            confirmed_snapshot_sha256=confirmed_snapshot_sha256,
            created_at=now, updated_at=now,
        )
        connection.execute(
            """INSERT INTO generations (
                generation_id, project_id, structure_revision, source_sha256,
                task_id, status, quality_status, output_dir, report_dir,
                artifact_manifest_json, detail, parent_generation_id,
                version_number, change_origin, created_by,
                active_constraints_json, trusted, review_status,
                rollback_target_id,
                gate_snapshot_json, confirmed_snapshot_path,
                confirmed_snapshot_sha256, created_at, updated_at
            ) VALUES (?, ?, ?, ?, NULL, ?, ?, NULL, NULL, '{}', '', ?, ?, ?, ?,
                ?, 0, ?, NULL, '{}', ?, ?, ?, ?)""",
            (
                generation_id, project_id, structure_revision, source_sha256,
                generation.status, generation.quality_status,
                parent_generation_id, next_number, change_origin, created_by,
                json.dumps(generation.active_constraints, ensure_ascii=False),
                generation.review_status,
                confirmed_snapshot_path, confirmed_snapshot_sha256, now, now,
            ),
        )
        return generation

    def get_generation(self, generation_id: str) -> Generation | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM generations WHERE generation_id = ?", (generation_id,)
            ).fetchone()
        if row is None:
            return None
        return Generation(
            generation_id=row["generation_id"], project_id=row["project_id"],
            structure_revision=row["structure_revision"], source_sha256=row["source_sha256"],
            task_id=row["task_id"], status=row["status"],
            quality_status=row["quality_status"], output_dir=row["output_dir"],
            report_dir=row["report_dir"],
            artifact_manifest=json.loads(row["artifact_manifest_json"] or "{}"),
            detail=row["detail"],
            parent_generation_id=row["parent_generation_id"],
            version_number=row["version_number"],
            change_origin=row["change_origin"], created_by=row["created_by"],
            active_constraints=json.loads(row["active_constraints_json"] or "[]"),
            trusted=bool(row["trusted"]),
            review_status=row["review_status"],
            rollback_target_id=row["rollback_target_id"],
            gate_snapshot=json.loads(row["gate_snapshot_json"] or "{}"),
            confirmed_snapshot_path=row["confirmed_snapshot_path"],
            confirmed_snapshot_sha256=row["confirmed_snapshot_sha256"],
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    def list_generations(self, project_id: str) -> list[Generation]:
        with self._connect() as connection:
            identifiers = [
                row["generation_id"] for row in connection.execute(
                    "SELECT generation_id FROM generations WHERE project_id = ? "
                    "ORDER BY version_number, created_at", (project_id,),
                ).fetchall()
            ]
        return [item for identifier in identifiers if (item := self.get_generation(identifier))]

    def _validate_trusted_row(self, connection, row) -> None:
        """写入和评审共用最小可信状态契约，不在数据库层重复哈希文件。"""
        if not row["trusted"]:
            return
        if row["status"] not in {"success", "degraded"}:
            raise ValueError("可信版本只能是成功或降级可交付状态。")
        project = connection.execute("SELECT workspace_dir FROM projects WHERE project_id = ?",
                                     (row["project_id"],)).fetchone()
        if project is None:
            raise ValueError("版本所属项目不存在。")
        validate_delivery_manifest(workspace_dir=project["workspace_dir"],
            generation_id=row["generation_id"], output_dir=row["output_dir"],
            report_dir=row["report_dir"], manifest=json.loads(row["artifact_manifest_json"]), check_files=False)

    def update_generation(self, generation_id: str, **values: Any) -> Generation:
        """只写入显式字段，事务内验证合并后的最小状态不变量。"""
        allowed = {
            "task_id", "status", "quality_status", "output_dir", "report_dir",
            "artifact_manifest", "detail", "trusted", "rollback_target_id",
            "gate_snapshot", "review_status", "active_constraints",
        }
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"不允许更新 generation 字段：{sorted(unknown)}")
        if values.get("trusted"):
            raise ValueError("可信提升只能通过 publish_trusted_generation 专用事务执行。")
        if values.get("review_status") in {"accepted", "superseded"}:
            raise ValueError("接受与替代状态只能通过专用评审事务写入。")
        json_columns = {"artifact_manifest": "artifact_manifest_json",
                        "gate_snapshot": "gate_snapshot_json", "active_constraints": "active_constraints_json"}
        updates = {json_columns.get(key, key): json.dumps(value, ensure_ascii=False)
                   if key in json_columns else int(value) if key == "trusted" else value
                   for key, value in values.items()}
        with self._write_lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM generations WHERE generation_id = ?", (generation_id,)).fetchone()
            if row is None:
                raise KeyError(generation_id)
            merged = {**dict(row), **updates}
            if row["trusted"] and values.get("trusted") is not False and set(values) & {
                "status", "quality_status", "output_dir", "report_dir", "artifact_manifest", "gate_snapshot",
            }:
                raise ValueError("可信交付事实不能通过普通更新重新拼装。")
            if row["review_status"] in {"accepted", "superseded"} and values.get("trusted") is False:
                raise ValueError("不能撤销已经接受版本的可信状态。")
            if not row["trusted"]:
                if merged["status"] in {"success", "degraded"} or merged["output_dir"]:
                    raise ValueError("任务记录不能通过普通更新变为交付版本。")
                items = json.loads(merged["artifact_manifest_json"]).get("artifacts", [])
                if any(item.get("kind") != "report" for item in items):
                    raise ValueError("未可信任务只能登记诊断报告。")
            self._validate_trusted_row(connection, merged)
            if not merged["trusted"] and merged["review_status"] != "not_reviewable":
                raise ValueError("未可信任务不能进入版本评审生命周期。")
            updates["updated_at"] = _utc_now()
            assignments = ", ".join(f"{key} = ?" for key in updates)
            connection.execute(f"UPDATE generations SET {assignments} WHERE generation_id = ?",
                               (*updates.values(), generation_id))
        return self.get_generation(generation_id)

    def publish_trusted_generation(self, generation_id: str, **delivery: Any) -> Generation:
        """原子写入终验与全部交付事实，保留首次任务 ID 和查询契约。"""
        from generation.publication import publish_trusted_generation
        return publish_trusted_generation(self, generation_id, **delivery)

    def register_trusted_feedback(self, *, identity: dict, delivery: dict, run_id: str) -> Generation:
        """反馈终验后原子创建可信候选，不留下预登记反馈版本。"""
        from generation.publication import register_trusted_feedback
        return register_trusted_feedback(self, identity=identity, delivery=delivery, run_id=run_id)

    def fail_generation(self, generation_id: str, *, report_dir=None, artifact_manifest=None, detail="生成任务内部异常。") -> None:
        """失败边界只收敛运行记录，不删除文件或修改已接受版本。"""
        with self._write_lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT trusted, review_status FROM generations WHERE generation_id = ?", (generation_id,)).fetchone()
            if row is None:
                raise KeyError(generation_id)
            if row["trusted"] or row["review_status"] in {"accepted", "superseded"}:
                raise ValueError("不能通过运行失败路径撤销已完成的可信版本。")
            connection.execute("""UPDATE generations SET
                status = CASE WHEN parent_generation_id IS NULL THEN 'failed' ELSE 'reverted' END,
                quality_status = 'internal_error', trusted = 0, review_status = 'not_reviewable',
                output_dir = NULL, report_dir = ?, artifact_manifest_json = ?, detail = ?,
                rollback_target_id = parent_generation_id, updated_at = ? WHERE generation_id = ?""",
                (report_dir, json.dumps(artifact_manifest or {"version": "1.0.0", "artifacts": []}, ensure_ascii=False),
                 detail, _utc_now(), generation_id))

    ##### 用户接受版本板块 #####

    def accept_generation(self, generation_id: str, *, session_id: str | None = None,
                          expected_session_revision: int | None = None) -> tuple[Generation, str | None]:
        """版本接受与所属修订会话共用原子事务，不改动不可变历史产物。"""
        from generation.transactions import accept_generation
        return accept_generation(self, generation_id, session_id=session_id,
                                 expected_session_revision=expected_session_revision)

    ##### 论文级反馈约束板块 #####

    def create_constraint(self, constraint: PaperConstraint) -> PaperConstraint:
        now = _utc_now()
        item = PaperConstraint(
            constraint_id=constraint.constraint_id, project_id=constraint.project_id,
            template_id=constraint.template_id,
            raw_feedback=constraint.raw_feedback, constraint_type=constraint.constraint_type,
            parameters=dict(constraint.parameters), description=constraint.description,
            scope_type=constraint.scope_type, scope_ref=constraint.scope_ref,
            source_feedback_id=constraint.source_feedback_id,
            source_session_id=constraint.source_session_id,
            candidate_generation_id=constraint.candidate_generation_id,
            status=constraint.status, priority=constraint.priority,
            conflict_ids=tuple(constraint.conflict_ids), supersedes_id=constraint.supersedes_id,
            verifier=dict(constraint.verifier), created_at=constraint.created_at or now,
            confirmed_at=constraint.confirmed_at, updated_at=now,
        )
        with self._write_lock, self._connect() as connection:
            project = connection.execute(
                "SELECT * FROM projects WHERE project_id=?", (item.project_id,),
            ).fetchone()
            generation = connection.execute(
                "SELECT project_id FROM generations WHERE generation_id=?",
                (item.candidate_generation_id,),
            ).fetchone()
            if project is None or generation is None or generation["project_id"] != item.project_id:
                raise ValueError("论文约束所属项目或候选版本不存在。")
            if (
                item.template_id != project["template_id"]
            ):
                raise ValueError("论文约束与项目冻结模板身份不一致。")
            constraint_repository.insert(connection, item)
        return item

    def get_constraint(self, constraint_id: str) -> PaperConstraint | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM paper_constraints WHERE constraint_id=?", (constraint_id,),
            ).fetchone()
        return constraint_repository.from_row(row) if row is not None else None

    def list_constraints(
        self, project_id: str, statuses: list[str] | None = None,
    ) -> list[PaperConstraint]:
        with self._connect() as connection:
            return constraint_repository.list_for_project(connection, project_id, statuses)

    def confirm_constraint(
        self, constraint_id: str, *, supersedes_id: str | None = None,
    ) -> PaperConstraint:
        now = _utc_now()
        with self._write_lock, self._connect() as connection:
            current = connection.execute(
                "SELECT * FROM paper_constraints WHERE constraint_id=?",
                (constraint_id,),
            ).fetchone()
            if current is None or current["status"] != "proposed":
                raise ValueError("只有待确认的论文约束可以被确认。")
            conflicts = set(json.loads(current["conflict_ids_json"] or "[]"))
            selected_supersedes = supersedes_id or current["supersedes_id"]
            if conflicts and selected_supersedes not in conflicts:
                raise ValueError("冲突约束必须明确选择要替代的活动约束。")
            if selected_supersedes:
                target = connection.execute(
                    "SELECT project_id, status FROM paper_constraints WHERE constraint_id=?",
                    (selected_supersedes,),
                ).fetchone()
                if (
                    target is None or target["project_id"] != current["project_id"]
                    or target["status"] != "active"
                ):
                    raise ValueError("要替代的约束不是当前项目的活动约束。")
            cursor = connection.execute(
                """UPDATE paper_constraints SET status='confirmed', supersedes_id=?,
                confirmed_at=?, updated_at=?
                WHERE constraint_id=? AND status='proposed'""",
                (selected_supersedes, now, now, constraint_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("只有待确认的论文约束可以被确认。")
        result = self.get_constraint(constraint_id)
        if result is None:
            raise RuntimeError("论文约束确认后无法读取。")
        return result

    def reject_generation(self, generation_id: str) -> tuple[Generation, str | None]:
        with self._write_lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT project_id, review_status FROM generations "
                "WHERE generation_id = ?", (generation_id,),
            ).fetchone()
            if row is None:
                raise KeyError(generation_id)
            accepted = connection.execute(
                "SELECT accepted_generation_id FROM projects WHERE project_id = ?",
                (row["project_id"],),
            ).fetchone()
            if accepted and accepted["accepted_generation_id"] == generation_id:
                raise ValueError("当前父版本不能直接拒绝，请先接受它的可信子版本。")
            if row["review_status"] not in {"pending", "rejected"}:
                raise ValueError("只有待评审版本可以拒绝。")
            if row["review_status"] == "rejected":
                return self.get_generation(generation_id), accepted["accepted_generation_id"] if accepted else None
            connection.execute(
                "UPDATE generations SET review_status = 'rejected', updated_at = ? "
                "WHERE generation_id = ?",
                (_utc_now(), generation_id),
            )
        rejected = self.get_generation(generation_id)
        if rejected is None:
            raise RuntimeError("拒绝版本事务完成后无法读取目标版本。")
        return rejected, accepted["accepted_generation_id"] if accepted else None

    def rollback_accepted_generation(self, generation_id: str) -> tuple[Generation, Generation]:
        """把当前接受指针退回直接可信父版本；不允许跨层或删除历史产物。"""
        now = _utc_now()
        with self._write_lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT * FROM generations WHERE generation_id = ?",
                (generation_id,),
            ).fetchone()
            if current is None:
                raise KeyError(generation_id)
            project = connection.execute(
                "SELECT accepted_generation_id FROM projects WHERE project_id = ?",
                (current["project_id"],),
            ).fetchone()
            if not project or project["accepted_generation_id"] != generation_id:
                raise ValueError("只能回退当前用户接受版本。")
            parent_id = current["parent_generation_id"]
            if not parent_id:
                raise ValueError("当前接受版本没有可回退的直接父版本。")
            parent = connection.execute(
                "SELECT * FROM generations WHERE generation_id = ?",
                (parent_id,),
            ).fetchone()
            if (
                parent is None or parent["project_id"] != current["project_id"] or not bool(parent["trusted"])
                or parent["status"] not in {"success", "degraded"}
            ):
                raise ValueError("直接父版本不是已发布的可信版本。")
            self._validate_trusted_row(connection, parent)
            constraint_repository.rollback_to_generation(
                connection, current_generation_id=generation_id,
                parent=parent, now=now,
            )
            connection.execute(
                "UPDATE generations SET review_status = 'rejected', "
                "rollback_target_id = ?, updated_at = ? WHERE generation_id = ?",
                (parent_id, now, generation_id),
            )
            connection.execute(
                "UPDATE generations SET review_status = 'accepted', updated_at = ? "
                "WHERE generation_id = ?",
                (now, parent_id),
            )
            connection.execute(
                "UPDATE projects SET accepted_generation_id = ?, updated_at = ? "
                "WHERE project_id = ?",
                (parent_id, now, current["project_id"]),
            )
        rolled_back = self.get_generation(generation_id)
        accepted_parent = self.get_generation(parent_id)
        if rolled_back is None or accepted_parent is None:
            raise RuntimeError("版本回退事务完成后无法读取版本。")
        return rolled_back, accepted_parent

    def revoke_candidate_constraints(self, generation_id: str) -> None:
        with self._write_lock, self._connect() as connection:
            constraint_repository.revoke_candidate(connection, generation_id, _utc_now())

    ##### Agent 工作记忆板块 #####

    def create_agent_run(
        self, *, run_id: str, project_id: str, parent_generation_id: str,
        status: str, state: dict[str, Any], candidate_generation_id: str | None = None,
        mode: str = "user_feedback",
    ) -> AgentRun:
        now = _utc_now()
        with self._write_lock, self._connect() as connection:
            connection.execute(
                """INSERT INTO agent_runs (
                    run_id, project_id, mode, parent_generation_id,
                    candidate_generation_id, status, state_json, state_revision,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)""",
                (
                    run_id, project_id, mode, parent_generation_id,
                    candidate_generation_id, status,
                    json.dumps(state, ensure_ascii=False), now, now,
                ),
            )
        return AgentRun(
            run_id=run_id, project_id=project_id, mode=mode,
            parent_generation_id=parent_generation_id,
            candidate_generation_id=candidate_generation_id,
            status=status, state=state, created_at=now, updated_at=now,
        )

    def get_agent_run(self, run_id: str) -> AgentRun | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM agent_runs WHERE run_id = ?", (run_id,),
            ).fetchone()
        if row is None:
            return None
        return AgentRun(
            run_id=row["run_id"], project_id=row["project_id"], mode=row["mode"],
            parent_generation_id=row["parent_generation_id"],
            candidate_generation_id=row["candidate_generation_id"],
            status=row["status"], state=json.loads(row["state_json"] or "{}"),
            state_revision=row["state_revision"], created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def update_agent_run(
        self, run_id: str, *, expected_revision: int, status: str,
        state: dict[str, Any], candidate_generation_id: str | None,
    ) -> AgentRun:
        now = _utc_now()
        with self._write_lock, self._connect() as connection:
            cursor = connection.execute(
                """UPDATE agent_runs SET status = ?, state_json = ?,
                    candidate_generation_id = ?, state_revision = state_revision + 1,
                    updated_at = ? WHERE run_id = ? AND state_revision = ?""",
                (
                    status, json.dumps(state, ensure_ascii=False),
                    candidate_generation_id, now, run_id, expected_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("Agent 工作记忆修订冲突。")
        updated = self.get_agent_run(run_id)
        if updated is None:
            raise RuntimeError("Agent 工作记忆更新后无法读取。")
        return updated

    ##### 修订会话板块 #####

    @staticmethod
    def _revision_session_from_row(row: sqlite3.Row) -> RevisionSession:
        return RevisionSession(
            session_id=row["session_id"], project_id=row["project_id"],
            base_generation_id=row["base_generation_id"],
            head_generation_id=row["head_generation_id"], status=row["status"],
            state=json.loads(row["state_json"] or "{}"),
            state_revision=row["state_revision"], created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def create_revision_session(
        self, *, session_id: str, project_id: str, base_generation_id: str,
        state: dict[str, Any], status: str = "collecting",
    ) -> RevisionSession:
        """同一项目只允许一个未结束会话，防止并发分支覆盖用户选择。"""
        now = _utc_now()
        with self._write_lock, self._connect() as connection:
            active = connection.execute(
                """SELECT session_id FROM revision_sessions
                WHERE project_id = ? AND status NOT IN ('accepted', 'discarded')
                ORDER BY updated_at DESC LIMIT 1""",
                (project_id,),
            ).fetchone()
            if active is not None:
                raise ValueError("项目已有进行中的修订会话。")
            connection.execute(
                """INSERT INTO revision_sessions (
                    session_id, project_id, base_generation_id,
                    head_generation_id, status, state_json, state_revision,
                    created_at, updated_at
                ) VALUES (?, ?, ?, NULL, ?, ?, 0, ?, ?)""",
                (
                    session_id, project_id, base_generation_id, status,
                    json.dumps(state, ensure_ascii=False), now, now,
                ),
            )
        session = self.get_revision_session(session_id)
        if session is None:
            raise RuntimeError("修订会话创建后无法读取。")
        return session

    def get_revision_session(self, session_id: str) -> RevisionSession | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM revision_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return self._revision_session_from_row(row) if row is not None else None

    def get_active_revision_session(self, project_id: str) -> RevisionSession | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT * FROM revision_sessions
                WHERE project_id = ? AND status NOT IN ('accepted', 'discarded')
                ORDER BY updated_at DESC LIMIT 1""",
                (project_id,),
            ).fetchone()
        return self._revision_session_from_row(row) if row is not None else None

    def update_revision_session(
        self, session_id: str, *, expected_revision: int, status: str,
        state: dict[str, Any], head_generation_id: str | None,
    ) -> RevisionSession:
        now = _utc_now()
        with self._write_lock, self._connect() as connection:
            cursor = connection.execute(
                """UPDATE revision_sessions SET status = ?, state_json = ?,
                head_generation_id = ?, state_revision = state_revision + 1,
                updated_at = ? WHERE session_id = ? AND state_revision = ?""",
                (
                    status, json.dumps(state, ensure_ascii=False),
                    head_generation_id, now, session_id, expected_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("修订会话状态已变化，请刷新后重试。")
        updated = self.get_revision_session(session_id)
        if updated is None:
            raise RuntimeError("修订会话更新后无法读取。")
        return updated

    def list_agent_runs(self, project_id: str, limit: int = 20) -> list[AgentRun]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT run_id FROM agent_runs WHERE project_id = ?
                ORDER BY updated_at DESC LIMIT ?""",
                (project_id, max(1, min(limit, 100))),
            ).fetchall()
        return [run for row in rows if (run := self.get_agent_run(row["run_id"]))]


##### 依赖注入板块 #####


_store: ProjectStore | None = None
_store_key: str | None = None


def get_store() -> ProjectStore:
    global _store, _store_key
    key = os.environ.get("SCHOLAR_DATABASE_PATH", "")
    if _store is None or _store_key != key:
        _store = ProjectStore(key or None)
        _store_key = key
    return _store
