# -*- coding: utf-8 -*-
"""反馈理解只执行一次，草稿确认与版本登记分开。"""

from copy import deepcopy

import pytest

from revision.feedback_goals import normalize_feedback_goals, confirm_feedback_goals
from persistence.store import ProjectStore
from tests.support.delivery import delivery_values, frozen_values


##### 可信会话夹具板块 #####

@pytest.fixture()
def case(tmp_path):
    store = ProjectStore(tmp_path / "state.db")
    project = store.create(project_id="project", docx_path="paper.docx", bib_path=None,
        citation_map_path=None, template_id="ouc-bachelor", reference_source="word_list",
        workspace_dir=str(tmp_path / "project"), source_sha256="source", structure_revision=1, structure_confirmed=True)
    store.create_generation(generation_id="parent", project_id="project", structure_revision=1,
                            source_sha256="source", **frozen_values(project, 'parent'))
    store.publish_trusted_generation("parent", **delivery_values(store, "parent"))
    store.accept_generation("parent")
    feedback = {"feedback_id": "feedback", "text": "段后距离调整为6pt", "selected_unit_ids": ["paragraph"], "status": "pending"}
    store.create_revision_session(session_id="session", project_id="project", base_generation_id="parent",
                                  state={"feedbacks": [feedback], "current_run_id": "run"})
    store.create_agent_run(run_id="run", project_id="project", parent_generation_id="parent",
        status="running", state={"session_id": "session", "feedback_ids": ["feedback"],
            "authorization": {"external_processing_allowed": True, "allowed_llm_tasks": ["feedback_normalization"]}})
    return store, [{"unit_id": "paragraph", "role": "body_paragraph", "text": "普通正文。"}]


class Model:
    def __init__(self, failure=None):
        self.calls = 0
        self.failure = failure

    def invoke_tools(self, **kwargs):
        self.calls += 1
        if self.failure:
            raise self.failure
        assert kwargs["tools"] == []
        return {"goal_drafts": [{"feedback_ids": ["feedback"], "kind": "format",
            "target_unit_ids": ["paragraph"], "description": "段后距离调整为6pt", "missing_information": []}]}


def normalize(store, evidence, model):
    return normalize_feedback_goals(store, "run", expected_revision=store.get_agent_run("run").state_revision,
                                    model=model, evidence=evidence)


##### 一次理解与故障恢复板块 #####

@pytest.mark.parametrize('override', [None, 'valid', 'unknown'])
def test_goal_confirmation_explicitly_replaces_only_known_protection(case, override):
    store, evidence = case
    protected = {'constraint_id': 'accepted-format', 'scope_ref': 'paragraph',
        'description': '保留此前段落设置', 'verifier': {'mode': 'human_review'}}
    store.update_generation('parent', active_constraints=[protected])
    model = Model()
    run = normalize(store, evidence, model)
    draft = run.state['goal_drafts'][0]
    assert draft['protections'] == [{'constraint_id': 'accepted-format', 'unit_id': 'paragraph',
                                     'description': '保留此前段落设置'}]
    decision = {'goal_id': draft['goal_id']}
    if override:
        decision['supersedes_constraint_ids'] = ['accepted-format' if override == 'valid' else 'unknown']
    if override == 'unknown':
        with pytest.raises(ValueError, match='保护'):
            confirm_feedback_goals(store, 'run', expected_revision=run.state_revision,
                                   decisions=[decision], evidence=evidence)
        assert store.get_agent_run('run').status == 'waiting_user'
    else:
        confirmed = confirm_feedback_goals(store, 'run', expected_revision=run.state_revision,
                                           decisions=[decision], evidence=evidence)
        assert confirmed.state['goals'][0].get('supersedes_constraint_ids', []) == (
            ['accepted-format'] if override else [])
    assert store.get_generation('parent').active_constraints == [protected]
    assert model.calls == 1


@pytest.mark.parametrize('phase', ['understanding', 'confirmation'])
def test_parent_change_between_context_read_and_write_cannot_commit_goal_state(case, monkeypatch, phase):
    from revision import feedback_goals as feedback_goal_service
    store, evidence = case
    model = Model()
    run = normalize(store, evidence, model) if phase == 'confirmation' else store.get_agent_run('run')
    original_context, reads = feedback_goal_service._context, []

    def change_after_read(*args):
        result = original_context(*args)
        reads.append(True)
        if len(reads) == 1:
            with store._connect() as connection:
                connection.execute("UPDATE projects SET source_sha256 = 'changed' WHERE project_id = 'project'")
        return result

    monkeypatch.setattr(feedback_goal_service, '_context', change_after_read)
    before_calls = model.calls
    with pytest.raises(ValueError, match='父版本'):
        if phase == 'understanding':
            normalize(store, evidence, model)
        else:
            confirm_feedback_goals(store, 'run', expected_revision=run.state_revision,
                decisions=[{'goal_id': run.state['goal_drafts'][0]['goal_id']}], evidence=evidence)
    saved = store.get_agent_run('run')
    assert saved.state == run.state and saved.state_revision == run.state_revision
    assert model.calls == before_calls and saved.candidate_generation_id is None


def test_normalization_is_cached_and_never_creates_candidate(case):
    store, evidence = case
    model = Model()
    result = normalize(store, evidence, model)
    assert result.status == "waiting_user" and result.state["goal_drafts"]
    assert result.state.get("goals", []) == []
    assert normalize(store, evidence, model).state["goal_drafts"] == result.state["goal_drafts"]
    assert model.calls == 1 and len(store.list_generations("project")) == 1


def test_request_claim_is_persisted_before_external_call(case):
    store, evidence = case
    class Inspect(Model):
        def invoke_tools(self, **kwargs):
            saved = ProjectStore(store.db_path).get_agent_run("run")
            assert saved.state["normalization_status"] == "requested"
            assert saved.state.get("goals", []) == []
            return super().invoke_tools(**kwargs)
    normalize(store, evidence, Inspect())


def test_failed_request_is_not_retried_on_restore(case):
    store, evidence = case
    model = Model(TimeoutError("private endpoint"))
    with pytest.raises(TimeoutError):
        normalize(store, evidence, model)
    saved = store.get_agent_run("run")
    assert saved.status == "failed" and saved.state["normalization_status"] == "failed"
    assert "private endpoint" not in saved.state["detail"]
    with pytest.raises(ValueError, match="重新调用"):
        normalize(store, evidence, model)
    assert model.calls == 1


def test_crash_after_claim_cannot_resend(case):
    store, evidence = case
    run = store.get_agent_run("run")
    store.update_agent_run("run", expected_revision=run.state_revision, status="running",
        state={**run.state, "normalization_status": "requested"}, candidate_generation_id=None)
    model = Model()
    with pytest.raises(ValueError, match="重新调用"):
        normalize(store, evidence, model)
    assert model.calls == 0


def test_missing_authorization_never_invokes_model(case):
    store, evidence = case
    run = store.get_agent_run("run")
    store.update_agent_run("run", expected_revision=run.state_revision, status="running",
        state={**run.state, "authorization": {}}, candidate_generation_id=None)
    model = Model()
    with pytest.raises(ValueError, match="授权"):
        normalize(store, evidence, model)
    assert model.calls == 0


##### 草稿确认板块 #####

def test_confirmation_uses_revision_and_keeps_raw_feedback(case):
    store, evidence = case
    original = deepcopy(store.get_revision_session("session").state)
    run = normalize(store, evidence, Model())
    decisions = [{"goal_id": run.state["goal_drafts"][0]["goal_id"]}]
    result = confirm_feedback_goals(store, "run", expected_revision=run.state_revision,
                                   decisions=decisions, evidence=evidence)
    assert result.state["goals"][0]["confirmed"] is True
    assert result.status == "running" and result.candidate_generation_id is None
    assert store.get_revision_session("session").state == original
    with pytest.raises(ValueError, match="状态"):
        confirm_feedback_goals(store, "run", expected_revision=run.state_revision,
                               decisions=decisions, evidence=evidence)


def test_confirmation_rejects_unknown_goal_without_state_change(case):
    store, evidence = case
    run = normalize(store, evidence, Model())
    with pytest.raises(ValueError):
        confirm_feedback_goals(store, "run", expected_revision=run.state_revision,
                               decisions=[{"goal_id": "unknown"}], evidence=evidence)
    assert store.get_agent_run("run").state_revision == run.state_revision


def test_parent_change_blocks_confirmation(case):
    store, evidence = case
    run = normalize(store, evidence, Model())
    store.create_generation(generation_id="new-parent", project_id="project", structure_revision=1,
                            source_sha256="source", parent_generation_id="parent")
    store.publish_trusted_generation("new-parent", **delivery_values(store, "new-parent"))
    store.accept_generation("new-parent")
    with pytest.raises(ValueError, match="父版本"):
        confirm_feedback_goals(store, "run", expected_revision=run.state_revision,
            decisions=[{"goal_id": run.state["goal_drafts"][0]["goal_id"]}], evidence=evidence)
    assert store.get_agent_run("run").candidate_generation_id is None


@pytest.mark.parametrize('phase', ['understanding', 'confirmation'])
def test_replaced_session_run_cannot_understand_or_confirm_old_feedback(case, phase):
    store, evidence = case
    model = Model()
    run = normalize(store, evidence, model) if phase == 'confirmation' else store.get_agent_run('run')
    session = store.get_revision_session('session')
    store.update_revision_session('session', expected_revision=session.state_revision, status='running',
        state={**session.state, 'current_run_id': 'new-run'}, head_generation_id=session.head_generation_id)
    before_calls = model.calls
    with pytest.raises(ValueError, match='会话'):
        if phase == 'understanding':
            normalize(store, evidence, model)
        else:
            confirm_feedback_goals(store, 'run', expected_revision=run.state_revision,
                decisions=[{'goal_id': run.state['goal_drafts'][0]['goal_id']}], evidence=evidence)
    assert model.calls == before_calls
    assert store.get_agent_run('run').state_revision == run.state_revision


def test_rejected_session_head_cannot_supply_feedback_authority(case):
    store, evidence = case
    store.create_generation(generation_id='child', project_id='project', parent_generation_id='parent',
                            source_sha256='source', structure_revision=1)
    store.publish_trusted_generation('child', **delivery_values(store, 'child'))
    store.update_generation('child', review_status='rejected')
    session = store.get_revision_session('session')
    store.update_revision_session('session', expected_revision=session.state_revision, status='running',
        state={**session.state, 'current_run_id': 'child-run'}, head_generation_id='child')
    store.create_agent_run(run_id='child-run', project_id='project', parent_generation_id='child', status='running',
        state={**store.get_agent_run('run').state})
    model = Model()
    with pytest.raises(ValueError, match='父版本'):
        normalize_feedback_goals(store, 'child-run', expected_revision=store.get_agent_run('child-run').state_revision,
                                 model=model, evidence=evidence)
    assert model.calls == 0


def test_feedback_normalization_binds_only_its_explicit_task_authorization(case):
    from agents.quality_repair.runtime import AuthorizedPlanningModel
    from llm.policy import LLMAuthorization
    store, evidence = case
    calls = []

    class Client:
        def complete_with_tools(self, system, user, tools, *, authorization, task):
            assert authorization.external_processing_allowed and authorization.allows_task(task)
            assert task == 'feedback_normalization' and tools == []
            assert not authorization.allows_task('user_feedback_patch_generation')
            calls.append(task)
            return Model().invoke_tools(tools=tools)

    authorization = LLMAuthorization.for_tasks(['feedback_normalization'],
        external_processing_allowed=True, source='confirmed-feedback-batch')
    model = AuthorizedPlanningModel(Client(), authorization, 'feedback_normalization')
    result = normalize(store, evidence, model)
    assert result.status == 'waiting_user' and calls == ['feedback_normalization']
    assert result.candidate_generation_id is None and len(store.list_generations('project')) == 1


##### 批次创建原子性板块 #####

@pytest.fixture()
def preparation_case(case):
    store, _ = case
    with store._connect() as connection:
        connection.execute("DELETE FROM agent_runs WHERE run_id = 'run'")
    session = store.get_revision_session('session')
    state = {key: value for key, value in session.state.items() if key != 'current_run_id'}
    store.update_revision_session('session', expected_revision=session.state_revision, status='collecting',
                                  state=state, head_generation_id=None)
    return store


def test_failed_batch_queue_write_leaves_no_orphan_agent(preparation_case):
    import sqlite3
    from revision.service import prepare_revision_run
    store = preparation_case
    with store._connect() as connection:
        connection.execute("""CREATE TRIGGER fail_queue BEFORE UPDATE ON revision_sessions
            WHEN NEW.status = 'queued' BEGIN SELECT RAISE(ABORT, 'injected queue failure'); END""")
    with pytest.raises(sqlite3.DatabaseError, match='injected queue failure'):
        prepare_revision_run(store, 'session')
    with store._connect() as connection:
        assert connection.execute('SELECT COUNT(*) FROM agent_runs').fetchone()[0] == 0
    assert store.get_revision_session('session').status == 'collecting'


def test_two_stores_cannot_prepare_duplicate_agents_for_same_batch(preparation_case, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from revision import service as revision_service
    from generation import versions as version_service
    store = preparation_case
    ready = Barrier(2)
    original = version_service.validate_trusted_generation
    def validate(parent, project):
        original(parent, project)
        ready.wait(timeout=10)
    monkeypatch.setattr(revision_service, 'validate_trusted_generation', validate)
    monkeypatch.setattr(version_service, 'validate_trusted_generation', validate)
    def prepare():
        try:
            return revision_service.prepare_revision_run(ProjectStore(store.db_path), 'session')
        except ValueError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: prepare(), range(2)))
    assert sum(result is not None for result in results) == 1
    with store._connect() as connection:
        assert connection.execute('SELECT COUNT(*) FROM agent_runs').fetchone()[0] == 1
    session = store.get_revision_session('session')
    run = store.get_agent_run(session.state['current_run_id'])
    assert run.status == 'running' and run.state['authorization']['allowed_llm_tasks'] == []


def test_task_submission_error_closes_prepared_feedback_run(preparation_case):
    import json
    from api.schemas import ProcessRevisionRequest
    from api.routes.revisions import process_feedbacks
    class Tasks:
        def submit(self, *args, **kwargs):
            raise OSError('D:/private/database submission failure')
    store = preparation_case
    response = process_feedbacks('session', ProcessRevisionRequest(), store=store, tasks=Tasks())
    assert response.status_code == 500 and 'D:/private' not in response.body.decode()
    session = store.get_revision_session('session')
    run = store.get_agent_run(session.state['current_run_id'])
    assert session.status == run.status == 'failed'
    assert run.candidate_generation_id is None
    assert run.state['stop_reason'] == 'feedback_submission_failed'
    assert 'submission failure' not in json.dumps(run.state.get('detail'))


def test_resume_submission_error_cannot_leave_session_queued(case):
    from api.schemas import ResumeRevisionRequest
    from api.routes.revisions import resume_feedbacks
    store, evidence = case
    run = normalize(store, evidence, Model())
    session = store.get_revision_session('session')
    store.update_revision_session('session', expected_revision=session.state_revision,
        status='needs_user_input', state={**session.state, 'current_task_id': 'previous-completed-task'},
        head_generation_id=None)
    class Tasks:
        def submit(self, *args, **kwargs):
            raise OSError('injected resume submission failure')
    response = resume_feedbacks('session', ResumeRevisionRequest(expected_run_revision=run.state_revision),
                               store=store, tasks=Tasks())
    assert response.status_code == 500
    assert store.get_revision_session('session').status == 'failed'
    assert store.get_agent_run('run').state['goal_drafts'] == run.state['goal_drafts']
    assert store.get_agent_run('run').candidate_generation_id is None
