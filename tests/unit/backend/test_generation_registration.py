# -*- coding: utf-8 -*-
"""首次生成结果的交付契约及异常终态登记。"""

import copy
import json
from pathlib import Path

import pytest

from generation import service
from persistence.store import ProjectStore
from tests.support.delivery import delivery_values, frozen_values
from agents.quality_repair.models import AgentState, RunStatus


##### 冻结输入夹具板块 #####


@pytest.fixture()
def case(tmp_path, monkeypatch):
    store = ProjectStore(tmp_path / 'state.db')
    project = store.create(project_id='p1', docx_path='paper.docx', bib_path=None, citation_map_path=None,
        template_id='ouc-bachelor', reference_source='word_list', workspace_dir=str(tmp_path / 'project'),
        source_sha256='hash', structure_revision=1, structure_confirmed=True)
    store.create_generation(generation_id='g1', project_id='p1', structure_revision=1, source_sha256='hash',
                            **frozen_values(project, 'g1'))
    values = delivery_values(store, 'g1')
    result = {'status': 'success', 'quality_status': 'passed', 'output_dir': values['output_dir'],
        'report_dir': str(tmp_path / 'project' / 'reports' / 'g1'), 'artifact_manifest': values['artifact_manifest'],
        'detail': 'published', 'gate_snapshot': {'published': True, 'gate_statuses': {
            'content-fidelity': 'passed', 'structure': 'passed', 'compile': 'passed', 'format': 'passed'}}}
    return store, result


##### 结果与故障板块 #####


@pytest.mark.parametrize('fault', ['not_dict', 'missing_field', 'empty_manifest', 'missing_pdf', 'missing_zip', 'bad_path', 'old_gates', 'unknown_status'])
def test_malformed_success_cannot_become_trusted(case, monkeypatch, fault):
    store, result = case
    result = copy.deepcopy(result)
    if fault == 'not_dict':
        result = None
    elif fault == 'missing_field':
        result.pop('detail')
    elif fault == 'empty_manifest':
        result['artifact_manifest']['artifacts'] = []
    elif fault in {'missing_pdf', 'missing_zip'}:
        kind = 'pdf' if fault == 'missing_pdf' else 'latex_source'
        result['artifact_manifest']['artifacts'] = [item for item in result['artifact_manifest']['artifacts'] if item['kind'] != kind]
    elif fault == 'bad_path':
        result['output_dir'] = str(store.db_path.parent)
    elif fault == 'unknown_status':
        result['status'] = 'mystery'
    else:
        result['gate_snapshot'] = dict.fromkeys(('published', 'fidelity_ok', 'structure_ok', 'compile_ok', 'format_publish_allowed'), True)
    monkeypatch.setattr(service, 'run_project_generation', lambda **kwargs: result)
    with pytest.raises(ValueError):
        service.execute_generation(database_path=str(store.db_path), generation_id='g1')
    generation = store.get_generation('g1')
    assert generation.status == 'failed' and generation.quality_status == 'internal_error'
    assert not generation.trusted and generation.output_dir is None
    assert all(item['kind'] == 'report' and item['sha256'] for item in generation.artifact_manifest['artifacts'])


def test_terminal_database_update_failure_does_not_leave_running(case, monkeypatch):
    store, result = case
    monkeypatch.setattr(service, 'run_project_generation', lambda **kwargs: result)
    (Path(result['output_dir']) / '.scholar-generation.json').write_text(json.dumps({
        'project_id': 'p1', 'generation_id': 'g1', 'status': 'published'}))
    def fail_success(self, identifier, **values):
        raise RuntimeError('database terminal failure')
    monkeypatch.setattr(ProjectStore, 'publish_trusted_generation', fail_success)
    with pytest.raises(RuntimeError, match='database terminal failure'):
        service.execute_generation(database_path=str(store.db_path), generation_id='g1')
    assert store.get_generation('g1').status == 'failed'
    assert Path(result['output_dir']).is_dir()
    report = json.loads((Path(result['report_dir']) / 'delivery-error.json').read_text(encoding='utf-8'))
    assert report['published'] is True and report['registered'] is False


def test_execution_uses_frozen_database_inputs_and_only_current_authorization(case, monkeypatch):
    store, result = case
    observed = {}
    def execute(**kwargs):
        observed.update(kwargs)
        return result
    monkeypatch.setattr(service, 'run_project_generation', execute)
    service.execute_generation(database_path=str(store.db_path), generation_id='g1')
    assert observed['project_id'] == 'p1' and observed['source_sha256'] == 'hash'
    assert observed['external_processing_allowed'] is False and observed['allowed_llm_tasks'] == []
    assert store.get_generation('g1').trusted
    assert store.list_agent_runs('p1') == []


def test_started_initial_agent_is_linked_before_generation_becomes_trusted(case, monkeypatch):
    store, result = case

    def execute(**kwargs):
        state = AgentState(run_id='g1', project_id='p1', mode='initial_generation',
                           template_id='ouc-bachelor', parent_source_sha256='hash')
        kwargs['on_agent_state'](state.to_dict())
        run = ProjectStore(store.db_path).get_agent_run('g1')
        generation = store.get_generation('g1')
        assert run.candidate_generation_id == 'g1' and run.status == 'running'
        assert generation.status == 'running' and not generation.trusted
        assert generation.review_status == 'not_reviewable' and not generation.artifact_manifest
        state.status = RunStatus.READY
        kwargs['on_agent_state'](state.to_dict())
        assert not store.get_generation('g1').trusted
        return result

    monkeypatch.setattr(service, 'run_project_generation', execute)
    service.execute_generation(database_path=str(store.db_path), generation_id='g1')
    assert store.get_generation('g1').trusted and store.get_agent_run('g1').status == 'ready'


def test_initial_agent_cannot_associate_another_project(case, monkeypatch):
    store, result = case

    def execute(**kwargs):
        kwargs['on_agent_state'](AgentState(run_id='g1', project_id='other', mode='initial_generation',
            template_id='ouc-bachelor', parent_source_sha256='hash').to_dict())
        return result

    monkeypatch.setattr(service, 'run_project_generation', execute)
    with pytest.raises(ValueError):
        service.execute_generation(database_path=str(store.db_path), generation_id='g1')
    assert not store.get_generation('g1').trusted and store.get_generation('g1').status == 'failed'
    assert store.list_agent_runs('p1') == []


def test_initial_execution_exception_closes_started_agent_without_trusting_generation(case, monkeypatch):
    store, _result = case

    def execute(**kwargs):
        kwargs['on_agent_state'](AgentState(run_id='g1', project_id='p1', mode='initial_generation',
            template_id='ouc-bachelor', parent_source_sha256='hash').to_dict())
        raise RuntimeError('injected stage interruption')

    monkeypatch.setattr(service, 'run_project_generation', execute)
    with pytest.raises(RuntimeError, match='injected stage interruption'):
        service.execute_generation(database_path=str(store.db_path), generation_id='g1')
    run = store.get_agent_run('g1')
    generation = store.get_generation('g1')
    assert run.status == 'failed' and run.state['agent']['status'] == 'failed'
    assert run.state['agent']['stop_reason'] == 'initial_generation_interrupted'
    assert generation.status == 'failed' and not generation.trusted
    assert generation.review_status == 'not_reviewable' and generation.output_dir is None


def test_completed_generation_is_not_executed_or_revoked_on_duplicate_delivery(case, monkeypatch):
    store, result = case
    monkeypatch.setattr(service, 'run_project_generation', lambda **kwargs: result)
    first = service.execute_generation(database_path=str(store.db_path), generation_id='g1')
    monkeypatch.setattr(service, 'run_project_generation',
                        lambda **kwargs: (_ for _ in ()).throw(AssertionError('duplicate generation')))
    assert service.execute_generation(database_path=str(store.db_path), generation_id='g1') == first
    assert store.get_generation('g1').trusted and store.get_generation('g1').review_status == 'pending'
    with pytest.raises(ValueError):
        store.fail_generation('g1')
    assert store.get_generation('g1').trusted
