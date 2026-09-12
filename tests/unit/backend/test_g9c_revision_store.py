# -*- coding: utf-8 -*-
"""修订会话持久化与乐观锁单元测试。"""

from __future__ import annotations

import pytest

from revision.service import discard_revision
from persistence.store import ProjectStore
from tests.support.delivery import delivery_values, ready_feedback_state


##### 会话事务板块 #####


@pytest.mark.parametrize('status', ['running', 'waiting_user', 'validating', 'ready', 'stopped', 'failed'])
def test_feedback_run_persistence_uses_shared_agent_states(status, tmp_path):
    from agents.quality_repair.models import AgentState
    state = AgentState(run_id='run', project_id='project', parent_generation_id='parent',
        template_id='ouc-bachelor', mode='user_feedback', status=status)
    store = ProjectStore(tmp_path / 'state.db')
    store.create_agent_run(run_id='run', project_id='project', parent_generation_id='parent',
                            status=status, state=state.to_dict())
    saved = ProjectStore(store.db_path).get_agent_run('run')
    assert saved.status == status and AgentState.from_dict(saved.state).to_dict() == state.to_dict()


def test_revision_session_keeps_single_active_branch(tmp_path) -> None:
    store = ProjectStore(tmp_path / "state.db")
    first = store.create_revision_session(
        session_id="s1", project_id="p1", base_generation_id="v1",
        state={"feedbacks": []},
    )
    updated = store.update_revision_session(
        "s1", expected_revision=first.state_revision,
        status="collecting", state={"feedbacks": [{"feedback_id": "f1"}]},
        head_generation_id=None,
    )
    assert updated.state_revision == 1
    assert store.get_active_revision_session("p1").state["feedbacks"][0]["feedback_id"] == "f1"
    with pytest.raises(ValueError, match="已有进行中的修订会话"):
        store.create_revision_session(
            session_id="s2", project_id="p1", base_generation_id="v1",
            state={"feedbacks": []},
        )


def test_revision_session_terminal_state_allows_next_session(tmp_path) -> None:
    store = ProjectStore(tmp_path / "state.db")
    first = store.create_revision_session(
        session_id="s1", project_id="p1", base_generation_id="v1",
        state={"feedbacks": []},
    )
    store.update_revision_session(
        "s1", expected_revision=first.state_revision,
        status="discarded", state={"feedbacks": []}, head_generation_id=None,
    )
    second = store.create_revision_session(
        session_id="s2", project_id="p1", base_generation_id="v1",
        state={"feedbacks": []},
    )
    assert second.session_id == "s2"


def test_discard_marks_candidate_rejected_without_moving_accepted_version(tmp_path) -> None:
    store = ProjectStore(tmp_path / "state.db")
    store.create(
        project_id="p1", docx_path="paper.docx", bib_path=None,
        citation_map_path=None, template_id="ouc-bachelor", reference_source="word_list",
        workspace_dir=str(tmp_path / "project"), source_sha256="hash",
        structure_revision=1, structure_confirmed=True,
    )
    root = store.create_generation(
        generation_id="root", project_id="p1", structure_revision=1, source_sha256="hash",
    )
    store.publish_trusted_generation("root", **delivery_values(store, "root"))
    store.accept_generation("root")
    session = store.create_revision_session(
        session_id="s1", project_id="p1", base_generation_id="root",
        state={"feedbacks": [{"feedback_id": "feedback", "status": "resolved"}]},
    )
    delivery = delivery_values(store, 'candidate', project_id='p1')
    store.create_agent_run(run_id='run', project_id='p1', parent_generation_id='root', status='ready',
        state=ready_feedback_state(run_id='run', project_id='p1', parent_id='root',
            session_id='s1', feedback_id='feedback', delivery=delivery))
    session = store.update_revision_session(
        "s1", expected_revision=session.state_revision, status="running",
        state={**session.state, 'current_run_id': 'run'}, head_generation_id=None,
    )
    store.register_trusted_feedback(run_id='run', identity=dict(generation_id='candidate', project_id='p1',
        source_sha256='hash', structure_revision=1, parent_generation_id='root',
        change_origin='user_feedback', created_by='agent'), delivery=delivery)

    discarded = discard_revision(store, session.session_id)

    assert discarded.status == "discarded"
    assert store.get("p1").accepted_generation_id == "root"
    assert store.get_generation("candidate").review_status == "rejected"
