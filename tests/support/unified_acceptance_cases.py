# -*- coding: utf-8 -*-
"""统一验收共用正式服务入口；客户端由离线测试或本次已授权活动显式提供。"""

import hashlib
import json
from pathlib import Path

from agents.quality_repair.runtime import AuthorizedPlanningModel
from llm.policy import LLMAuthorization, RECOGNITION_REVIEW_TASK, FEEDBACK_NORMALIZATION_TASK, USER_FEEDBACK_PATCH_TASK
from pipeline.workspace import WorkspaceManager
from persistence.store import ProjectStore
from tests.support.paths import REPO_ROOT
from tests.support.fake_llm import FakeLLMClient
from infrastructure import stage1_adapter


##### 项目和授权板块 #####


def create_case_project(root, monkeypatch, *, source_name, template_id):
    monkeypatch.setenv('SCHOLAR_WORKSPACE_ROOT', str(root / 'workspace'))
    stage1_adapter._get_workspace_manager.cache_clear()
    project = WorkspaceManager(root / 'workspace').create_project()
    store = ProjectStore(root / 'state.db')
    source = REPO_ROOT / 'tests/fixtures/word' / source_name
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    record = store.create(project_id=project.project_id, docx_path=str(source), bib_path=None,
        citation_map_path=None, reference_source='word_list', template_id=template_id,
        workspace_dir=str(project.project_dir), source_sha256=digest)
    stage1_adapter.run_extract_and_recognize(docx_path=str(source), project_dir=str(project.project_dir), source_sha256=digest)
    return store, project, record


def authorized_model(client, task):
    authorization = LLMAuthorization.for_tasks([task], external_processing_allowed=True,
                                               source='unified-acceptance-explicit-authorization')
    return AuthorizedPlanningModel(client, authorization, task)


##### 结构歧义只读审查板块 #####


def exercise_recognition(root, monkeypatch, client):
    from recognition.review import prepare_recognition_review, execute_recognition_review, resume_recognition_review
    from recognition.structure import read_structure_snapshot
    store, project, record = create_case_project(root, monkeypatch,
        source_name='W03_ambiguous_structure.docx', template_id='ouc-bachelor')
    before = read_structure_snapshot(record)
    run = prepare_recognition_review(store, project.project_id)
    result = execute_recognition_review(store, run.run_id, model=authorized_model(client, RECOGNITION_REVIEW_TASK))
    assert result['status'] == 'waiting_user', result
    assert result['suggestions'] and read_structure_snapshot(record) == before
    assert not store.list_generations(project.project_id)
    # 验收者明确确认候选为正文；模型只交付建议，不能替代用户完成集中确认。
    decisions = {item['unit_id']: 'body' for item in before['headings'] if item.get('requires_review')}
    run = store.get_agent_run(run.run_id)
    completed, snapshot = resume_recognition_review(store, run.run_id,
        expected_state_revision=run.state_revision, decisions=decisions)
    assert completed.status == 'ready' and snapshot['confirmed']
    assert all(not item.get('changed_files') for item in completed.state['agent']['observations'])
    stage1_adapter._get_workspace_manager.cache_clear()
    return completed.state['agent']


##### 一次理解与用户确认修订板块 #####


def exercise_feedback(root, monkeypatch, client, *, template_id):
    from tests.e2e.test_p6_agent_closure import _register_parent
    from revision.feedback_goals import prepare_revision_run, load_feedback_evidence, normalize_feedback_goals, confirm_feedback_goals
    from revision.feedback import execute_confirmed_feedback
    from revision.service import add_feedback, accept_revision
    import pipeline.generation_service as initial

    monkeypatch.setenv('SCHOLAR_WORKSPACE_ROOT', str(root / 'workspace'))
    stage1_adapter._get_workspace_manager.cache_clear()
    project = WorkspaceManager(root / 'workspace').create_project()
    store = ProjectStore(root / 'state.db')
    source = REPO_ROOT / 'tests/fixtures/word/W01_minimal_body.docx'
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    monkeypatch.setattr(initial, 'LLMClient', lambda settings: FakeLLMClient())
    parent_id, identifier = _register_parent(store=store, project_workspace=project,
        template_id=template_id, source_sha256=digest)
    return exercise_confirmed_feedback(store, project, parent_id, identifier, client, source)


def exercise_confirmed_feedback(store, project, parent_id, identifier, client, source):
    """合成与真实来源共用同一目标确认、私有执行、授信和接受验收。"""
    from revision.feedback_goals import prepare_revision_run, load_feedback_evidence, normalize_feedback_goals, confirm_feedback_goals
    from revision.feedback import execute_confirmed_feedback
    from revision.service import add_feedback, accept_revision

    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    parent = store.get_generation(parent_id)
    parent_archive = Path(parent.output_dir) / 'latex-source.zip'
    parent_archive_hash = hashlib.sha256(parent_archive.read_bytes()).hexdigest()
    session = add_feedback(store, project_id=project.project_id,
        feedback_text='请仅取消所选正文段落的首行缩进，保留全部可见文字和相邻内容。',
        selected_unit_ids=[identifier], page_number=None)
    session, run = prepare_revision_run(store, session.session_id)
    run = store.update_agent_run(run.run_id, expected_revision=run.state_revision, status=run.status,
        state={**run.state, 'authorization': {'external_processing_allowed': True,
            'allowed_llm_tasks': [FEEDBACK_NORMALIZATION_TASK, USER_FEEDBACK_PATCH_TASK]}}, candidate_generation_id=None)
    evidence = load_feedback_evidence(store, run.run_id, expected_revision=run.state_revision)
    normalized = normalize_feedback_goals(store, run.run_id, expected_revision=run.state_revision,
        model=authorized_model(client, FEEDBACK_NORMALIZATION_TASK), evidence=evidence)
    assert normalized.status == 'waiting_user' and len(normalized.state['goal_drafts']) == 1
    assert len(store.list_generations(project.project_id)) == 1
    draft = normalized.state['goal_drafts'][0]
    assert draft['kind'] == 'format' and draft['target_unit_ids'] == [identifier]
    confirmed = confirm_feedback_goals(store, run.run_id, expected_revision=normalized.state_revision,
        decisions=[{'goal_id': draft['goal_id']}], evidence=evidence)
    assert confirmed.state['goals'][0]['confirmed'] and confirmed.candidate_generation_id is None
    # 对齐正式调度：候选登记只能来自本会话当前正在执行的批次。
    session = store.get_revision_session(session.session_id)
    store.update_revision_session(session.session_id, expected_revision=session.state_revision,
        status='running', state=session.state, head_generation_id=session.head_generation_id)
    result = execute_confirmed_feedback(store, run.run_id, model=authorized_model(client, USER_FEEDBACK_PATCH_TASK))
    assert result.status == 'ready', result.state
    candidate = store.get_generation(result.candidate_generation_id)
    assert candidate.trusted and candidate.review_status == 'pending'
    assert candidate.parent_generation_id == parent_id
    assert store.get(project.project_id).accepted_generation_id == parent_id
    assert all(candidate.gate_snapshot['gate_statuses'][key] == 'passed' for key in ('structure', 'compile'))
    assert candidate.gate_snapshot['gate_statuses']['content-fidelity'] == parent.gate_snapshot['gate_statuses']['content-fidelity']
    before_content = [item for item in parent.gate_snapshot['issues'] if item['check_type'] == 'content-fidelity']
    after_content = [item for item in candidate.gate_snapshot['issues'] if item['check_type'] == 'content-fidelity']
    assert after_content == before_content
    assert (Path(candidate.output_dir) / 'main.pdf').is_file()
    assert (Path(candidate.output_dir) / 'latex-source.zip').is_file()
    temporary = store.list_constraints(project.project_id, ['confirmed'])
    assert len(temporary) == 1
    assert accept_revision(store, session.session_id).status == 'accepted'
    assert store.get_constraint(temporary[0].constraint_id).status == 'active'
    assert Path(parent.output_dir).is_dir() and hashlib.sha256(source.read_bytes()).hexdigest() == digest
    assert hashlib.sha256(parent_archive.read_bytes()).hexdigest() == parent_archive_hash
    stage1_adapter._get_workspace_manager.cache_clear()
    return result.state['agent']
