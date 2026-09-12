# -*- coding: utf-8 -*-
"""后台任务提交、终态和只读安全轮询。"""

import json

import pytest

from infrastructure.tasks import TaskManager, TaskStatus
from api.routes.tasks import get_task
from persistence.store import ProjectStore


##### 任务故障板块 #####


@pytest.fixture()
def context(tmp_path):
    store = ProjectStore(tmp_path / 'state.db')
    store.create(project_id='p1', docx_path='paper.docx', bib_path=None, citation_map_path=None,
                 template_id='ouc-bachelor', reference_source='word_list', workspace_dir=str(tmp_path / 'project'), source_sha256='hash')
    store.create_generation(generation_id='g1', project_id='p1', structure_revision=1, source_sha256='hash')
    manager = TaskManager(store.db_path)
    yield manager, store
    manager._executor.shutdown(wait=True)


def test_submit_failure_closes_linked_generation(context, monkeypatch):
    manager, store = context
    def fail(*args):
        assert store.get_generation('g1').task_id
        raise RuntimeError('submit failure')
    monkeypatch.setattr(manager._executor, 'submit', fail)
    with pytest.raises(RuntimeError, match='submit failure'):
        manager.submit(lambda: {}, project_id='p1', linked_generation_id='g1')
    generation = store.get_generation('g1')
    assert generation.status == 'failed' and generation.quality_status == 'internal_error'
    assert manager.get(generation.task_id).status == TaskStatus.FAILED


def test_non_serializable_result_is_terminal(context):
    manager, store = context
    task_id = manager.submit(lambda: {'not_json': object()}, project_id='p1', linked_generation_id='g1')
    manager._executor.shutdown(wait=True)
    assert manager.get(task_id).status == TaskStatus.FAILED
    assert store.get_generation('g1').status == 'failed'


@pytest.mark.parametrize('stage', [TaskStatus.RUNNING, TaskStatus.DONE])
def test_state_write_failure_is_terminal(context, monkeypatch, stage):
    manager, _ = context
    real_set = manager._set
    def failed_set(task_id, **values):
        if values['status'] == stage:
            raise OSError('state failure')
        return real_set(task_id, **values)
    monkeypatch.setattr(manager, '_set', failed_set)
    task_id = manager.submit(lambda: {}, project_id='p1')
    manager._executor.shutdown(wait=True)
    assert manager.get(task_id).status == TaskStatus.FAILED


def test_restart_reconciles_only_unfinished_linked_tasks(context, monkeypatch):
    manager, store = context
    done_id = manager.submit(lambda: {'ok': True}, project_id='p1')
    manager._executor.shutdown(wait=True)
    monkeypatch.setattr(manager._executor, 'submit', lambda *args: None)
    pending = manager.submit(lambda: {}, project_id='p1', linked_generation_id='g1')
    restored = TaskManager(store.db_path)
    try:
        assert restored.get(pending).status == TaskStatus.FAILED
        assert store.get_generation('g1').quality_status == 'internal_error'
        assert restored.get(done_id).status == TaskStatus.DONE
    finally:
        restored._executor.shutdown(wait=True)


@pytest.mark.parametrize('agent_status', ['running', 'validating', 'ready'])
def test_restart_closes_only_unfinished_initial_agent_associated_with_task(context, monkeypatch, agent_status):
    from agents.quality_repair.models import AgentState
    manager, store = context
    monkeypatch.setattr(manager._executor, 'submit', lambda *args: None)
    task_id = manager.submit(lambda: {}, project_id='p1', linked_generation_id='g1')
    state = AgentState(run_id='g1', project_id='p1', mode='initial_generation',
        template_id='ouc-bachelor', status=agent_status).to_dict()
    store.create_agent_run(run_id='g1', project_id='p1', parent_generation_id='',
        candidate_generation_id='g1', mode='initial_generation', status=agent_status, state={'agent': state})
    restored = TaskManager(store.db_path)
    try:
        run = store.get_agent_run('g1')
        assert run.status == ('ready' if agent_status == 'ready' else 'failed')
        assert run.state['agent']['status'] == run.status
        assert restored.get(task_id).status == TaskStatus.FAILED
        assert not store.get_generation('g1').trusted
    finally:
        restored._executor.shutdown(wait=True)


##### 查询安全板块 #####


def _revision_run(store):
    store.create_revision_session(session_id='session', project_id='p1', base_generation_id='parent',
        status='queued', state={'current_run_id': 'feedback-run'})
    store.create_agent_run(run_id='feedback-run', project_id='p1', parent_generation_id='parent',
        status='running', state={'session_id': 'session'})


def test_feedback_task_is_linked_before_worker_starts(context):
    manager, store = context
    _revision_run(store)
    def worker():
        session = store.get_revision_session('session')
        assert session.state['current_task_id']
        assert manager.get(session.state['current_task_id']).status == TaskStatus.RUNNING
        return {'ok': True}
    task_id = manager.submit(worker, project_id='p1', task_type='revision_batch', linked_revision_run_id='feedback-run')
    manager._executor.shutdown(wait=True)
    assert manager.get(task_id).status == TaskStatus.DONE


@pytest.mark.parametrize('failure', ['submit', 'serialization', 'restart'])
def test_interrupted_feedback_task_closes_run_and_session_without_candidate(context, monkeypatch, failure):
    manager, store = context
    _revision_run(store)
    if failure == 'submit':
        def fail(*args):
            assert store.get_revision_session('session').state['current_task_id']
            raise RuntimeError('injected feedback submission failure')
        monkeypatch.setattr(manager._executor, 'submit', fail)
        with pytest.raises(RuntimeError, match='injected feedback'):
            manager.submit(lambda: {}, project_id='p1', linked_revision_run_id='feedback-run')
    else:
        if failure == 'restart':
            monkeypatch.setattr(manager._executor, 'submit', lambda *args: None)
        manager.submit(lambda: {'invalid': object()}, project_id='p1', linked_revision_run_id='feedback-run')
        manager._executor.shutdown(wait=True)
        if failure == 'restart':
            restarted = TaskManager(store.db_path)
            restarted._executor.shutdown(wait=True)
    run, session = store.get_agent_run('feedback-run'), store.get_revision_session('session')
    assert run.status == session.status == 'failed'
    assert run.candidate_generation_id is None and len(store.list_generations('p1')) == 1
    assert session.state['current_run_revision'] == run.state_revision


@pytest.mark.parametrize('status', ['validating', 'ready'])
@pytest.mark.parametrize('failure', ['worker', 'restart'])
def test_unregistered_ready_feedback_cannot_survive_failed_delivery(context, monkeypatch, status, failure):
    manager, store = context
    _revision_run(store)

    def prepared():
        run = store.get_agent_run('feedback-run')
        store.update_agent_run(run.run_id, expected_revision=run.state_revision, status=status,
            state={**run.state, 'agent': {'status': status, 'planning_round': 2,
                'pending_action': {'tool': 'read_source'}, 'goals': [{'goal_id': 'goal'}]}},
            candidate_generation_id=None)

    def fail():
        prepared()
        raise OSError('delivery interrupted')

    if failure == 'restart':
        monkeypatch.setattr(manager._executor, 'submit', lambda *args: None)
    task_id = manager.submit(fail, project_id='p1', linked_revision_run_id='feedback-run')
    manager._executor.shutdown(wait=True)
    if failure == 'restart':
        prepared()
        restored = TaskManager(store.db_path)
        restored._executor.shutdown(wait=True)
    run = store.get_agent_run('feedback-run')
    assert run.status == store.get_revision_session('session').status == 'failed'
    assert manager.get(task_id).status == TaskStatus.FAILED and run.candidate_generation_id is None
    assert run.state['agent']['status'] == 'failed' and run.state['agent']['pending_action'] is None
    assert run.state['agent']['planning_round'] == 2 and run.state['agent']['goals'] == [{'goal_id': 'goal'}]


def test_completed_feedback_pause_survives_backend_restart(context):
    manager, store = context
    _revision_run(store)
    def pause():
        run = store.get_agent_run('feedback-run')
        store.update_agent_run(run.run_id, expected_revision=run.state_revision, status='waiting_user',
            state={**run.state, 'normalization_status': 'completed'}, candidate_generation_id=None)
        session = store.get_revision_session('session')
        store.update_revision_session(session.session_id, expected_revision=session.state_revision,
            status='needs_user_input', state=session.state, head_generation_id=None)
        return {'status': 'waiting_user'}
    task_id = manager.submit(pause, project_id='p1', linked_revision_run_id='feedback-run')
    manager._executor.shutdown(wait=True)
    restored = TaskManager(store.db_path)
    try:
        assert restored.get(task_id).status == TaskStatus.DONE
        assert store.get_agent_run('feedback-run').status == 'waiting_user'
        assert store.get_revision_session('session').status == 'needs_user_input'
    finally:
        restored._executor.shutdown(wait=True)


def test_feedback_task_cannot_link_another_projects_run(context):
    manager, store = context
    _revision_run(store)
    with pytest.raises(ValueError, match='反馈'):
        manager.submit(lambda: {}, project_id='other', linked_revision_run_id='feedback-run')
    with store._connect() as connection:
        assert connection.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 0


def test_failure_polling_has_no_writes_or_private_paths(context, tmp_path):
    manager, _ = context
    task_id = manager.submit(lambda: (_ for _ in ()).throw(OSError('D:/private/server/trace')), project_id='p1')
    manager._executor.shutdown(wait=True)
    before = {path: path.stat().st_mtime_ns for path in tmp_path.rglob('*') if path.is_file()}
    for _ in range(3):
        payload = get_task(task_id, tasks=manager).model_dump()
        assert payload['status'] == 'failed' and payload['result'] is None
        assert 'log_path' not in payload and 'traceback' not in json.dumps(payload)
        assert 'D:/' not in json.dumps(payload)
    assert before == {path: path.stat().st_mtime_ns for path in tmp_path.rglob('*') if path.is_file()}


def test_unknown_task_update_and_query(context):
    manager, _ = context
    with pytest.raises(KeyError):
        manager._set('missing', status=TaskStatus.FAILED)
    assert get_task('missing', tasks=manager).status_code == 404
