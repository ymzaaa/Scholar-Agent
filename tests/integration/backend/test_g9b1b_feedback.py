# -*- coding: utf-8 -*-
"""G9-B1b 接受父版本、反馈定位、工作记忆与源码克隆集成测试。"""

from __future__ import annotations

import hashlib
import json
import uuid
import zipfile
import time
from pathlib import Path

from fastapi.testclient import TestClient

from infrastructure import stage1_adapter
from main import app
from pipeline.workspace import WorkspaceManager
from persistence.store import ProjectStore, get_store
from infrastructure.tasks import TaskManager, get_task_manager
from generation.versions import freeze_confirmed_snapshot
from tests.support.delivery import delivery_values, frozen_values


##### 可信版本夹具板块 #####


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _setup(tmp_path: Path, monkeypatch):
    workspace_root = tmp_path / "workspace"
    manager = WorkspaceManager(workspace_root)
    project_id = str(uuid.uuid4())
    workspace = manager.create_project(project_id=project_id)
    monkeypatch.setenv("SCHOLAR_WORKSPACE_ROOT", str(workspace_root))
    stage1_adapter._get_workspace_manager.cache_clear()

    store = ProjectStore(tmp_path / "state.db")
    store.create(
        project_id=project_id, docx_path="paper.docx", bib_path=None,
        citation_map_path=None, template_id="ouc-graduate", reference_source="auto",
        workspace_dir=str(workspace.project_dir), source_sha256="word-hash",
        structure_revision=1, structure_confirmed=True,
    )
    parent_id = str(uuid.uuid4())
    snapshot = {
        "project_source_sha256": "word-hash", "revision": 1,
        "confirmed": True, "headings": [], "object_bindings": [],
    }
    snapshot_path, snapshot_hash = freeze_confirmed_snapshot(
        store.get(project_id), parent_id, snapshot, structure_revision=1,
    )
    output = workspace.generations_dir / parent_id
    (output / "reports").mkdir(parents=True)
    pdf = output / "main.pdf"
    pdf.write_bytes(b"%PDF-synthetic")
    archive = output / "latex-source.zip"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("main.tex", "\\input{contents/section_01}")
        package.writestr("contents/section_01.tex", "% SCHOLAR_UNIT_BEGIN u1 body_paragraph\nSynthetic content\n% SCHOLAR_UNIT_END u1\n")
    extraction = workspace.reports_dir / 'extraction/content_units.json'
    extraction.parent.mkdir()
    extraction.write_text(json.dumps({'schema_version': '1.7.0', 'source_path': 'paper.docx',
        'metadata': {'source_sha256': 'word-hash'}, 'units': [{'unit_id': 'u1', 'unit_type': 'paragraph',
            'text': 'Synthetic content', 'order': 0, 'payload': {'inline_tokens': [{'kind': 'text', 'text': 'Synthetic content'}]}}],
        'issues': [], 'paragraphs': [], 'tables': [], 'media_files': {}}), encoding='utf-8')
    trace = output / "reports" / "render_trace.json"
    trace.write_text(json.dumps({
        "records": [{
            "record_id": "r1", "marker_unit_id": "u1",
            "source_unit_ids": ["u1"], "role": "body_paragraph",
            "target_file": "contents/section_01.tex", "source_order": 1,
        }],
    }), encoding="utf-8")
    pipeline_log = output / "reports" / "pipeline_log.json"
    pipeline_log.write_text("{}", encoding="utf-8")
    parent = store.create_generation(
        generation_id=parent_id, project_id=project_id,
        structure_revision=1, source_sha256="word-hash",
        confirmed_snapshot_path=snapshot_path,
        confirmed_snapshot_sha256=snapshot_hash,
    )
    (output / '.scholar-generation.json').write_text(json.dumps({
        'project_id': project_id, 'generation_id': parent_id, 'status': 'published',
    }), encoding='utf-8')
    store.publish_trusted_generation(
        parent_id, status="success", quality_status="passed",
        output_dir=str(output), report_dir=str(workspace.reports_dir / parent_id),
        gate_snapshot={'published': True, 'gate_statuses': {
            'content-fidelity': 'passed', 'structure': 'passed', 'compile': 'passed', 'format': 'passed'}},
        artifact_manifest={"artifacts": [
            {"kind": "report", "name": pipeline_log.name, "root": "output",
             "relative_path": "reports/pipeline_log.json", "size": pipeline_log.stat().st_size,
             "sha256": _sha(pipeline_log)},
            {"kind": "pdf", "name": pdf.name, "root": "output",
             "relative_path": pdf.name, "size": pdf.stat().st_size,
             "sha256": _sha(pdf)},
            {"kind": "latex_source", "name": archive.name, "root": "output",
             "relative_path": archive.name, "size": archive.stat().st_size,
             "sha256": _sha(archive)},
            {"kind": "report", "name": trace.name, "root": "output",
             "relative_path": "reports/render_trace.json",
             "size": trace.stat().st_size, "sha256": _sha(trace)},
        ]},
    )
    app.dependency_overrides[get_store] = lambda: store
    return TestClient(app), store, project_id, parent_id


##### 接受版本与反馈闭环板块 #####


def test_accepting_child_moves_parent_pointer_and_blocks_old_branch(
    tmp_path: Path, monkeypatch,
) -> None:
    client, store, project_id, parent_id = _setup(tmp_path, monkeypatch)
    try:
        assert client.post(f"/api/versions/{parent_id}/accept").status_code == 200
        child_id = str(uuid.uuid4())
        child = store.create_generation(
            generation_id=child_id, project_id=project_id,
            structure_revision=1, source_sha256="word-hash",
            parent_generation_id=parent_id,
            **frozen_values(store.get(project_id), child_id),
        )
        store.publish_trusted_generation(child.generation_id, **delivery_values(store, child_id))
        accepted = client.post(f"/api/versions/{child_id}/accept")
        assert accepted.status_code == 200, accepted.text
        assert store.get(project_id).accepted_generation_id == child_id
        assert store.get_generation(parent_id).review_status == "superseded"
        stale = client.post(f"/api/versions/{parent_id}/feedback-runs",
            json={"feedback_text": "调整该段间距", "selected_unit_ids": ["u1"]})
        assert stale.status_code == 404
        current = client.post(f'/api/projects/{project_id}/revision-session/feedbacks',
            json={'feedback_text': '调整该段间距', 'selected_unit_ids': ['u1']})
        assert current.status_code == 200 and current.json()['base_version_id'] == child_id
    finally:
        app.dependency_overrides.clear()
        stage1_adapter._get_workspace_manager.cache_clear()


def test_old_feedback_entry_and_raw_run_query_are_removed(tmp_path: Path, monkeypatch) -> None:
    client, store, project_id, parent_id = _setup(tmp_path, monkeypatch)
    try:
        store.create_agent_run(run_id='existing-run', project_id=project_id, parent_generation_id=parent_id,
                               status='waiting_user', state={})
        assert client.post(f'/api/versions/{parent_id}/feedback-runs',
            json={'feedback_text': '调整格式', 'selected_unit_ids': ['u1']}).status_code == 404
        assert client.get('/api/agent-runs/existing-run').status_code == 404
        assert len(store.list_generations(project_id)) == 1
    finally:
        app.dependency_overrides.clear()
        stage1_adapter._get_workspace_manager.cache_clear()


def test_ambiguous_feedback_stays_in_goal_confirmation_without_private_candidate(tmp_path: Path, monkeypatch) -> None:
    from revision import service as revision_service
    client, store, project_id, parent_id = _setup(tmp_path, monkeypatch)
    tasks = TaskManager(store.db_path, max_workers=1)
    app.dependency_overrides[get_task_manager] = lambda: tasks
    calls = []

    class Model:
        def invoke_tools(self, *, system, user, tools):
            assert tools == []
            payload = json.loads(user)
            assert payload['evidence'] == []
            calls.append(True)
            return {'goal_drafts': [{'feedback_ids': [payload['feedbacks'][0]['feedback_id']],
                'kind': 'clarification', 'target_unit_ids': [], 'description': '明确需要修改的对象。',
                'missing_information': ['请选择相关段落或图表。']}]}

    monkeypatch.setattr(revision_service, '_feedback_model', lambda *args: Model())
    try:
        assert client.post(f'/api/versions/{parent_id}/accept').status_code == 200
        created = client.post(f'/api/projects/{project_id}/revision-session/feedbacks',
                              json={'feedback_text': '这里不太对', 'page_number': 2}).json()
        response = client.post(f"/api/revision-sessions/{created['session_id']}/process",
            json={'external_processing_allowed': True, 'allowed_llm_tasks': ['feedback_normalization']})
        assert response.status_code == 202, response.text
        for _ in range(150):
            current = client.get(f'/api/projects/{project_id}/revision-session').json()
            if current['status'] not in {'queued', 'running'}:
                break
            time.sleep(0.02)
        assert current['status'] == 'needs_user_input'
        assert current['goal_drafts'][0]['kind'] == 'clarification' and current['goals'] == []
        assert current['current_version_id'] is None and calls == [True]
        assert len(store.list_generations(project_id)) == 1
        assert not list((Path(store.get(project_id).workspace_dir) / 'generations').glob('.staging-*'))
    finally:
        tasks._executor.shutdown(wait=True)
        app.dependency_overrides.clear()
        stage1_adapter._get_workspace_manager.cache_clear()


##### G9-C 修订会话板块 #####


def test_revision_session_batches_feedback_without_moving_parent(
    tmp_path: Path, monkeypatch,
) -> None:
    client, store, project_id, parent_id = _setup(tmp_path, monkeypatch)
    tasks = TaskManager(store.db_path, max_workers=1)
    app.dependency_overrides[get_task_manager] = lambda: tasks
    try:
        assert client.post(f"/api/versions/{parent_id}/accept").status_code == 200
        first = client.post(
            f"/api/projects/{project_id}/revision-session/feedbacks",
            json={"feedback_text": "该段落前间距过大", "selected_unit_ids": ["u1"]},
        )
        assert first.status_code == 200, first.text
        second = client.post(
            f"/api/projects/{project_id}/revision-session/feedbacks",
            json={"feedback_text": "这里还需要检查", "page_number": 2},
        )
        assert second.status_code == 200
        body = second.json()
        assert len(body["feedbacks"]) == 2
        assert store.get(project_id).accepted_generation_id == parent_id

        started = time.monotonic()
        queued = client.post(
            f"/api/revision-sessions/{body['session_id']}/process", json={},
        )
        assert queued.status_code == 202, queued.text
        assert time.monotonic() - started < 1.0
        for _ in range(100):
            current = client.get(f"/api/projects/{project_id}/revision-session").json()
            if current["status"] not in {"queued", "running"}:
                break
            time.sleep(0.02)
        assert current["status"] == "needs_user_input"
        assert current["current_version_id"] is None
        assert store.get(project_id).accepted_generation_id == parent_id
    finally:
        app.dependency_overrides.clear()
        stage1_adapter._get_workspace_manager.cache_clear()


def test_revision_session_resumes_same_agent_after_goal_confirmation(tmp_path: Path, monkeypatch) -> None:
    """HTTP 故障夹具使用假门禁；真实编译由双 OUC 后端执行测试独立覆盖。"""
    from revision import service as revision_service
    from pipeline.validation_runner import ValidationOutcome

    client, store, project_id, parent_id = _setup(tmp_path, monkeypatch)
    tasks = TaskManager(store.db_path, max_workers=1)
    app.dependency_overrides[get_task_manager] = lambda: tasks
    calls = []

    class Model:
        def invoke_tools(self, *, system, user, tools):
            context = json.loads(user)
            if not tools:
                calls.append('understand')
                return {'goal_drafts': [{'feedback_ids': [context['feedbacks'][0]['feedback_id']],
                    'kind': 'format', 'target_unit_ids': ['u1'], 'description': '取消首行缩进。',
                    'missing_information': []}]}
            calls.append('patch')
            source = context['evidence'][0]
            return {'tool_calls': [{'name': 'apply_protected_patch', 'arguments': {
                'unit_id': 'u1', 'expected_sha256': source['sha256'],
                'edits': [{'old_text': 'Synthetic content', 'new_text': r'\noindent Synthetic content'}]}}]}

    def validator(**kwargs):
        def validate(root):
            (root / 'main.pdf').write_bytes(b'%PDF-offline-api-fixture')
            return ValidationOutcome(gate_statuses={'content-fidelity': 'passed', 'structure': 'passed',
                                                    'compile': 'passed', 'format': 'passed'})
        return validate

    monkeypatch.setattr(revision_service, '_feedback_model', lambda *args: Model())
    monkeypatch.setattr('pipeline.revision_validation.build_revision_validator', validator)

    def wait_for_session():
        for _ in range(150):
            current = client.get(f'/api/projects/{project_id}/revision-session').json()
            if current['status'] not in {'queued', 'running'}:
                return current
            time.sleep(0.02)
        raise AssertionError('反馈任务没有收敛到等待或终态。')

    try:
        assert client.post(f'/api/versions/{parent_id}/accept').status_code == 200
        created = client.post(f'/api/projects/{project_id}/revision-session/feedbacks',
                              json={'feedback_text': '取消首行缩进。'}).json()
        endpoint = f"/api/revision-sessions/{created['session_id']}"
        assert client.post(endpoint + '/process', json={}).status_code == 202
        current = wait_for_session()
        assert current['status'] == 'needs_user_input' and current['goal_drafts'] == [] and calls == []
        run_id, revision = current['current_run_id'], current['current_run_revision']
        response = client.post(endpoint + '/resume', json={'expected_run_revision': revision,
            'clarifications': {current['feedbacks'][0]['feedback_id']: ['u1']},
            'external_processing_allowed': True, 'allowed_llm_tasks': ['feedback_normalization']})
        assert response.status_code == 202, response.text
        assert response.json()['run_id'] == run_id
        current = wait_for_session()
        assert current['status'] == 'needs_user_input' and len(current['goal_drafts']) == 1
        assert calls == ['understand'] and current['current_version_id'] is None
        reloaded = client.get(f'/api/projects/{project_id}/revision-session').json()
        assert reloaded['goal_drafts'] == current['goal_drafts'] and calls == ['understand']
        response = client.post(endpoint + '/resume', json={
            'expected_run_revision': current['current_run_revision'],
            'goal_decisions': [{'goal_id': current['goal_drafts'][0]['goal_id']}],
            'external_processing_allowed': True, 'allowed_llm_tasks': ['user_feedback_patch_generation']})
        assert response.status_code == 202, response.text
        completed = wait_for_session()
        assert completed['status'] == 'reviewable', completed
        candidate = store.get_generation(completed['current_version_id'])
        assert candidate.trusted and candidate.parent_generation_id == parent_id
        assert completed['current_run_id'] == run_id and calls == ['understand', 'patch']
        assert store.get(project_id).accepted_generation_id == parent_id
        assert (Path(store.get(project_id).workspace_dir) / 'reports/agent-checkpoints.sqlite').is_file()
        stale = client.post(endpoint + '/resume', json={'expected_run_revision': revision})
        assert stale.status_code == 409
    finally:
        tasks._executor.shutdown(wait=True)
        app.dependency_overrides.clear()
        stage1_adapter._get_workspace_manager.cache_clear()
