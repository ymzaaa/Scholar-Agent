# -*- coding: utf-8 -*-
"""反馈目标只读取当前可信父版本的有限来源，不能退回原 Word 猜测正文。"""

from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from revision.feedback_goals import load_feedback_evidence
from persistence.store import ProjectStore
from tests.support.delivery import delivery_values, frozen_values


##### 持久化来源夹具板块 #####

@pytest.fixture()
def case(tmp_path):
    store = ProjectStore(tmp_path / 'state.db')
    project = store.create(project_id='project', docx_path='never-open.docx', bib_path=None,
        citation_map_path=None, template_id='ouc-bachelor', reference_source='word_list',
        workspace_dir=str(tmp_path / 'project'), source_sha256='source')
    units = [
        {'unit_id': 'body', 'unit_type': 'paragraph', 'order': 0, 'text': '原始正文。',
         'payload': {'inline_tokens': [{'kind': 'text', 'text': '原始正文。'}]}},
        {'unit_id': 'formula-body', 'unit_type': 'paragraph', 'order': 1, 'text': '结果为x。',
         'payload': {'inline_tokens': [{'kind': 'text', 'text': '结果为'}, {'kind': 'formula', 'unit_id': 'math'}]}},
        {'unit_id': 'math', 'unit_type': 'formula', 'order': 2, 'text': 'x',
         'relations': {'parent_unit_id': 'formula-body'}},
        {'unit_id': 'private', 'unit_type': 'paragraph', 'order': 3, 'text': '不属于反馈范围的材料。'},
    ]
    extraction = Path(project.workspace_dir) / 'reports/extraction/content_units.json'
    extraction.parent.mkdir(parents=True)
    extraction.write_text(json.dumps({'schema_version': '1.7.0', 'source_path': 'never-open.docx',
        'metadata': {'source_sha256': 'source'}, 'units': units, 'issues': [],
        'paragraphs': [], 'tables': [], 'media_files': {}}), encoding='utf-8')
    trace = {'records': [
        {'marker_unit_id': key, 'source_unit_ids': [key], 'role': 'body_paragraph',
         'status': 'rendered', 'target_file': 'chapters/section_01.tex', 'source_order': index}
        for index, key in enumerate(['body', 'formula-body', 'private'])], 'body_replacements': []}
    return store, project, trace, extraction


def parent_with_trace(case, trace=None):
    store, project, original, _ = case
    store.create_generation(generation_id='parent', project_id=project.project_id,
        structure_revision=project.structure_revision, source_sha256=project.source_sha256,
        **frozen_values(project, 'parent'))
    delivery = delivery_values(store, 'parent')
    path = Path(delivery['output_dir']) / 'reports/render_trace.json'
    content = json.dumps(original if trace is None else trace).encode()
    path.write_bytes(content)
    delivery['artifact_manifest']['artifacts'].append({'kind': 'report', 'root': 'output',
        'name': path.name, 'relative_path': 'reports/render_trace.json', 'size': len(content),
        'sha256': hashlib.sha256(content).hexdigest()})
    store.publish_trusted_generation('parent', **delivery)
    store.accept_generation('parent')
    store.create_revision_session(session_id='session', project_id=project.project_id,
        base_generation_id='parent', state={'current_run_id': 'run', 'feedbacks': [{'feedback_id': 'feedback',
        'text': '调整段落格式', 'selected_unit_ids': ['body', 'formula-body']}]})
    return store.create_agent_run(run_id='run', project_id=project.project_id,
        parent_generation_id='parent', status='running',
        state={'session_id': 'session', 'feedback_ids': ['feedback']})


def load(case):
    store = case[0]
    return load_feedback_evidence(store, 'run', expected_revision=store.get_agent_run('run').state_revision)


##### 当前正文与范围板块 #####

def test_revision_context_exposes_selectable_sources_and_only_public_model_fields(case, monkeypatch):
    from types import SimpleNamespace
    from revision.feedback_goals import revision_context
    store, project, _, _ = case
    parent_with_trace(case)
    monkeypatch.setattr('config.get_settings', lambda: SimpleNamespace(llm_provider='openai',
        llm_model='test-model', llm_configured=True, llm_api_key='secret-key',
        llm_base_url='https://private.internal'))
    result = revision_context(store, project.project_id)
    by_id = {item['unit_id']: item for item in result['targets']}
    assert by_id['body']['text'] == '原始正文。' and by_id['body']['body_replace_allowed'] is True
    assert by_id['formula-body']['body_replace_allowed'] is False
    assert result['model'] == {'provider': 'OpenAI 兼容服务', 'name': 'test-model', 'configured': True}
    encoded = json.dumps(result, ensure_ascii=False)
    assert 'secret-key' not in encoded and 'private.internal' not in encoded
    assert project.workspace_dir not in encoded

def test_evidence_uses_persisted_sources_without_word_or_unselected_material(case):
    parent_with_trace(case)
    evidence = load(case)
    assert [item['unit_id'] for item in evidence] == ['body', 'formula-body']
    assert evidence[0]['text'] == '原始正文。' and evidence[0]['has_semantic_objects'] is False
    assert evidence[1]['has_semantic_objects'] is True
    assert '不属于反馈范围' not in json.dumps(evidence, ensure_ascii=False)
    assert 'never-open' not in json.dumps(evidence)


def test_second_feedback_reads_latest_confirmed_replacement(case):
    trace = deepcopy(case[2])
    trace['body_replacements'] = [
        {'goal_id': 'first', 'kind': 'body_replace', 'confirmed': True, 'target_unit_ids': ['body'],
         'replacement': {'old_text': '原始正文。', 'new_text': '首次确认的正文。'}},
        {'goal_id': 'second', 'kind': 'body_replace', 'confirmed': True, 'target_unit_ids': ['body'],
         'replacement': {'old_text': '首次确认的正文。', 'new_text': '当前已接受正文。'}},
    ]
    parent_with_trace(case, trace)
    before = case[3].read_bytes()
    assert load(case)[0]['text'] == '当前已接受正文。'
    assert case[3].read_bytes() == before


@pytest.mark.parametrize('fault', ['old_text', 'unconfirmed', 'semantic_object', 'duplicate_goal'])
def test_invalid_replacement_history_never_becomes_confirmation_evidence(case, fault):
    trace = deepcopy(case[2])
    goal = {'goal_id': 'replacement', 'kind': 'body_replace', 'confirmed': True, 'target_unit_ids': ['body'],
            'replacement': {'old_text': '原始正文。', 'new_text': '新的正文。'}}
    if fault == 'old_text':
        goal['replacement']['old_text'] = '不匹配的原文'
    elif fault == 'unconfirmed':
        goal['confirmed'] = False
    elif fault == 'semantic_object':
        goal['target_unit_ids'] = ['formula-body']
        goal['replacement']['old_text'] = '结果为x。'
    trace['body_replacements'] = [goal, deepcopy(goal)] if fault == 'duplicate_goal' else [goal]
    parent_with_trace(case, trace)
    with pytest.raises(ValueError, match='正文|替换'):
        load(case)


##### 可信来源异常板块 #####

@pytest.mark.parametrize('fault', ['duplicate_marker', 'missing_unit', 'trace_hash', 'source_hash'])
def test_missing_or_ambiguous_sources_block_before_model(case, fault):
    trace = deepcopy(case[2])
    if fault == 'duplicate_marker':
        trace['records'].append(deepcopy(trace['records'][0]))
    elif fault == 'missing_unit':
        trace['records'][0]['marker_unit_id'] = 'unknown'
    parent_with_trace(case, trace)
    if fault == 'trace_hash':
        parent = case[0].get_generation('parent')
        (Path(parent.output_dir) / 'reports/render_trace.json').write_text('{}', encoding='utf-8')
    elif fault == 'source_hash':
        data = json.loads(case[3].read_text(encoding='utf-8'))
        data['metadata']['source_sha256'] = 'another-source'
        case[3].write_text(json.dumps(data), encoding='utf-8')
    with pytest.raises((ValueError, RuntimeError)):
        load(case)
