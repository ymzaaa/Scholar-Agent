# -*- coding: utf-8 -*-
"""P10-A：论文级约束持久化、冲突与版本原子事务。"""

from __future__ import annotations

from pathlib import Path

import pytest

from constraints.models import PaperConstraint
from persistence.store import ProjectStore
from tests.support.delivery import delivery_values


def _setup(tmp_path: Path) -> tuple[ProjectStore, str, str]:
    store = ProjectStore(tmp_path / "state.db")
    store.create(
        project_id="p1", docx_path="paper.docx", bib_path=None,
        citation_map_path=None, template_id="template-a", reference_source="auto",
        workspace_dir=str(tmp_path / "workspace"), source_sha256="hash",
        structure_revision=1, structure_confirmed=True,
    )
    root = store.create_generation(
        generation_id="root", project_id="p1", structure_revision=1,
        source_sha256="hash",
    )
    store.publish_trusted_generation(
        root.generation_id, **delivery_values(store, root.generation_id),
    )
    store.accept_generation(root.generation_id)
    child = store.create_generation(
        generation_id="child", project_id="p1", structure_revision=1,
        source_sha256="hash", parent_generation_id=root.generation_id,
        active_constraints=root.active_constraints,
    )
    store.publish_trusted_generation(
        child.generation_id, **delivery_values(store, child.generation_id),
    )
    return store, root.generation_id, child.generation_id


def _constraint(
    candidate: str, constraint_id: str = "c1", *,
    conflict_ids: tuple[str, ...] = (), supersedes_id: str | None = None,
) -> PaperConstraint:
    return PaperConstraint(
        constraint_id=constraint_id, project_id="p1", template_id="template-a",
        raw_feedback="正文保持小四号，只调整段后间距。",
        constraint_type="font_size", parameters={"value": "12pt"},
        description="正文保持小四号", scope_type="document", scope_ref=None,
        source_feedback_id=f"feedback-{constraint_id}", source_session_id="session-1",
        candidate_generation_id=candidate, conflict_ids=conflict_ids,
        supersedes_id=supersedes_id,
        verifier={"mode": "deterministic", "checker": "latex_font_size"},
    )


def test_unconfirmed_constraint_blocks_version_acceptance_atomically(tmp_path: Path) -> None:
    store, root_id, child_id = _setup(tmp_path)
    store.create_constraint(_constraint(child_id))

    with pytest.raises(ValueError, match="未确认"):
        store.accept_generation(child_id)

    assert store.get("p1").accepted_generation_id == root_id
    assert store.get_constraint("c1").status == "proposed"


def test_confirmed_constraint_activates_with_accepted_version(tmp_path: Path) -> None:
    store, _, child_id = _setup(tmp_path)
    store.create_constraint(_constraint(child_id))
    store.confirm_constraint("c1")

    accepted, _ = store.accept_generation(child_id)

    assert store.get_constraint("c1").status == "active"
    assert accepted.active_constraints[0]["constraint_id"] == "c1"
    restored = ProjectStore(store.db_path).get_generation(child_id)
    assert restored.active_constraints[0]["raw_feedback"].startswith("正文保持")


def test_conflict_requires_explicit_supersedes_relation(tmp_path: Path) -> None:
    store, _, child_id = _setup(tmp_path)
    store.create_constraint(_constraint(child_id, "c1"))
    store.confirm_constraint("c1")
    store.accept_generation(child_id)
    grandchild = store.create_generation(
        generation_id="grandchild", project_id="p1", structure_revision=1,
        source_sha256="hash", parent_generation_id=child_id,
        active_constraints=store.get_generation(child_id).active_constraints,
    )
    store.publish_trusted_generation(
        grandchild.generation_id, **delivery_values(store, grandchild.generation_id),
    )
    store.create_constraint(_constraint("grandchild", "c2", conflict_ids=("c1",)))
    with pytest.raises(ValueError, match="明确选择"):
        store.confirm_constraint("c2")

    assert store.get("p1").accepted_generation_id == child_id


def test_explicit_replacement_and_rollback_restore_parent_constraint(tmp_path: Path) -> None:
    store, _, child_id = _setup(tmp_path)
    store.create_constraint(_constraint(child_id, "c1"))
    store.confirm_constraint("c1")
    store.accept_generation(child_id)
    grandchild = store.create_generation(
        generation_id="grandchild", project_id="p1", structure_revision=1,
        source_sha256="hash", parent_generation_id=child_id,
        active_constraints=store.get_generation(child_id).active_constraints,
    )
    store.publish_trusted_generation(
        "grandchild", **delivery_values(store, "grandchild"),
    )
    store.create_constraint(_constraint(
        "grandchild", "c2", conflict_ids=("c1",), supersedes_id="c1",
    ))
    store.confirm_constraint("c2")
    store.accept_generation("grandchild")
    assert store.get_constraint("c1").status == "superseded"
    assert store.get_constraint("c2").status == "active"

    store.rollback_accepted_generation("grandchild")

    assert store.get_constraint("c1").status == "active"
    assert store.get_constraint("c2").status == "revoked"
    assert store.get("p1").accepted_generation_id == child_id
