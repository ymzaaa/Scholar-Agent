# -*- coding: utf-8 -*-
"""正式版本列表、接受、拒绝、回退及历史入口移除验收。"""

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from main import app
from persistence.store import ProjectStore, get_store
from infrastructure.tasks import TaskManager, get_task_manager
from generation.versions import freeze_confirmed_snapshot, load_parent_snapshot, VersionContractError
from tests.support.delivery import delivery_values, ready_feedback_state


##### 版本夹具板块 #####


@pytest.fixture()
def context(tmp_path):
    store = ProjectStore(tmp_path / "state.db")
    project = store.create(project_id="p1", docx_path="paper.docx", bib_path=None, citation_map_path=None,
        template_id="ouc-bachelor", reference_source="word_list", workspace_dir=str(tmp_path / "project"),
        source_sha256="hash", structure_revision=1, structure_confirmed=True)
    tasks = TaskManager(store.db_path)
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_task_manager] = lambda: tasks
    try:
        yield TestClient(app), store, project
    finally:
        tasks._executor.shutdown(wait=True)
        app.dependency_overrides.clear()


def trusted_version(store, project, parent=None):
    identifier = str(uuid.uuid4())
    path, digest = freeze_confirmed_snapshot(project, identifier,
        {"confirmed": True, "revision": 1, "project_source_sha256": "hash"}, structure_revision=1)
    store.create_generation(generation_id=identifier, project_id="p1", source_sha256="hash",
        structure_revision=1, parent_generation_id=parent,
        confirmed_snapshot_path=path, confirmed_snapshot_sha256=digest)
    return store.publish_trusted_generation(identifier, **delivery_values(store, identifier))


##### 正式版本评审板块 #####


def test_list_accept_reject_and_rollback_have_consistent_identity(context):
    client, store, project = context
    root = trusted_version(store, project)
    accepted = client.post(f"/api/versions/{root.generation_id}/accept").json()
    assert accepted["version_id"] == root.generation_id
    assert accepted["previous_accepted_version_id"] is None
    repeated = client.post(f"/api/versions/{root.generation_id}/accept").json()
    assert repeated["previous_accepted_version_id"] == root.generation_id
    queried = client.get(f"/api/generations/{root.generation_id}").json()
    assert queried["accepted"] and "version_id" not in queried
    child = trusted_version(store, project, root.generation_id)
    response = client.post(f"/api/versions/{child.generation_id}/accept")
    assert response.status_code == 200, response.text
    assert response.json()["previous_accepted_version_id"] == root.generation_id
    listing = client.get("/api/projects/p1/versions").json()
    assert listing["accepted_version_id"] == child.generation_id
    assert [v["accepted"] for v in listing["versions"]] == [False, True]
    assert [v["version_number"] for v in listing["versions"]] == [1, 2]
    rollback = client.post(f"/api/versions/{child.generation_id}/rollback")
    assert rollback.status_code == 200, rollback.text
    assert rollback.json() == {
        "project_id": "p1", "version_id": child.generation_id, "accepted_version_id": root.generation_id,
        "previous_accepted_version_id": child.generation_id, "review_status": "rejected"}
    rejected = client.post(f"/api/versions/{child.generation_id}/reject").json()
    assert rejected["review_status"] == "rejected"
    assert rejected["previous_accepted_version_id"] == rejected["accepted_version_id"] == root.generation_id
    assert client.post(f"/api/versions/{child.generation_id}/accept").status_code == 409


def test_untrusted_version_cannot_be_accepted(context):
    client, store, _ = context
    store.create_generation(generation_id="untrusted", project_id="p1", structure_revision=1, source_sha256="hash")
    assert client.post("/api/versions/untrusted/accept").status_code == 409
    assert client.post("/api/versions/untrusted/reject").status_code == 409


def test_only_direct_candidate_can_replace_current_version(context):
    client, store, project = context
    root = trusted_version(store, project)
    sibling_root = trusted_version(store, project)
    store.accept_generation(root.generation_id)
    assert client.post(f"/api/versions/{sibling_root.generation_id}/accept").status_code == 409


def test_revision_session_can_accept_its_verified_descendant_without_changing_version_api(context):
    client, store, project = context
    root = trusted_version(store, project)
    store.accept_generation(root.generation_id)
    session = store.create_revision_session(session_id='session-chain', project_id='p1',
        base_generation_id=root.generation_id, state={'feedbacks': [{'feedback_id': 'feedback', 'status': 'pending'}]})
    parent_id = root.generation_id
    identifiers = []
    for _ in range(2):
        identifier = str(uuid.uuid4())
        run_id = str(uuid.uuid4())
        delivery = delivery_values(store, identifier, project_id='p1')
        store.create_agent_run(run_id=run_id, project_id='p1', parent_generation_id=parent_id,
            status='ready', state=ready_feedback_state(run_id=run_id, project_id='p1', parent_id=parent_id,
                session_id=session.session_id, feedback_id='feedback', delivery=delivery))
        session = store.update_revision_session(session.session_id, expected_revision=session.state_revision,
            status='running', state={**session.state, 'current_run_id': run_id},
            head_generation_id=session.head_generation_id)
        path, digest = freeze_confirmed_snapshot(project, identifier,
            {'confirmed': True, 'revision': 1, 'project_source_sha256': 'hash'}, structure_revision=1)
        store.register_trusted_feedback(run_id=run_id,
            identity=dict(generation_id=identifier, project_id='p1', source_sha256='hash', structure_revision=1,
                parent_generation_id=parent_id, change_origin='user_feedback', created_by='agent',
                confirmed_snapshot_path=path, confirmed_snapshot_sha256=digest),
            delivery=delivery)
        identifiers.append(identifier)
        parent_id = identifier
        session = store.get_revision_session(session.session_id)
        session = store.update_revision_session(session.session_id, expected_revision=session.state_revision,
            status='reviewable', state=session.state, head_generation_id=parent_id)
    store.update_revision_session(session.session_id, expected_revision=session.state_revision,
        status='reviewable', state=session.state, head_generation_id=parent_id)
    assert client.post(f'/api/versions/{parent_id}/accept').status_code == 409
    response = client.post('/api/revision-sessions/session-chain/accept')
    assert response.status_code == 200, response.text
    assert response.json()['current_version_id'] == parent_id
    assert response.json()['status'] == 'accepted'
    assert client.post('/api/revision-sessions/session-chain/accept').json() == response.json()
    listing = client.get('/api/projects/p1/versions').json()
    assert listing['accepted_version_id'] == parent_id
    assert [entry['review_status'] for entry in listing['versions']] == ['superseded', 'superseded', 'accepted']
    rollback = client.post(f'/api/versions/{parent_id}/rollback')
    assert rollback.status_code == 200, rollback.text
    assert rollback.json()['version_id'] == parent_id
    assert rollback.json()['accepted_version_id'] == identifiers[0]


@pytest.mark.parametrize("tampered", ["pdf", "snapshot", "snapshot_missing"])
def test_review_rejects_tampered_or_missing_frozen_parent(context, tampered):
    client, store, project = context
    root = trusted_version(store, project)
    if tampered == "pdf":
        (Path(root.output_dir) / "main.pdf").write_bytes(b"tampered")
    elif tampered == "snapshot":
        Path(root.confirmed_snapshot_path).write_text("{}")
    else:
        Path(root.confirmed_snapshot_path).unlink()
        latest = Path(project.workspace_dir) / "reports" / "confirmed_structure.json"
        latest.write_text('{"confirmed": true, "revision": 1, "project_source_sha256": "hash"}')
        with pytest.raises(VersionContractError):
            load_parent_snapshot(root, project)
    assert client.post(f"/api/versions/{root.generation_id}/accept").status_code == 409


def test_historical_candidate_and_duplicate_queries_are_gone(context):
    client, store, project = context
    root = trusted_version(store, project)
    assert client.post(f"/api/versions/{root.generation_id}/candidates", json={}).status_code == 404
    assert client.get(f"/api/versions/{root.generation_id}").status_code == 404
    assert client.get(f"/api/generations/{root.generation_id}/artifacts").status_code == 404
    assert len(store.list_generations("p1")) == 1


def test_rollback_rejects_damaged_direct_parent_without_moving_pointer(context):
    """回退父产物损坏属于可读版本冲突，当前接受指针不能变化。"""
    client, store, project = context
    root = trusted_version(store, project)
    store.accept_generation(root.generation_id)
    child = trusted_version(store, project, root.generation_id)
    store.accept_generation(child.generation_id)
    (Path(root.output_dir) / "main.pdf").unlink()
    response = client.post(f"/api/versions/{child.generation_id}/rollback")
    assert response.status_code == 409
    assert store.get("p1").accepted_generation_id == child.generation_id
