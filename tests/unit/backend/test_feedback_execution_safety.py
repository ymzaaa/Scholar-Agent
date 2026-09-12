# -*- coding: utf-8 -*-
"""统一反馈后端的暂停、保护、清理和登记中断；门禁使用离线故障注入。"""

import hashlib
import json
import sqlite3
import uuid
import zipfile
from pathlib import Path

import pytest

from infrastructure import stage1_adapter
from revision.feedback import execute_confirmed_feedback
from pipeline.validation_runner import ValidationOutcome
from pipeline.workspace import WorkspaceManager
from persistence.store import ProjectStore
from tests.support.delivery import delivery_values, frozen_values


##### 有所有权的离线父版本板块 #####

@pytest.fixture()
def case(tmp_path, monkeypatch):
    monkeypatch.setenv('SCHOLAR_WORKSPACE_ROOT', str(tmp_path / 'work'))
    stage1_adapter._get_workspace_manager.cache_clear()
    project_dir = WorkspaceManager(tmp_path / 'work').create_project()
    store = ProjectStore(tmp_path / 'state.db')
    project = store.create(project_id=project_dir.project_id, docx_path='never-open.docx', bib_path=None,
        citation_map_path=None, template_id='ouc-bachelor', reference_source='word_list',
        workspace_dir=str(project_dir.project_dir), source_sha256='source', structure_revision=1)
    extraction = project_dir.reports_dir / 'extraction/content_units.json'
    extraction.parent.mkdir()
    extraction.write_text(json.dumps({'schema_version': '1.7.0', 'source_path': 'never-open.docx',
        'metadata': {'source_sha256': 'source'}, 'units': [{'unit_id': 'body', 'unit_type': 'paragraph',
            'text': 'Original text.', 'order': 0, 'payload': {'inline_tokens': [{'kind': 'text', 'text': 'Original text.'}]}}],
        'issues': [], 'paragraphs': [], 'tables': [], 'media_files': {}}), encoding='utf-8')
    parent_id = str(uuid.uuid4())
    store.create_generation(generation_id=parent_id, project_id=project.project_id, source_sha256='source',
        structure_revision=1, **frozen_values(project, parent_id))
    delivery = delivery_values(store, parent_id)
    root = Path(delivery['output_dir'])
    with zipfile.ZipFile(root / 'latex-source.zip', 'w') as archive:
        archive.writestr('main.tex', r'\input{chapter}')
        archive.writestr('chapter.tex', '% SCHOLAR_UNIT_BEGIN body body_paragraph\nOriginal text.\n% SCHOLAR_UNIT_END body\n')
    trace = root / 'reports/render_trace.json'
    trace.write_text(json.dumps({'records': [{'marker_unit_id': 'body', 'source_unit_ids': ['body'],
        'role': 'body_paragraph', 'target_file': 'chapter.tex'}]}), encoding='utf-8')
    delivery['artifact_manifest']['artifacts'].append({'kind': 'report', 'root': 'output',
        'relative_path': 'reports/render_trace.json', 'name': trace.name})
    for item in delivery['artifact_manifest']['artifacts']:
        content = (root / item['relative_path']).read_bytes()
        item.update(size=len(content), sha256=hashlib.sha256(content).hexdigest())
    store.publish_trusted_generation(parent_id, **delivery)
    store.accept_generation(parent_id)
    session = store.create_revision_session(session_id='session', project_id=project.project_id,
        base_generation_id=parent_id, state={'current_run_id': 'run', 'feedbacks': [
            {'feedback_id': 'feedback', 'text': 'Replace plain text.', 'selected_unit_ids': ['body'], 'status': 'pending'}]})
    store.update_revision_session('session', expected_revision=session.state_revision, status='running',
        state=session.state, head_generation_id=None)
    store.create_agent_run(run_id='run', project_id=project.project_id, parent_generation_id=parent_id,
        status='running', state={'session_id': 'session', 'feedback_ids': ['feedback'],
            'normalization_status': 'completed', 'goals': [{'goal_id': 'goal', 'kind': 'body_replace',
                'confirmed': True, 'status': 'pending', 'target_unit_ids': ['body'],
                'replacement': {'old_text': 'Original text.', 'new_text': 'Confirmed text.'}}]})
    outcome = ValidationOutcome(gate_statuses={'content-fidelity': 'passed', 'structure': 'passed',
                                               'compile': 'passed', 'format': 'passed'})
    def factory(**kwargs):
        def validate(root):
            (root / 'main.pdf').write_bytes(b'%PDF-offline-injected-gates')
            return outcome
        return validate
    monkeypatch.setattr('pipeline.revision_validation.build_revision_validator', factory)
    try:
        yield store, project_dir, outcome
    finally:
        stage1_adapter._get_workspace_manager.cache_clear()


##### 失败与私有恢复板块 #####


def test_discard_paused_session_removes_only_its_private_staging(case):
    """正文替换后暂停仍不可交付；用户放弃应清理私有修改并停止同一运行。"""
    from revision.service import discard_revision
    store, project, _ = case
    run = store.get_agent_run('run')
    goals = run.state['goals'] + [{'goal_id': 'format', 'kind': 'format', 'confirmed': True,
        'status': 'pending', 'target_unit_ids': ['body'], 'description': '仅调整格式。'}]
    store.update_agent_run('run', expected_revision=run.state_revision, status='running',
        state={**run.state, 'goals': goals, 'authorization': {'external_processing_allowed': True,
            'allowed_llm_tasks': ['user_feedback_patch_generation']}}, candidate_generation_id=None)
    class AskModel:
        def invoke_tools(self, **kwargs):
            return {'status': 'ask_user', 'detail': '请确认是否继续当前格式目标。'}
    paused = execute_confirmed_feedback(store, 'run', model=AskModel())
    assert paused.status == 'waiting_user' and paused.candidate_generation_id is None
    private = project.generations_dir / ('.staging-' + paused.state['private_generation_id'])
    assert 'Confirmed text.' in (private / 'chapter.tex').read_text(encoding='utf-8')
    session = store.get_revision_session('session')
    store.update_revision_session('session', expected_revision=session.state_revision,
        status='needs_user_input', state=session.state, head_generation_id=None)
    assert discard_revision(store, 'session').status == 'discarded'
    assert not private.exists()
    assert store.get_agent_run('run').status == 'stopped'
    assert len(store.list_generations(project.project_id)) == 1
    assert store.get(project.project_id).accepted_generation_id == paused.parent_generation_id
    assert discard_revision(store, 'session').status == 'discarded'


def test_completed_goal_registers_temporary_protection_with_candidate(case):
    store, project, _ = case
    result = execute_confirmed_feedback(store, 'run', model=None)
    candidate = store.get_generation(result.candidate_generation_id)
    constraints = store.list_constraints(project.project_id)
    assert len(constraints) == 1
    protection = constraints[0]
    assert protection.status == 'confirmed'
    assert protection.source_session_id == 'session' and protection.source_feedback_id == 'feedback'
    assert protection.candidate_generation_id == candidate.generation_id and protection.scope_ref == 'body'
    assert protection.verifier == {'mode': 'deterministic', 'checker': 'confirmed_action',
        'relative_path': 'chapter.tex', 'action_id': result.state['agent']['protections'][0]['action_id']}
    assert 'fragment' not in json.dumps(protection.to_snapshot())
    assert candidate.active_constraints == [] and candidate.review_status == 'pending'
    store.accept_generation(candidate.generation_id)
    assert store.get_constraint(protection.constraint_id).status == 'active'
    assert store.get_generation(candidate.generation_id).active_constraints[0]['constraint_id'] == protection.constraint_id


def test_protection_insert_failure_rolls_back_candidate_registration(case):
    store, project, _ = case
    parent = store.get(project.project_id).accepted_generation_id
    with store._connect() as connection:
        connection.execute("CREATE TRIGGER fail_protection BEFORE INSERT ON paper_constraints "
                           "BEGIN SELECT RAISE(ABORT, 'injected protection'); END")
    with pytest.raises(sqlite3.IntegrityError, match='injected protection'):
        execute_confirmed_feedback(store, 'run', model=None)
    assert len(store.list_generations(project.project_id)) == 1
    assert store.get_agent_run('run').candidate_generation_id is None
    assert store.get_revision_session('session').head_generation_id is None
    assert store.get(project.project_id).accepted_generation_id == parent
    assert not list(project.generations_dir.glob('.staging-*'))
    assert [path.name for path in project.generations_dir.iterdir()] == [parent]


def test_confirmed_replacement_changes_protection_only_when_candidate_is_accepted(case):
    from constraints.models import PaperConstraint
    store, project, _ = case
    parent = store.get(project.project_id).accepted_generation_id
    old = store.create_constraint(PaperConstraint(constraint_id='old-content', project_id=project.project_id,
        template_id='ouc-bachelor', raw_feedback='保留原正文', constraint_type='confirmed_change',
        parameters={}, description='保留原正文', scope_type='content_unit', scope_ref='body',
        source_feedback_id='previous-feedback', source_session_id='previous-session',
        candidate_generation_id=parent, status='active', verifier={'mode': 'deterministic',
            'checker': 'tex_unit_contains', 'relative_path': 'chapter.tex', 'needle': 'Original text.'}))
    store.update_generation(parent, active_constraints=[old.to_snapshot()])
    run = store.get_agent_run('run')
    run.state['goals'][0]['supersedes_constraint_ids'] = ['old-content']
    store.update_agent_run('run', expected_revision=run.state_revision, status=run.status,
                           state=run.state, candidate_generation_id=None)
    result = execute_confirmed_feedback(store, 'run', model=None)
    assert result.status == 'ready'
    candidate = store.get_generation(result.candidate_generation_id)
    temporary = store.list_constraints(project.project_id, ['confirmed'])
    assert len(temporary) == 1 and temporary[0].conflict_ids == ('old-content',)
    assert temporary[0].parameters['supersedes_constraint_ids'] == ['old-content']
    assert store.get_constraint('old-content').status == 'active'
    assert store.get(project.project_id).accepted_generation_id == parent
    store.accept_generation(candidate.generation_id)
    assert store.get_constraint('old-content').status == 'superseded'
    assert store.get_generation(candidate.generation_id).active_constraints[0]['constraint_id'] == temporary[0].constraint_id


@pytest.mark.parametrize('manual', [False, True])
def test_parent_constraints_are_enforced_and_manual_checks_remain_visible(case, manual):
    store, project, _ = case
    parent_id = store.get(project.project_id).accepted_generation_id
    constraints = [{'constraint_id': 'accepted-content', 'scope_ref': 'body',
        'description': '保留已接受的正文', 'verifier': {'mode': 'human_review'} if manual else {
            'mode': 'deterministic', 'checker': 'tex_unit_contains',
            'relative_path': 'chapter.tex', 'needle': 'Original text.'}}]
    store.update_generation(parent_id, active_constraints=constraints)
    result = execute_confirmed_feedback(store, 'run', model=None)
    assert store.get_generation(parent_id).active_constraints == constraints
    assert store.get(project.project_id).accepted_generation_id == parent_id
    if manual:
        child = store.get_generation(result.candidate_generation_id)
        assert child.trusted and child.status == 'degraded'
        assert child.quality_status == 'degraded' and child.gate_snapshot['gate_statuses']['format'] == 'degraded'
        issue = next(item for item in child.gate_snapshot['issues']
                     if item['code'] == 'inherited-constraint-manual-review')
        assert issue['severity'] == 'degrade' and issue['occurrence_count'] == 1
        assert result.state['agent']['gate_snapshots']['final']['constraint_results'][0]['passed'] is None
    else:
        assert result.status == 'failed' and result.candidate_generation_id is None
        assert result.state['agent']['stop_reason'] == 'protected_content_changed'
        assert len(store.list_generations(project.project_id)) == 1
        assert not list(project.generations_dir.glob('.staging-*'))

def test_failed_terminal_validation_does_not_register_candidate(case):
    store, project, outcome = case
    outcome.gate_statuses['content-fidelity'] = 'failed'
    before = store.get(project.project_id).accepted_generation_id
    result = execute_confirmed_feedback(store, 'run', model=None)
    assert result.status in {'failed', 'stopped'} and result.candidate_generation_id is None
    assert len(store.list_generations(project.project_id)) == 1
    assert not list(project.generations_dir.glob('.staging-*'))
    assert store.get(project.project_id).accepted_generation_id == before


@pytest.mark.parametrize('failed_table, failed_column', [
    ('generations', 'trusted'), ('agent_runs', 'candidate_generation_id'),
    ('revision_sessions', 'head_generation_id'),
])
def test_candidate_registration_failure_cleans_only_unregistered_output(case, failed_table, failed_column):
    store, project, _ = case
    parent = store.get(project.project_id).accepted_generation_id
    with store._connect() as connection:
        connection.execute(f"CREATE TRIGGER fail_delivery AFTER UPDATE OF {failed_column} ON {failed_table} "
                           "BEGIN SELECT RAISE(ABORT, 'injected delivery'); END")
    with pytest.raises(sqlite3.IntegrityError, match='injected delivery'):
        execute_confirmed_feedback(store, 'run', model=None)
    assert len(store.list_generations(project.project_id)) == 1
    assert [path.name for path in project.generations_dir.iterdir()] == [parent]
    assert store.get_agent_run('run').candidate_generation_id is None
    assert store.get_revision_session('session').head_generation_id is None


def test_resume_rejects_snapshot_outside_its_frozen_candidate(case, tmp_path):
    store, project, _ = case
    run = store.get_agent_run('run')
    goals = run.state['goals'] + [{'goal_id': 'format', 'kind': 'format', 'confirmed': True,
                                  'status': 'pending', 'target_unit_ids': ['body'], 'description': 'No indent.'}]
    store.update_agent_run('run', expected_revision=run.state_revision, status='running',
        state={**run.state, 'goals': goals}, candidate_generation_id=None)
    paused = execute_confirmed_feedback(store, 'run', model=None)
    original = Path(paused.state['confirmed_snapshot_path'])
    unrelated = tmp_path / 'unrelated.json'
    unrelated.write_bytes(original.read_bytes())
    store.update_agent_run('run', expected_revision=paused.state_revision, status='waiting_user',
        state={**paused.state, 'confirmed_snapshot_path': str(unrelated)}, candidate_generation_id=None)
    with pytest.raises(ValueError, match='冻结快照'):
        execute_confirmed_feedback(store, 'run', model=None, resume=True)
    assert unrelated.read_bytes() == original.read_bytes()
    assert len(store.list_generations(project.project_id)) == 1
