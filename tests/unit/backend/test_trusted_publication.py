# -*- coding: utf-8 -*-
"""首次任务预登记与单事务可信提升，失败不能留下半个可信版本。"""

import sqlite3
from pathlib import Path

import pytest

from persistence.store import ProjectStore
from tests.support.delivery import delivery_values, ready_feedback_state


##### 项目与交付夹具板块 #####

@pytest.fixture()
def case(tmp_path):
    store = ProjectStore(tmp_path / "state.db")
    store.create(project_id="p1", docx_path="paper.docx", bib_path=None, citation_map_path=None,
                 template_id="ouc-bachelor", reference_source="word_list",
                 workspace_dir=str(tmp_path / "project"), source_sha256="hash")
    store.create_generation(generation_id="g1", project_id="p1", structure_revision=1, source_sha256="hash")
    return store


def publication(store):
    values = delivery_values(store, "g1")
    values.pop("trusted", None)
    values.pop("review_status", None)
    return {**values, "report_dir": str(store.db_path.parent / "project/reports/g1"),
            "detail": "终验通过", "gate_snapshot": {"published": True, "gate_statuses": {
                "content-fidelity": "passed", "structure": "passed", "compile": "passed", "format": "passed"}}}


##### 预登记与原子提升板块 #####

@pytest.mark.parametrize('missing', ['session_id', 'agent'])
def test_feedback_cannot_publish_without_session_and_unified_terminal_state(case, missing):
    case.publish_trusted_generation('g1', **publication(case))
    case.accept_generation('g1')
    session = case.create_revision_session(session_id='session', project_id='p1', base_generation_id='g1',
        state={'current_run_id': 'run', 'feedbacks': [{'feedback_id': 'feedback', 'status': 'pending'}]})
    case.update_revision_session('session', expected_revision=session.state_revision,
        status='running', state=session.state, head_generation_id=None)
    delivery = delivery_values(case, 'child', project_id='p1')
    state = {'session_id': 'session', 'feedback_ids': ['feedback'], 'agent': {
        'run_id': 'run', 'project_id': 'p1', 'parent_generation_id': 'g1', 'mode': 'user_feedback',
        'status': 'ready', 'goals': [{'goal_id': 'goal', 'confirmed': True, 'status': 'satisfied'}],
        'gate_snapshots': {'final': {'gate_statuses': delivery['gate_snapshot']['gate_statuses']}}}}
    state.pop(missing)
    case.create_agent_run(run_id='run', project_id='p1', parent_generation_id='g1', status='ready', state=state)
    with pytest.raises(ValueError, match='会话|统一运行'):
        case.register_trusted_feedback(run_id='run', delivery=delivery,
            identity=dict(generation_id='child', project_id='p1', source_sha256='hash', structure_revision=1,
                parent_generation_id='g1', change_origin='user_feedback', created_by='agent'))
    assert case.get_generation('child') is None
    assert case.get_agent_run('run').candidate_generation_id is None
    assert case.get('p1').accepted_generation_id == 'g1'

@pytest.mark.parametrize('run_status', ['failed', 'stopped', 'waiting_user'])
def test_closed_or_paused_feedback_cannot_register_delayed_delivery(case, run_status):
    """晚到的打包结果不能复活已失败、停止或等待用户的运行。"""
    case.publish_trusted_generation('g1', **publication(case))
    case.accept_generation('g1')
    case.create_agent_run(run_id='delayed', project_id='p1', parent_generation_id='g1',
                          status=run_status, state={})
    identity = dict(generation_id='child', project_id='p1', structure_revision=1, source_sha256='hash',
                    parent_generation_id='g1', change_origin='user_feedback', created_by='agent')
    with pytest.raises(ValueError, match='反馈运行'):
        case.register_trusted_feedback(identity=identity,
            delivery=delivery_values(case, 'child', project_id='p1'), run_id='delayed')
    restored = ProjectStore(case.db_path)
    assert restored.get_generation('child') is None
    assert restored.get_agent_run('delayed').status == run_status
    assert restored.get_agent_run('delayed').candidate_generation_id is None
    assert restored.get('p1').accepted_generation_id == 'g1'


@pytest.mark.parametrize('change', ['missing', 'current_run', 'head', 'accepted', 'discarded', 'parent_rejected'])
def test_feedback_registration_rechecks_session_and_parent_in_its_transaction(case, change):
    case.publish_trusted_generation('g1', **publication(case))
    case.accept_generation('g1')
    session = case.create_revision_session(session_id='session', project_id='p1', base_generation_id='g1',
        state={'current_run_id': 'run'})
    case.update_revision_session('session', expected_revision=session.state_revision,
        status='running', state=session.state, head_generation_id=None)
    case.create_agent_run(run_id='run', project_id='p1', parent_generation_id='g1',
        status='ready', state={'session_id': 'session'})
    with case._connect() as connection:
        if change == 'missing':
            connection.execute("DELETE FROM revision_sessions WHERE session_id = 'session'")
        elif change == 'current_run':
            connection.execute("UPDATE revision_sessions SET state_json = '{}' WHERE session_id = 'session'")
        elif change == 'head':
            connection.execute("UPDATE revision_sessions SET head_generation_id = 'new-head' WHERE session_id = 'session'")
        elif change == 'accepted':
            connection.execute("UPDATE projects SET accepted_generation_id = NULL WHERE project_id = 'p1'")
        elif change == 'discarded':
            connection.execute("UPDATE revision_sessions SET status = 'discarded' WHERE session_id = 'session'")
        else:
            connection.execute("UPDATE generations SET review_status = 'rejected' WHERE generation_id = 'g1'")
    identity = dict(generation_id='child', project_id='p1', structure_revision=1, source_sha256='hash',
                    parent_generation_id='g1', change_origin='user_feedback', created_by='agent')
    before = case.get('p1').accepted_generation_id
    with pytest.raises(ValueError, match='会话|父版本'):
        case.register_trusted_feedback(identity=identity,
            delivery=delivery_values(case, 'child', project_id='p1'), run_id='run')
    assert case.get_generation('child') is None
    assert case.get_agent_run('run').candidate_generation_id is None
    assert case.get('p1').accepted_generation_id == before

def test_preregistered_generation_is_pollable_but_not_reviewable(case):
    record = case.get_generation("g1")
    assert record.status == "queued" and not record.trusted
    assert record.review_status == "not_reviewable" and not record.artifact_manifest
    case.update_generation("g1", task_id="t1", status="running")
    assert ProjectStore(case.db_path).get_generation("g1").task_id == "t1"
    with pytest.raises(ValueError):
        case.accept_generation("g1")
    with pytest.raises(ValueError):
        case.rollback_accepted_generation("g1")
    with pytest.raises(ValueError):
        case.create_generation(generation_id="child", project_id="p1", structure_revision=1,
                               source_sha256="hash", parent_generation_id="g1")


def test_ordinary_update_cannot_construct_trust_even_with_complete_delivery(case):
    with pytest.raises(ValueError):
        case.update_generation("g1", **publication(case), trusted=True, review_status="pending")
    assert not case.get_generation("g1").trusted


@pytest.mark.parametrize("values", [
    {"status": "success"}, {"review_status": "pending"}, {"review_status": "rejected"}, {"output_dir": "some-output"},
    {"artifact_manifest": {"artifacts": [{"kind": "pdf"}]}},
])
def test_preregistered_record_cannot_accumulate_delivery_fields(case, values):
    with pytest.raises(ValueError):
        case.update_generation("g1", **values)


def test_dedicated_transaction_promotes_same_identifier_and_preserves_task(case):
    case.update_generation("g1", task_id="t1", status="running")
    result = case.publish_trusted_generation("g1", **publication(case))
    assert result.generation_id == "g1" and result.task_id == "t1"
    assert result.trusted and result.review_status == "pending" and result.status == "success"
    assert result.gate_snapshot["published"] and len(result.artifact_manifest["artifacts"]) == 3


def test_initial_agent_association_does_not_grant_trust(case):
    case.update_generation("g1", status="running")
    run = case.create_agent_run(run_id="initial-agent", project_id="p1", parent_generation_id="",
        candidate_generation_id="g1", mode="initial_generation", status="running", state={"goals": []})
    assert run.candidate_generation_id == "g1"
    generation = case.get_generation("g1")
    assert not generation.trusted and generation.review_status == "not_reviewable"
    with pytest.raises(ValueError):
        case.accept_generation("g1")


def test_sql_failure_rolls_back_all_trusted_fields(case):
    case.update_generation("g1", status="running")
    values = publication(case)
    with case._connect() as connection:
        connection.execute("CREATE TRIGGER reject_publication AFTER UPDATE OF trusted ON generations "
                           "WHEN NEW.trusted = 1 BEGIN SELECT RAISE(ABORT, 'publication interrupted'); END")
    with pytest.raises(sqlite3.IntegrityError, match="publication interrupted"):
        case.publish_trusted_generation("g1", **values)
    record = ProjectStore(case.db_path).get_generation("g1")
    assert not record.trusted and record.review_status == "not_reviewable"
    assert record.output_dir is None and not record.artifact_manifest and not record.gate_snapshot
    assert case.get("p1").accepted_generation_id is None


def test_second_source_archive_cannot_be_hidden_under_another_name(case):
    values = publication(case)
    items = values['artifact_manifest']['artifacts']
    original = next(item for item in items if item['kind'] == 'latex_source')
    duplicate = {**original, 'name': 'another-source.zip', 'relative_path': 'another-source.zip'}
    root = Path(values['output_dir'])
    (root / duplicate['relative_path']).write_bytes((root / original['relative_path']).read_bytes())
    items.append(duplicate)
    with pytest.raises(ValueError, match='唯一'):
        case.publish_trusted_generation('g1', **values)
    record = case.get_generation('g1')
    assert not record.trusted and record.review_status == 'not_reviewable'
    assert not record.artifact_manifest and record.output_dir is None


@pytest.mark.parametrize("fault", ["gate", "publication", "quality", "missing_pdf", "extra_gate", "hidden_degradation", "malformed_issues"])
def test_promotion_validates_complete_delivery_and_actual_gate_statuses(case, fault):
    values = publication(case)
    if fault == "gate":
        values["gate_snapshot"]["gate_statuses"]["structure"] = "not_run"
    elif fault == "publication":
        values["gate_snapshot"]["published"] = False
    elif fault == "quality":
        values["quality_status"] = "pending"
    elif fault == "extra_gate":
        values["gate_snapshot"]["gate_statuses"]["unclassified"] = "internal_error"
    elif fault == "hidden_degradation":
        values["gate_snapshot"]["gate_statuses"]["format"] = "degraded"
    elif fault == "malformed_issues":
        values["gate_snapshot"]["issues"] = [None]
    else:
        values["artifact_manifest"]["artifacts"] = [item for item in values["artifact_manifest"]["artifacts"] if item["kind"] != "pdf"]
    with pytest.raises(ValueError):
        case.publish_trusted_generation("g1", **values)
    assert not case.get_generation("g1").trusted


@pytest.mark.parametrize("failure", [None, "gate", "sql"])
def test_feedback_is_registered_only_after_successful_complete_delivery(case, failure):
    case.publish_trusted_generation("g1", **publication(case))
    case.accept_generation('g1')
    delivery = delivery_values(case, "child", project_id="p1")
    session = case.create_revision_session(session_id='session', project_id='p1', base_generation_id='g1',
        state={'current_run_id': 'run-1', 'feedbacks': [{'feedback_id': 'feedback', 'status': 'pending'}]})
    case.update_revision_session('session', expected_revision=session.state_revision,
        status='running', state=session.state, head_generation_id=None)
    case.create_agent_run(run_id="run-1", project_id="p1", parent_generation_id="g1", status="ready",
        state=ready_feedback_state(run_id='run-1', project_id='p1', parent_id='g1', session_id='session',
            feedback_id='feedback', delivery=delivery))
    identity = dict(generation_id="child", project_id="p1", structure_revision=1, source_sha256="hash",
                    parent_generation_id="g1", change_origin="user_feedback", created_by="agent")
    with pytest.raises(ValueError, match="终验"):
        case.create_generation(**identity)
    assert case.get_generation("child") is None
    if failure == "gate":
        delivery["gate_snapshot"]["gate_statuses"]["compile"] = "failed"
    elif failure == "sql":
        with case._connect() as connection:
            connection.execute("CREATE TRIGGER reject_feedback AFTER UPDATE OF trusted ON generations "
                               "WHEN NEW.generation_id = 'child' BEGIN SELECT RAISE(ABORT, 'injected'); END")
    if failure:
        with pytest.raises((ValueError, sqlite3.IntegrityError)):
            case.register_trusted_feedback(identity=identity, delivery=delivery, run_id="run-1")
        assert case.get_generation("child") is None
        assert case.get_agent_run("run-1").candidate_generation_id is None
    else:
        result = case.register_trusted_feedback(identity=identity, delivery=delivery, run_id="run-1")
        assert result.trusted and result.review_status == "pending"
        assert case.get_agent_run("run-1").candidate_generation_id == "child"
        assert case.get_agent_run("run-1").status == "ready"


def test_feedback_run_association_failure_rolls_back_candidate_and_run_status(case):
    case.publish_trusted_generation('g1', **publication(case))
    case.accept_generation('g1')
    delivery = delivery_values(case, 'child', project_id='p1')
    session = case.create_revision_session(session_id='session', project_id='p1', base_generation_id='g1',
        state={'current_run_id': 'run', 'feedbacks': [{'feedback_id': 'feedback', 'status': 'pending'}]})
    case.update_revision_session('session', expected_revision=session.state_revision,
        status='running', state=session.state, head_generation_id=None)
    run = case.create_agent_run(run_id='run', project_id='p1', parent_generation_id='g1',
        status='ready', state=ready_feedback_state(run_id='run', project_id='p1', parent_id='g1',
            session_id='session', feedback_id='feedback', delivery=delivery))
    with case._connect() as connection:
        connection.execute("CREATE TRIGGER reject_run_delivery AFTER UPDATE OF candidate_generation_id ON agent_runs "
                           "BEGIN SELECT RAISE(ABORT, 'run association interrupted'); END")
    with pytest.raises(sqlite3.IntegrityError, match='run association interrupted'):
        case.register_trusted_feedback(run_id='run',
            identity=dict(generation_id='child', project_id='p1', structure_revision=1, source_sha256='hash',
                parent_generation_id='g1', change_origin='user_feedback', created_by='agent'),
            delivery=delivery)
    restored = ProjectStore(case.db_path)
    assert restored.get_generation('child') is None
    assert restored.get_agent_run('run') == run


@pytest.mark.parametrize('interrupt_session', [False, True])
def test_confirmed_feedback_delivery_and_session_head_commit_together(case, interrupt_session):
    case.publish_trusted_generation('g1', **publication(case))
    case.accept_generation('g1')
    session = case.create_revision_session(session_id='session', project_id='p1', base_generation_id='g1',
        state={'current_run_id': 'run', 'feedbacks': [{'feedback_id': 'feedback', 'status': 'pending'}]})
    session = case.update_revision_session('session', expected_revision=session.state_revision,
        status='running', state=session.state, head_generation_id=None)
    delivery = delivery_values(case, 'child', project_id='p1')
    run = case.create_agent_run(run_id='run', project_id='p1', parent_generation_id='g1', status='ready',
        state={'session_id': 'session', 'feedback_ids': ['feedback'], 'agent': {
            'run_id': 'run', 'project_id': 'p1', 'parent_generation_id': 'g1', 'mode': 'user_feedback',
            'status': 'ready', 'goals': [{'goal_id': 'goal', 'confirmed': True, 'status': 'satisfied'}],
            'gate_snapshots': {'final': {'gate_statuses': delivery['gate_snapshot']['gate_statuses']}}}})
    identity = dict(generation_id='child', project_id='p1', structure_revision=1, source_sha256='hash',
                    parent_generation_id='g1', change_origin='user_feedback', created_by='agent')
    if interrupt_session:
        with case._connect() as connection:
            connection.execute("CREATE TRIGGER reject_session_delivery AFTER UPDATE OF head_generation_id ON revision_sessions "
                               "BEGIN SELECT RAISE(ABORT, 'session delivery interrupted'); END")
        with pytest.raises(sqlite3.IntegrityError, match='session delivery interrupted'):
            case.register_trusted_feedback(run_id='run', identity=identity, delivery=delivery)
        assert case.get_generation('child') is None
        assert case.get_agent_run('run') == run
        assert case.get_revision_session('session') == session
    else:
        case.register_trusted_feedback(run_id='run', identity=identity, delivery=delivery)
        completed = case.get_revision_session('session')
        assert completed.status == 'reviewable' and completed.head_generation_id == 'child'
        assert completed.state['feedbacks'][0]['status'] == 'resolved'
        assert completed.state['current_run_revision'] == case.get_agent_run('run').state_revision
    assert case.get('p1').accepted_generation_id == 'g1'
