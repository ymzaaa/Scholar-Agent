# -*- coding: utf-8 -*-
"""G9-A 父子版本持久化、可信约束与失败回退单元测试。"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from generation.service import execute_generation
from persistence.store import ProjectStore
from tests.support.delivery import delivery_values, frozen_values


##### 测试数据板块 #####


def _project(store: ProjectStore, root: Path) -> None:
    (root / "reports").mkdir(parents=True)
    store.create(
        project_id="p1", docx_path="paper.docx", bib_path=None,
        citation_map_path=None, template_id="ouc-graduate", reference_source="auto",
        workspace_dir=str(root), source_sha256="hash",
        structure_revision=1, structure_confirmed=True,
    )


def _trusted_root(store: ProjectStore, output: Path):
    root = store.create_generation(generation_id="root", project_id="p1",
        structure_revision=1, source_sha256="hash", **frozen_values(store.get("p1"), "root"))
    return store.publish_trusted_generation(root.generation_id, **delivery_values(store, root.generation_id))


##### 父子持久化板块 #####


def test_legacy_generation_migrates_without_implicitly_becoming_trusted(tmp_path: Path) -> None:
    database = tmp_path / "legacy.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """CREATE TABLE generations (
                generation_id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
                structure_revision INTEGER NOT NULL, source_sha256 TEXT NOT NULL,
                task_id TEXT, status TEXT NOT NULL, quality_status TEXT NOT NULL,
                output_dir TEXT, report_dir TEXT,
                artifact_manifest_json TEXT NOT NULL DEFAULT '{}',
                detail TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        connection.execute(
            "INSERT INTO generations VALUES "
            "('legacy', 'p1', 1, 'hash', NULL, 'degraded', 'degraded', "
            "'published/legacy', 'reports/legacy', '{}', '', '2026', '2026')"
        )

    restored = ProjectStore(database).get_generation("legacy")

    assert restored is not None
    assert restored.version_number == 1
    assert restored.trusted is False
    assert restored.parent_generation_id is None
    assert restored.change_origin == "initial_generation"


def test_child_version_is_persistent_and_monotonic(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path / "state.db")
    _project(store, tmp_path / "project")
    parent = _trusted_root(store, tmp_path / "root-output")

    child = store.create_generation(
        generation_id="child", project_id="p1",
        structure_revision=1, source_sha256="hash",
        parent_generation_id=parent.generation_id,
        change_origin="manual_validation", created_by="user",
    )
    restored = ProjectStore(tmp_path / "state.db").get_generation("child")

    assert restored is not None
    assert restored.parent_generation_id == "root"
    assert restored.version_number == 2
    assert restored.change_origin == "manual_validation"
    assert restored.trusted is False
    assert [item.generation_id for item in store.list_generations("p1")] == [
        "root", "child",
    ]


def test_untrusted_parent_cannot_create_child(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path / "state.db")
    _project(store, tmp_path / "project")
    store.create_generation(
        generation_id="parent", project_id="p1",
        structure_revision=1, source_sha256="hash",
    )

    with pytest.raises(ValueError, match="不是可信版本"):
        store.create_generation(
            generation_id="child", project_id="p1",
            structure_revision=1, source_sha256="hash",
            parent_generation_id="parent",
        )


##### 回退语义板块 #####


def test_failed_candidate_reverts_without_mutating_parent(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path / "state.db")
    project_root = tmp_path / "project"
    _project(store, project_root)
    parent = _trusted_root(store, tmp_path / "root-output")
    child = store.create_generation(
        generation_id="child", project_id="p1",
        structure_revision=1, source_sha256="hash",
        parent_generation_id=parent.generation_id,
        **frozen_values(store.get("p1"), "child"),
    )
    snapshot = project_root / "reports" / "confirmed_structure.json"
    snapshot.write_text("{}", encoding="utf-8")

    with patch(
        "generation.service.run_project_generation",
        side_effect=RuntimeError("candidate failure"),
    ), pytest.raises(RuntimeError, match="candidate failure"):
        execute_generation(
            database_path=str(store.db_path), generation_id=child.generation_id,
        )

    restored_parent = store.get_generation(parent.generation_id)
    restored_child = store.get_generation(child.generation_id)
    assert restored_parent is not None and restored_parent.trusted is True
    assert restored_parent.output_dir == parent.output_dir
    assert restored_child is not None and restored_child.status == "reverted"
    assert restored_child.trusted is False
    assert restored_child.rollback_target_id == parent.generation_id


def test_successful_candidate_becomes_trusted(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path / "state.db")
    project_root = tmp_path / "project"
    _project(store, project_root)
    parent = _trusted_root(store, tmp_path / "root-output")
    child = store.create_generation(
        generation_id="child", project_id="p1",
        structure_revision=1, source_sha256="hash",
        parent_generation_id=parent.generation_id,
        **frozen_values(store.get("p1"), "child"),
    )
    snapshot = project_root / "reports" / "confirmed_structure.json"
    snapshot.write_text("{}", encoding="utf-8")
    values = delivery_values(store, child.generation_id)
    result = {
        "status": "success", "quality_status": "passed", "output_dir": values["output_dir"],
        "report_dir": str(project_root / "reports" / child.generation_id),
        "artifact_manifest": values["artifact_manifest"], "detail": "published",
        "gate_snapshot": {"published": True, "compile_ok": False, "gate_statuses": {
            "content-fidelity": "passed", "structure": "passed", "compile": "passed", "format": "passed"}},
    }

    with patch("generation.service.run_project_generation", return_value=result):
        execute_generation(
            database_path=str(store.db_path), generation_id=child.generation_id,
        )

    restored = store.get_generation(child.generation_id)
    assert restored is not None and restored.trusted is True
    assert restored.status == "success"
    assert restored.gate_snapshot["compile_ok"] is False  # 旧投影不参与可信判定。
    assert restored.rollback_target_id is None


def test_current_version_can_only_rollback_to_direct_trusted_parent(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path / "state.db")
    _project(store, tmp_path / "project")
    parent = _trusted_root(store, tmp_path / "root-output")
    store.accept_generation(parent.generation_id)
    child = store.create_generation(
        generation_id="child", project_id="p1",
        structure_revision=1, source_sha256="hash",
        parent_generation_id=parent.generation_id,
        **frozen_values(store.get("p1"), "child"),
    )
    store.publish_trusted_generation(
        child.generation_id, **delivery_values(store, child.generation_id),
    )
    store.accept_generation(child.generation_id)

    rolled_back, accepted = store.rollback_accepted_generation(child.generation_id)

    assert accepted.generation_id == parent.generation_id
    assert accepted.review_status == "accepted"
    assert rolled_back.review_status == "rejected"
    assert rolled_back.rollback_target_id == parent.generation_id
    assert store.get("p1").accepted_generation_id == parent.generation_id
    with pytest.raises(ValueError, match="当前用户接受版本"):
        store.rollback_accepted_generation(child.generation_id)
