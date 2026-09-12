# -*- coding: utf-8 -*-
"""反馈登记失败只清理本次私有产物，不碰父版本或其他目录。"""

import json
from types import SimpleNamespace

import pytest

from revision.feedback import _cleanup_unregistered_feedback
from pipeline.workspace import WorkspaceManager


##### 本次私有工作副本板块 #####

@pytest.fixture()
def case(tmp_path):
    manager = WorkspaceManager(tmp_path / "workspace")
    project = manager.create_project()
    workspace = manager.begin_generation(project.project_id)
    (workspace.staging_dir / "main.tex").write_text("owned candidate")
    final = workspace.publish()
    store = SimpleNamespace(get_generation=lambda identifier: None)
    return workspace, store, final


##### 清理与所有权板块 #####

def test_unregistered_published_feedback_directory_is_cleaned(case):
    workspace, store, final = case
    _cleanup_unregistered_feedback(store, workspace)
    assert not final.exists() and workspace.project.project_dir.is_dir()


def test_registered_version_is_never_deleted(case):
    workspace, store, final = case
    store.get_generation = lambda identifier: object()
    _cleanup_unregistered_feedback(store, workspace)
    assert (final / "main.tex").is_file()


def test_changed_generation_marker_blocks_cleanup(case):
    workspace, store, final = case
    marker = final / ".scholar-generation.json"
    value = json.loads(marker.read_text(encoding="utf-8"))
    value["generation_id"] = "foreign-version"
    marker.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError):
        _cleanup_unregistered_feedback(store, workspace)
    assert (final / "main.tex").is_file()
