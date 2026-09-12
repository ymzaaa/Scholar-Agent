# -*- coding: utf-8 -*-
"""首次发布生命周期故障注入，门禁结果使用离线固定通过桩。"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from pipeline import generation_service as service
from pipeline.workspace import WorkspaceManager, GenerationWorkspace


##### 隔离生成夹具板块 #####


@pytest.fixture()
def case(tmp_path, monkeypatch):
    manager = WorkspaceManager(tmp_path / 'workspace')
    project = manager.create_project()
    import content_extraction.persistence as persistence
    monkeypatch.setattr(persistence, 'load_extraction', lambda *args, **kwargs: object())
    recognized = SimpleNamespace(review={'formula_review': {'status': 'passed'}}, internal={})
    monkeypatch.setattr(service, 'apply_confirmed_structure', lambda *args, **kwargs: recognized)
    monkeypatch.setattr(service, 'LLMClient', lambda *args: SimpleNamespace(configured=False))
    snapshot = SimpleNamespace(publish_allowed=True, fidelity_ok=True, structure_ok=True, compile_ok=True,
        format_publish_allowed=True, blockers=0, issues=[], gate_statuses={
            'content-fidelity': 'passed', 'structure': 'passed', 'compile': 'passed', 'format': 'passed'})
    outcome = SimpleNamespace(snapshot=lambda: snapshot, check_result=None, compile_result=None,
                              report_paths={}, fidelity_report={}, structural_checks={})
    monkeypatch.setattr(service, 'run_validation_gates', lambda *args, **kwargs: outcome)
    def render(extracted, recognized, template, output, bib):
        root = Path(output)
        (root / 'main.tex').write_text('source')
        (root / 'main.pdf').write_bytes(b'%PDF-fixture')
        return SimpleNamespace(stats={}, render_trace={})
    runner = SimpleNamespace(template_adapter=SimpleNamespace(name='fixture'), template_manifest=None, render=render)
    snapshot_path = tmp_path / 'confirmed.json'
    snapshot_path.write_text('{}')
    import uuid
    generation_id = str(uuid.uuid4())
    kwargs = dict(runner=runner, workspace_root=str(manager.root), project_id=project.project_id,
        generation_id=generation_id, bib_path=None, citation_map_path=None, reference_source='word_list',
        template_dir='unused', template_id='ouc-bachelor', source_sha256='hash', structure_revision=1,
        confirmed_snapshot_path=str(snapshot_path))
    return project, generation_id, kwargs


##### 提交与失败板块 #####


def test_initial_client_does_not_inherit_network_retries(case, monkeypatch):
    """首次任务的具体授权不包含隐藏重发，也不能改写共享模型配置。"""
    from unittest.mock import patch
    from config import Settings
    from llm.client import LLMClient, LLMError
    from llm.policy import LLMAuthorization, HEADING_TRANSLATION_TASK

    configured = Settings(llm_provider='openai', llm_api_key='synthetic-key',
        llm_base_url='https://fake.invalid/v1', llm_model='fake-model', llm_max_retries=2)
    clients = []
    def create_client(settings):
        client = LLMClient(settings)
        clients.append(client)
        return client
    monkeypatch.setattr(service, 'get_settings', lambda: configured)
    monkeypatch.setattr(service, 'LLMClient', create_client)
    service.run_confirmed_generation(**case[2])
    assert len(clients) == 1 and configured.llm_max_retries == 2
    authorization = LLMAuthorization.for_tasks([HEADING_TRANSLATION_TASK], external_processing_allowed=True)
    with patch('llm.client.requests.post', return_value=SimpleNamespace(status_code=503)) as transport:
        with pytest.raises(LLMError):
            clients[0].complete_text('system', 'synthetic heading',
                authorization=authorization, task=HEADING_TRANSLATION_TASK)
    assert transport.call_count == 1
    assert len(clients[0].call_records) == 1


def test_reports_and_archive_are_ready_before_commit(case, monkeypatch):
    project, identifier, kwargs = case
    publish = GenerationWorkspace.publish
    def checked(workspace):
        assert (workspace.staging_dir / 'latex-source.zip').is_file()
        assert (workspace.staging_dir / 'reports' / 'pipeline_log.json').is_file()
        audit = json.loads((project.reports_dir / identifier / 'pipeline_log.json').read_text(encoding='utf-8'))
        assert audit['published'] is False
        return publish(workspace)
    monkeypatch.setattr(GenerationWorkspace, 'publish', checked)
    result = service.run_confirmed_generation(**kwargs)
    audit = json.loads((Path(result['report_dir']) / 'pipeline_log.json').read_text(encoding='utf-8'))
    final = json.loads((Path(result['output_dir']) / 'reports' / 'pipeline_log.json').read_text(encoding='utf-8'))
    assert audit == final
    assert result['gate_snapshot']['published'] is audit['published'] is True
    assert all(item['sha256'] for item in result['artifact_manifest']['artifacts'])


@pytest.mark.parametrize('failure', ['package', 'publish'])
def test_delivery_failure_never_claims_publication(case, monkeypatch, failure):
    project, identifier, kwargs = case
    def fail(*args):
        raise OSError(failure)
    monkeypatch.setattr(service, '_package_latex_source', fail) if failure == 'package' else monkeypatch.setattr(GenerationWorkspace, 'publish', fail)
    with pytest.raises(OSError, match=failure):
        service.run_confirmed_generation(**kwargs)
    assert list(project.generations_dir.iterdir()) == []
    report = json.loads((project.reports_dir / identifier / 'pipeline_log.json').read_text(encoding='utf-8'))
    assert report['published'] is False and report['failure_stage'] == 'delivery'
    assert report['issues'][-1]['check_type'] == 'delivery'
    assert report['user_report']['delivery']['published'] is False
    assert not list((project.reports_dir / identifier).glob('.pipeline-*'))


def test_report_directory_conflict_cleans_staging_without_touching_existing_report(case):
    project, identifier, kwargs = case
    report_dir = project.reports_dir / identifier
    report_dir.mkdir()
    sentinel = report_dir / 'user.json'
    sentinel.write_text('keep')
    with pytest.raises(FileExistsError):
        service.run_confirmed_generation(**kwargs)
    assert sentinel.read_text() == 'keep'
    assert list(project.generations_dir.iterdir()) == []
