# -*- coding: utf-8 -*-
"""现有修订后台入口先理解一次，再等待用户确认目标，不能直接规划补丁。"""

import pytest

from revision.service import execute_revision_run
from tests.unit.backend.test_feedback_goals import case, Model


##### 单次理解与具体授权板块 #####

def test_revision_entry_normalizes_once_and_waits_for_goal_confirmation(case, monkeypatch):
    from revision import service as revision_service
    store, evidence = case
    model = Model()
    monkeypatch.setattr('revision.feedback_goals.load_feedback_evidence', lambda *args, **kwargs: evidence)
    monkeypatch.setattr(revision_service, '_feedback_model', lambda *args: model, raising=False)
    result = execute_revision_run(store, 'session', 'run')
    assert result['status'] == 'needs_user_input'
    run = store.get_agent_run('run')
    assert run.status == 'waiting_user' and run.state['stop_reason'] == 'goal_confirmation'
    assert result['goal_drafts'] == run.state['goal_drafts']
    assert run.candidate_generation_id is None and run.state['goals'] == []
    assert model.calls == 1 and len(store.list_generations('project')) == 1
    again = execute_revision_run(store, 'session', 'run')
    assert again == result and model.calls == 1


def test_revision_entry_without_normalization_authorization_never_constructs_model(case, monkeypatch):
    from revision import service as revision_service
    store, evidence = case
    run = store.get_agent_run('run')
    store.update_agent_run('run', expected_revision=run.state_revision, status='running',
        state={**run.state, 'authorization': {}}, candidate_generation_id=None)
    monkeypatch.setattr('revision.feedback_goals.load_feedback_evidence', lambda *args, **kwargs: evidence)
    monkeypatch.setattr(revision_service, '_feedback_model',
        lambda *args: pytest.fail('未授权理解任务不得构造模型'), raising=False)
    result = execute_revision_run(store, 'session', 'run')
    assert result['status'] == 'needs_user_input' and result['goal_drafts'] == []
    assert store.get_agent_run('run').state.get('normalization_status') is None
    assert len(store.list_generations('project')) == 1


@pytest.mark.parametrize('failure', [
    TimeoutError('HTTPSConnectionPool(api.deepseek.com): Read timed out; private endpoint'),
    RuntimeError('D:/private/work/paper.tex: private endpoint'),
])
def test_normalization_failure_closes_same_run_and_session_without_candidate(case, monkeypatch, failure):
    from revision import service as revision_service
    store, evidence = case
    model = Model(failure)
    monkeypatch.setattr('revision.feedback_goals.load_feedback_evidence', lambda *args, **kwargs: evidence)
    monkeypatch.setattr(revision_service, '_feedback_model', lambda *args: model, raising=False)
    with pytest.raises(type(failure)):
        execute_revision_run(store, 'session', 'run')
    run = store.get_agent_run('run')
    assert run.status == 'failed' and run.state['normalization_status'] == 'failed'
    assert store.get_revision_session('session').status == 'failed'
    assert 'private endpoint' not in store.get_revision_session('session').state['detail']
    assert 'api.deepseek.com' not in store.get_revision_session('session').state['detail']
    assert 'D:/private' not in store.get_revision_session('session').state['detail']
    assert model.calls == 1 and run.candidate_generation_id is None


def test_target_clarification_is_separate_from_original_feedback(case, monkeypatch):
    from revision import service as revision_service
    store, evidence = case
    original = store.get_revision_session('session').state['feedbacks']
    run = store.get_agent_run('run')
    store.update_agent_run('run', expected_revision=run.state_revision, status='waiting_user',
        state={**run.state, 'authorization': {}, 'stop_reason': 'normalization_authorization'},
        candidate_generation_id=None)
    monkeypatch.setattr('revision.feedback_goals.load_feedback_evidence', lambda *args, **kwargs: evidence)
    model = Model()
    monkeypatch.setattr(revision_service, '_feedback_model', lambda *args: model)
    result = execute_revision_run(store, 'session', 'run', resume_payload={
        'clarifications': {'feedback': ['paragraph']}, 'external_processing_allowed': True,
        'allowed_llm_tasks': ['feedback_normalization']})
    assert result['status'] == 'needs_user_input' and model.calls == 1
    assert store.get_agent_run('run').state['target_selections'] == {'feedback': ['paragraph']}
    assert store.get_revision_session('session').state['feedbacks'] == original
