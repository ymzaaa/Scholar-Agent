# -*- coding: utf-8 -*-
"""统一反馈执行后只有完整终验允许的私有副本可以准备交付。"""

import json
from types import SimpleNamespace

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from agents.quality_repair.models import AgentState
from pipeline.revision_batch import run_confirmed_feedback_candidate
from pipeline.validation_runner import ValidationOutcome
from pipeline.workspace import WorkspaceManager
from tests.unit.pipeline.test_confirmed_feedback_runtime import Model


##### 私有副本与终验故障夹具板块 #####

@pytest.fixture()
def case(tmp_path, monkeypatch):
    manager = WorkspaceManager(tmp_path / 'work')
    project = manager.create_project()
    workspace = manager.begin_generation(project.project_id)
    root = workspace.staging_dir
    (root / 'chapter.tex').write_text('% SCHOLAR_UNIT_BEGIN body body_paragraph\nOriginal text.\n% SCHOLAR_UNIT_END body\n')
    (root / 'main.tex').write_text(r'\input{chapter}')
    (root / 'main.pdf').write_bytes(b'%PDF-fake-offline-test')
    (root / 'reports').mkdir()
    (root / 'reports/render_trace.json').write_text('{"records": []}')
    state = AgentState(run_id='run', project_id=project.project_id, mode='user_feedback', template_id='ouc-bachelor',
        parent_generation_id='parent', parent_source_sha256='source',
        goals=[{'goal_id': 'replace', 'kind': 'body_replace', 'confirmed': True, 'target_unit_ids': ['body'],
                'replacement': {'old_text': 'Original text.', 'new_text': 'Confirmed text.'}, 'status': 'pending'}])
    outcome = ValidationOutcome(gate_statuses={'content-fidelity': 'passed', 'structure': 'passed',
                                               'compile': 'passed', 'format': 'passed'})
    validations = []
    def factory(**kwargs):
        assert kwargs['root'] == root and kwargs['journal'].entries() == []
        assert kwargs['confirmed_goals'] == state.goals
        def validate(candidate_root):
            assert candidate_root == root
            validations.append('complete')
            return outcome
        return validate
    monkeypatch.setattr('pipeline.revision_validation.build_revision_validator', factory)
    evidence = [{'unit_id': 'body', 'role': 'body_paragraph', 'relative_path': 'chapter.tex',
                 'text': 'Original text.', 'has_semantic_objects': False}]
    return SimpleNamespace(workspace=workspace, state=state, evidence=evidence, outcome=outcome,
                           validations=validations)


def execute(case):
    return run_confirmed_feedback_candidate(workspace=case.workspace, state=case.state, evidence=case.evidence,
        snapshot={}, structure_revision=1, model=None, checkpointer=InMemorySaver(),
        parent_identity=lambda: ('parent', 'source'))


##### 终验与发布事实板块 #####

def test_waiting_feedback_keeps_only_private_staging_and_resumes_same_candidate(case):
    case.state.goals.append({'goal_id': 'format', 'kind': 'format', 'confirmed': True,
        'target_unit_ids': ['body'], 'description': 'Set paragraph spacing to 6pt.', 'status': 'pending'})
    saver = InMemorySaver()
    path = case.workspace.staging_dir / 'chapter.tex'
    original = path.read_bytes()
    model = Model(path, source='Confirmed text.')
    arguments = dict(workspace=case.workspace, evidence=case.evidence, snapshot={}, structure_revision=1,
                     model=model, checkpointer=saver, parent_identity=lambda: ('parent', 'source'))
    paused = run_confirmed_feedback_candidate(state=case.state, **arguments)
    assert paused['agent_state']['status'] == 'waiting_user' and paused['output_dir'] is None
    assert not paused['gate_snapshot']['published'] and not case.workspace.final_dir.exists()
    assert path.read_bytes() == original and model.calls == 0 and not case.validations
    assert not (case.workspace.staging_dir / 'latex-source.zip').exists()
    resumed = AgentState.from_dict(paused['agent_state'])
    resumed.authorization = {'external_processing_allowed': True,
                             'allowed_llm_tasks': ['user_feedback_patch_generation']}
    result = run_confirmed_feedback_candidate(state=resumed,
        resume=Command(resume={'continue': True}), **arguments)
    assert result['agent_state']['status'] == 'ready' and result['gate_snapshot']['published']
    assert model.calls == 1 and case.validations == ['complete']
    assert result['output_dir'] == str(case.workspace.final_dir)
    assert not case.workspace.staging_dir.exists()


def test_confirmed_feedback_delivers_only_after_full_validation(case):
    result = execute(case)
    assert case.validations == ['complete']
    assert result['agent_state']['status'] == 'ready'
    assert result['gate_snapshot']['published'] is True
    assert case.workspace.final_dir.exists() and not case.workspace.staging_dir.exists()
    assert (case.workspace.final_dir / 'latex-source.zip').is_file()
    report = json.loads((case.workspace.final_dir / 'reports/pipeline_log.json').read_text(encoding='utf-8'))
    assert report['published'] is report['user_report']['delivery']['published'] is True
    assert {item['name'] for item in result['artifact_manifest']['artifacts']} >= {
        'main.pdf', 'latex-source.zip', 'pipeline_log.json', 'render_trace.json'}


@pytest.mark.parametrize('gate', ['content-fidelity', 'structure', 'compile', 'format'])
def test_failed_final_gate_never_publishes_or_packages(case, gate):
    case.outcome.gate_statuses[gate] = 'failed'
    result = execute(case)
    assert result['output_dir'] is None and result['gate_snapshot']['published'] is False
    assert not case.workspace.final_dir.exists() and not case.workspace.staging_dir.exists()
    assert result['artifact_manifest']['artifacts']
    assert all(item['kind'] == 'report' for item in result['artifact_manifest']['artifacts'])
    report_dir = case.workspace.project.reports_dir / case.workspace.generation_id
    report = json.loads((report_dir / 'pipeline_log.json').read_text(encoding='utf-8'))
    assert report['user_report']['delivery']['blocking_count'] >= 1


@pytest.mark.parametrize('failure', ['zip', 'publish'])
def test_delivery_failure_cleans_private_staging_without_trusted_result(case, monkeypatch, failure):
    def fail(*args, **kwargs):
        raise OSError('injected delivery failure')
    target = 'pipeline.revision_batch._package_latex_source' if failure == 'zip' else 'pipeline.workspace.GenerationWorkspace.publish'
    monkeypatch.setattr(target, fail)
    with pytest.raises(OSError, match='injected delivery failure'):
        execute(case)
    assert not case.workspace.final_dir.exists() and not case.workspace.staging_dir.exists()
    report_dir = case.workspace.project.reports_dir / case.workspace.generation_id
    report = json.loads((report_dir / 'pipeline_log.json').read_text(encoding='utf-8'))
    assert report['published'] is False and report['gate_snapshot']['published'] is False
    assert report['gate_snapshot']['quality_status'] == 'internal_error'
    assert report['user_report']['delivery']['blocking_count'] >= 1
