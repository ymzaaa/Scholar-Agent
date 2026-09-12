# -*- coding: utf-8 -*-
"""首次门禁之后的具体授权说明只由已定位问题产生，不触发模型或改变交付规则。"""

import json
from types import SimpleNamespace

import pytest

from llm.policy import HEADING_TRANSLATION_TASK, LLMTaskResult
from pipeline import generation_service as service
from tests.unit.pipeline.test_initial_quality_repair import case


##### 具体任务与发送边界板块 #####


def request(case, monkeypatch, **changes):
    arguments, *_ = case
    monkeypatch.setattr(service, 'get_settings', lambda: SimpleNamespace(
        llm_configured=True, llm_provider='openai', llm_model='test-model'))
    values = {key: arguments[key] for key in ('output_dir', 'generation_id', 'rendered')}
    values.update(snapshot=arguments['outcome'].snapshot(), tasks=[], agent_state=None)
    values.update(changes)
    return service._initial_repair_request(**values)


def test_local_compile_request_contains_only_known_task_and_safe_description(case, monkeypatch):
    result = request(case, monkeypatch)
    assert result['generation_id'] == 'candidate'
    assert result['model'] == {'provider': 'OpenAI 兼容服务', 'name': 'test-model', 'configured': True}
    assert [item['task'] for item in result['tasks']] == ['initial_generation_repair']
    assert result['max_calls'] == 3
    assert '局部' in result['tasks'][0]['data_scope']
    text = json.dumps(result, ensure_ascii=False)
    assert 'Original text' not in text and str(case[0]['output_dir']) not in text
    assert not case[2] and not case[3] and not case[4]


def test_language_is_scoped_to_missing_items_without_granting_unknown_tasks(case, monkeypatch):
    result = request(case, monkeypatch, tasks=[LLMTaskResult.disabled(HEADING_TRANSLATION_TASK, 16)])
    assert [item['task'] for item in result['tasks']] == ['initial_generation_repair', HEADING_TRANSLATION_TASK]
    assert result['max_calls'] == 5 and result['tasks'][1]['item_count'] == 16


@pytest.mark.parametrize('reason', ['published', 'content', 'structure', 'unknown', 'unlocated', 'already_run', 'called'])
def test_unavailable_or_completed_work_never_offers_generic_authorization(case, monkeypatch, reason):
    args, *_ = case
    changes = {}
    if reason == 'published':
        args['outcome'].gate_statuses = dict.fromkeys(('content-fidelity', 'structure', 'compile', 'format'), 'passed')
    elif reason in {'content', 'structure'}:
        args['outcome'].gate_statuses['content-fidelity' if reason == 'content' else 'structure'] = 'failed'
    elif reason == 'unknown':
        args['outcome'].issues[0]['repairable'] = False
    elif reason == 'unlocated':
        args['outcome'].compile_result.raw_error = 'unknown.tex:2: Missing } inserted.'
    elif reason == 'already_run':
        changes['agent_state'] = {'status': 'failed'}
    else:
        changes['tasks'] = [LLMTaskResult(task=HEADING_TRANSLATION_TASK, status='failed', external_call_attempted=True)]
    assert request(case, monkeypatch, **changes) is None
