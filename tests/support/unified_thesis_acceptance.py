# -*- coding: utf-8 -*-
"""同一正式确认与交付链路的合成语言准备和整篇论文验收。"""

import hashlib
import json
from pathlib import Path
import uuid

from pypdf import PdfReader

from generation.service import execute_generation
from pipeline.source_clone import clone_latex_source
from pipeline.workspace import WorkspaceManager
from pipeline_api import PipelineRunner
from persistence.store import ProjectStore
from recognition.structure import commit_structure_confirmation
from template_registry.registry import resolve_builtin_template
from generation.versions import freeze_confirmed_snapshot
from infrastructure import stage1_adapter

from tests.support.paths import REPO_ROOT
from tests.support.template_source import source_snapshot
from tests.support.unified_acceptance_cases import exercise_confirmed_feedback


##### 正式首次交付板块 #####


def exercise_initial_delivery(root, monkeypatch, client, *, source, template_id, feedback=False):
    """客户端由调用方选择；无语言需求时不调用模型、不创建空 AgentRun。"""
    monkeypatch.setenv('SCHOLAR_WORKSPACE_ROOT', str(root / 'workspace'))
    stage1_adapter._get_workspace_manager.cache_clear()
    manager = WorkspaceManager(root / 'workspace')
    workspace = manager.create_project()
    store = ProjectStore(root / 'state.db')
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    resolved = resolve_builtin_template(template_id)
    protected = source_snapshot(REPO_ROOT / resolved.manifest.source_path)
    project = store.create(project_id=workspace.project_id, docx_path=str(source), bib_path=None,
        citation_map_path=None, reference_source='word_list', template_id=template_id,
        workspace_dir=str(workspace.project_dir), source_sha256=digest)
    stage1_adapter.run_extract_and_recognize(docx_path=str(source), project_dir=str(workspace.project_dir), source_sha256=digest)
    snapshot = commit_structure_confirmation(project=project, store=store, base_revision=0,
                                              heading_overrides=[], confirmed=True)
    project = store.get(project.project_id)
    identifier = str(uuid.uuid4())
    frozen, frozen_hash = freeze_confirmed_snapshot(project, identifier, snapshot, structure_revision=1)
    task = store.create_generation(generation_id=identifier, project_id=project.project_id,
        structure_revision=1, source_sha256=digest, confirmed_snapshot_path=frozen, confirmed_snapshot_sha256=frozen_hash)
    assert not task.trusted and task.review_status == 'not_reviewable'
    assert not task.output_dir and not task.artifact_manifest.get('artifacts')
    import pipeline.generation_service as initial
    monkeypatch.setattr(initial, 'LLMClient', lambda settings: client)
    execute_generation(database_path=str(store.db_path), generation_id=identifier,
        external_processing_allowed=True, allowed_llm_tasks=['heading_translation', 'required_caption_translation'])
    parent = store.get_generation(identifier)
    assert parent.trusted and parent.review_status == 'pending', parent.detail
    assert parent.gate_snapshot['gate_statuses']['structure'] == parent.gate_snapshot['gate_statuses']['compile'] == 'passed'
    assert parent.gate_snapshot['gate_statuses']['content-fidelity'] in {'passed', 'degraded'}
    report = json.loads((Path(parent.output_dir) / 'reports/pipeline_log.json').read_text(encoding='utf-8'))
    assert report['content_fidelity']['all_passed'] is True
    runs = store.list_agent_runs(project.project_id)
    if template_id == 'ouc-graduate':
        assert len(runs) == 1 and runs[0].status == 'ready'
        assert runs[0].candidate_generation_id == identifier
        assert any(item['status'] == 'succeeded' for item in report['llm_tasks'])
    else:
        assert not runs and report['agent_run'] is None
    store.accept_generation(identifier)
    if feedback:
        trace = json.loads((Path(parent.output_dir) / 'reports/render_trace.json').read_text(encoding='utf-8'))
        target = next(item['marker_unit_id'] for item in trace['records']
                      if item['role'] == 'body_paragraph' and item['status'] == 'rendered')
        result = exercise_confirmed_feedback(store, workspace, identifier, target, client, source)
        assert result['status'] == 'ready'
    accepted = store.get_generation(store.get(project.project_id).accepted_generation_id)


    ##### 独立源码包重建板块 #####

    archive = Path(accepted.output_dir) / 'latex-source.zip'
    with manager.begin_generation(project.project_id) as rebuilt:
        clone_latex_source(archive, rebuilt.staging_dir, expected_size=archive.stat().st_size,
            expected_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
            report_path=root / 'rebuild-clone.json', parent_version_id=accepted.generation_id,
            candidate_version_id=rebuilt.generation_id)
        assert not (rebuilt.staging_dir / 'main.pdf').exists()
        compiled = PipelineRunner(resolved_template=resolved).compile(str(rebuilt.staging_dir))
        assert compiled.success and all(item['passed'] for item in compiled.gates.values()), compiled.raw_error
        pages = len(PdfReader(str(compiled.pdf_path)).pages)
        assert pages > 0
    assert source_snapshot(REPO_ROOT / resolved.manifest.source_path) == protected
    assert hashlib.sha256(source.read_bytes()).hexdigest() == digest
    evidence = {'template_id': template_id, 'source_name': source.name, 'source_sha256': digest,
        'chapter_count': len(snapshot['chapters']), 'gate_statuses': accepted.gate_snapshot['gate_statuses'],
        'source_package_rebuild': {'passed': True, 'pdf_pages': pages},
        'initial_agent_started': bool(runs), 'feedback_accepted': feedback,
        'template_source_unchanged': True, 'source_word_unchanged': True,
        'quality_status': accepted.quality_status,
        'known_content_issues': [item['code'] for item in accepted.gate_snapshot['issues']
                                 if item['check_type'] == 'content-fidelity']}
    (root / 'case-evidence.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
    stage1_adapter._get_workspace_manager.cache_clear()
    return evidence
