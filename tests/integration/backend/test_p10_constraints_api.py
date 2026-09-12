# -*- coding: utf-8 -*-
"""P10-B 约束查询、冲突确认与版本接受 API 测试。"""

from pathlib import Path

from fastapi.testclient import TestClient

from constraints.models import PaperConstraint
from main import app
from persistence.store import ProjectStore, get_store
from tests.support.delivery import delivery_values


def _constraint(candidate: str, constraint_id: str, *, status: str, value: str) -> PaperConstraint:
    return PaperConstraint(
        constraint_id=constraint_id, project_id="p1", template_id="template-a",
        raw_feedback=f"保持 {value}", constraint_type="font_size",
        parameters={"value": value}, description=f"字号保持 {value}",
        scope_type="content_unit", scope_ref="u1", source_feedback_id=f"f-{constraint_id}",
        source_session_id="s1", candidate_generation_id=candidate, status=status,
        conflict_ids=("old",) if constraint_id == "new" else (),
    )


def test_conflicting_constraint_requires_explicit_replacement(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path / "state.db")
    store.create(
        project_id="p1", docx_path="paper.docx", bib_path=None,
        citation_map_path=None, template_id="template-a", reference_source="word",
        workspace_dir=str(tmp_path / "project"), source_sha256="hash",
        structure_revision=1, structure_confirmed=True,
    )
    root = store.create_generation(
        generation_id="root", project_id="p1", structure_revision=1, source_sha256="hash",
    )
    store.publish_trusted_generation("root", **delivery_values(store, "root"))
    store.accept_generation("root")
    store.create_constraint(_constraint("root", "old", status="active", value="12pt"))
    candidate = store.create_generation(
        generation_id="candidate", project_id="p1", structure_revision=1,
        source_sha256="hash", parent_generation_id="root",
        active_constraints=[_constraint("root", "old", status="active", value="12pt").to_snapshot()],
    )
    store.publish_trusted_generation(
        candidate.generation_id, **delivery_values(store, candidate.generation_id),
    )
    store.create_constraint(_constraint("candidate", "new", status="proposed", value="14pt"))
    app.dependency_overrides[get_store] = lambda: store
    try:
        client = TestClient(app)
        listing = client.get("/api/projects/p1/constraints")
        assert listing.status_code == 200
        assert {item["constraint_id"] for item in listing.json()["constraints"]} == {"old", "new"}

        blocked = client.post("/api/constraints/new/confirm", json={})
        assert blocked.status_code == 409
        confirmed = client.post(
            "/api/constraints/new/confirm", json={"supersedes_id": "old"},
        )
        assert confirmed.status_code == 200
        assert confirmed.json()["status"] == "confirmed"
        store.accept_generation("candidate")
        assert store.get_constraint("old").status == "superseded"
        assert store.get_constraint("new").status == "active"
    finally:
        app.dependency_overrides.clear()
