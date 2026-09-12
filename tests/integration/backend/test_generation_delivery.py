# -*- coding: utf-8 -*-
"""产物安全下载与首次生成创建中断。"""

import json
from pathlib import Path

import pytest

from tests.integration.backend.test_g9a_version_api import context, trusted_version


##### 产物访问板块 #####


@pytest.mark.parametrize('fault', ['untrusted', 'failed', 'unknown_root', 'escape', 'duplicate', 'missing', 'size'])
def test_invalid_pdf_cannot_be_downloaded(context, fault):
    client, store, project = context
    generation = trusted_version(store, project)
    manifest = generation.artifact_manifest
    pdf = next(item for item in manifest['artifacts'] if item['kind'] == 'pdf')
    if fault in {'untrusted', 'failed'}:
        store.update_generation(generation.generation_id, trusted=False, review_status='not_reviewable',
                                status='failed' if fault == 'failed' else 'success')
    else:
        if fault == 'unknown_root':
            pdf['root'] = 'unknown'
        elif fault == 'escape':
            pdf['relative_path'] = '../../escape.pdf'
        elif fault == 'duplicate':
            manifest['artifacts'].append(dict(pdf))
        elif fault == 'missing':
            (Path(generation.output_dir) / 'main.pdf').unlink()
        elif fault == 'size':
            pdf['size'] += 1
        # 模拟磁盘损坏或旧数据库记录；不能通过正式写入接口绕开可信校验。
        with store._connect() as connection:
            connection.execute('UPDATE generations SET artifact_manifest_json = ? WHERE generation_id = ?',
                               (json.dumps(manifest), generation.generation_id))
    assert client.get(f'/api/generations/{generation.generation_id}/pdf').status_code == 404
    if fault in {'untrusted', 'failed'}:
        assert client.get(f'/api/generations/{generation.generation_id}/latex-source').status_code == 404
        assert client.get(f'/api/generations/{generation.generation_id}/reports/pipeline_log.json').status_code == 200


def test_pdf_is_inline_and_download_does_not_rehash(context, monkeypatch):
    client, store, project = context
    generation = trusted_version(store, project)
    import hashlib
    monkeypatch.setattr(hashlib, 'sha256', lambda *args: (_ for _ in ()).throw(AssertionError('download rehash')))
    response = client.get(f'/api/generations/{generation.generation_id}/pdf')
    assert response.status_code == 200
    assert response.headers['content-disposition'].startswith('inline;')


##### 生成创建中断板块 #####

@pytest.mark.parametrize('fault', ['foreign_report_root', 'malformed_item', 'missing_hash'])
def test_diagnostic_download_checks_ownership_and_registered_metadata(context, tmp_path, fault):
    client, store, project = context
    generation = trusted_version(store, project)
    manifest = generation.artifact_manifest
    report = next(item for item in manifest['artifacts'] if item['kind'] == 'report')
    report_dir = generation.report_dir
    if fault == 'foreign_report_root':
        report_dir = str(tmp_path / 'another-project')
        Path(report_dir).mkdir()
        (Path(report_dir) / report['name']).write_bytes(b'{}')
        report.update(root='report', relative_path=report['name'])
    elif fault == 'malformed_item':
        manifest['artifacts'].append(None)
    else:
        report.pop('sha256')
    with store._connect() as connection:
        connection.execute('UPDATE generations SET trusted = 0, status = ?, review_status = ?, report_dir = ?, '
                           'artifact_manifest_json = ? WHERE generation_id = ?',
                           ('failed', 'not_reviewable', report_dir, json.dumps(manifest), generation.generation_id))
    response = client.get(f'/api/generations/{generation.generation_id}/reports/pipeline_log.json')
    assert response.status_code == 404


def test_preregistered_identifier_survives_polling_without_becoming_a_version(context, monkeypatch):
    client, store, project = context
    from infrastructure.tasks import get_task_manager
    from main import app
    tasks = app.dependency_overrides[get_task_manager]()
    queued_calls = []
    monkeypatch.setattr(tasks._executor, 'submit', lambda *args: queued_calls.append(args))
    latest = Path(project.workspace_dir) / 'reports' / 'confirmed_structure.json'
    latest.parent.mkdir(parents=True, exist_ok=True)
    latest.write_text(json.dumps({'confirmed': True, 'revision': 1, 'project_source_sha256': 'hash'}))
    response = client.post('/api/projects/p1/generate', json={'structure_revision': 1})
    assert response.status_code == 200, response.text
    identifiers = response.json()
    assert set(identifiers) == {'generation_id', 'task_id', 'status'}
    assert identifiers['status'] == 'queued'
    identifier = identifiers['generation_id']
    for status in ('queued', 'running'):
        store.update_generation(identifier, status=status)
        record = client.get(f'/api/generations/{identifier}').json()
        assert record['task_id'] == identifiers['task_id'] and record['status'] == status
        assert not record['trusted'] and record['review_status'] == 'not_reviewable' and not record['artifacts']
        assert client.get(f'/api/generations/{identifier}/pdf').status_code == 404
        assert client.get(f'/api/generations/{identifier}/latex-source').status_code == 404
        assert client.post(f'/api/versions/{identifier}/accept').status_code == 409
    assert len(queued_calls) == 1 and not store.list_agent_runs('p1')
    store.fail_generation(identifier)
    assert client.get(f'/api/generations/{identifier}').json()['status'] == 'failed'
    assert client.get('/api/projects/p1/versions').json()['versions'] == []
    assert len(store.list_generations('p1')) == 1


@pytest.mark.parametrize('fault', ['corrupt', 'freeze', 'store', 'submit'])
def test_generation_creation_failure_converges_without_orphan_snapshot(context, monkeypatch, fault):
    client, store, project = context
    latest = Path(project.workspace_dir) / 'reports' / 'confirmed_structure.json'
    latest.parent.mkdir(parents=True, exist_ok=True)
    latest.write_text('broken' if fault == 'corrupt' else json.dumps({
        'confirmed': True, 'revision': 1, 'project_source_sha256': 'hash'}))
    if fault == 'freeze':
        monkeypatch.setattr('generation.versions._write_json',
            lambda *args: (_ for _ in ()).throw(OSError('freeze write interrupted')))
    elif fault == 'store':
        monkeypatch.setattr(store, 'create_generation', lambda **kwargs: (_ for _ in ()).throw(RuntimeError('database failure')))
    elif fault == 'submit':
        monkeypatch.setattr('api.routes.generations.submit_generation_version', lambda **kwargs: (_ for _ in ()).throw(RuntimeError('submit failure')))
    response = client.post('/api/projects/p1/generate', json={'structure_revision': 1})
    assert response.status_code == (409 if fault == 'corrupt' else 500)
    records = store.list_generations('p1')
    frozen = list((latest.parent / 'version-inputs').glob('*/confirmed_structure.json'))
    if fault == 'submit':
        assert len(records) == len(frozen) == 1
        assert records[0].status == 'failed' and records[0].quality_status == 'internal_error'
    else:
        assert not records and not frozen
