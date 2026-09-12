# -*- coding: utf-8 -*-
"""G9-B1b Agent 工作记忆的持久化和并发修订测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from persistence.store import ProjectStore


def _store(tmp_path: Path) -> ProjectStore:
    store = ProjectStore(tmp_path / "state.db")
    store.create(
        project_id="p1", docx_path="paper.docx", bib_path=None,
        citation_map_path=None, template_id="ouc-graduate", reference_source="auto",
        workspace_dir=str(tmp_path / "project"), source_sha256="hash",
        structure_revision=1, structure_confirmed=True,
    )
    return store


def test_agent_run_survives_restart_and_advances_revision(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create_agent_run(
        run_id="r1", project_id="p1", parent_generation_id="v1",
        status="locating", state={"detail": "locating"},
    )
    restored = ProjectStore(tmp_path / "state.db").get_agent_run("r1")
    assert restored is not None and restored.state_revision == 0

    updated = store.update_agent_run(
        "r1", expected_revision=0, status="ready",
        state={"detail": "ready"}, candidate_generation_id="v2",
    )
    assert updated.state_revision == 1
    assert updated.candidate_generation_id == "v2"


def test_agent_run_rejects_stale_revision(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create_agent_run(
        run_id="r1", project_id="p1", parent_generation_id="v1",
        status="locating", state={},
    )
    store.update_agent_run(
        "r1", expected_revision=0, status="ready", state={},
        candidate_generation_id="v2",
    )
    with pytest.raises(ValueError, match="修订冲突"):
        store.update_agent_run(
            "r1", expected_revision=0, status="failed", state={},
            candidate_generation_id=None,
        )
