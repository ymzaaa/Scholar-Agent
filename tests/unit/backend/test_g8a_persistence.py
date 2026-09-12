# -*- coding: utf-8 -*-
"""G8-A SQLite 项目与任务状态单元测试。"""

from pathlib import Path

from infrastructure.runtime_paths import default_database_path, default_project_workspace_root
import pytest
from pipeline.confirmed_structure import validate_object_bindings, ConfirmedStructureError
from persistence.store import ProjectStore
from infrastructure.tasks import TaskManager, TaskStatus


##### 项目恢复板块 #####


def test_default_database_and_workspace_are_disjoint() -> None:
    database = default_database_path().resolve()
    workspace = default_project_workspace_root().resolve()
    assert database != workspace
    assert workspace not in database.parents
    assert database not in workspace.parents


def test_project_survives_store_recreation(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    first = ProjectStore(database)
    first.create(
        project_id="p1", docx_path="paper.docx", bib_path=None,
        citation_map_path=None, template_id="ouc-graduate", reference_source="auto",
        workspace_dir="workspace/p1", source_sha256="abc",
    )
    first.update_extra("p1", extract_task_id="t1")

    restored = ProjectStore(database).get("p1")
    assert restored is not None
    assert restored.extra["extract_task_id"] == "t1"
    assert restored.source_sha256 == "abc"


def test_generation_survives_store_recreation(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    first = ProjectStore(database)
    first.create(project_id="p1", docx_path="paper.docx", bib_path=None, citation_map_path=None,
                 template_id="ouc-bachelor", reference_source="word_list",
                 workspace_dir=str(tmp_path / "project"), source_sha256="abc")
    first.create_generation(
        generation_id="g1", project_id="p1",
        structure_revision=2, source_sha256="abc",
    )
    first.update_generation(
        "g1", task_id="t1", status="running", quality_status="pending",
        detail="正在执行完整终验。",
    )
    restored = ProjectStore(database).get_generation("g1")
    assert restored is not None
    assert restored.status == "running" and not restored.trusted
    assert restored.structure_revision == 2
    assert restored.task_id == "t1" and not restored.artifact_manifest


##### 任务恢复板块 #####


def test_stale_running_task_becomes_failed_after_restart(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    manager = TaskManager(database, max_workers=1)
    task_id = manager.submit(lambda: {"ok": True}, project_id="p1", task_type="extract")
    for _ in range(100):
        task = manager.get(task_id)
        if task and task.status == TaskStatus.DONE:
            break
    assert task is not None
    assert task.result == {"ok": True}


##### 结构关系校验板块 #####


def test_unbound_body_caption_cannot_be_shadowed_by_same_number() -> None:
    bindings = [
        {"kind": "figure", "number": "图1-1", "status": "unbound", "caption_unit_id": "a", "object_unit_ids": []},
        {"kind": "figure", "number": "图1-1", "status": "bound", "caption_unit_id": "b", "object_unit_ids": ["image"]},
    ]
    index = {"a": {"unit_type": "paragraph"}, "b": {"unit_type": "paragraph"}, "image": {"unit_type": "image"}}
    with pytest.raises(ConfirmedStructureError, match="图表关系尚未确认"):
        validate_object_bindings(bindings, index)


def test_duplicate_bound_objects_are_not_hidden() -> None:
    bindings = [
        {"kind": "table", "status": "bound", "caption_unit_id": caption, "object_unit_ids": ["table"]}
        for caption in ("a", "b")
    ]
    index = {"a": {"unit_type": "paragraph"}, "b": {"unit_type": "paragraph"}, "table": {"unit_type": "table", "semantic_role": "data"}}
    with pytest.raises(ConfirmedStructureError, match="重复绑定"):
        validate_object_bindings(bindings, index)
