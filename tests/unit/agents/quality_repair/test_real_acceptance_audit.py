# -*- coding: utf-8 -*-
"""真实验收活动的请求上限、失败停止和脱敏审计，仅使用离线网络替身。"""

import json
import hashlib
import time

import pytest

from llm.policy import LLMAuthorization, INITIAL_GENERATION_REPAIR_TASK
from tests.support.real_model_acceptance import AcceptanceClient


##### 显式活动配置板块 #####

def client(tmp_path, monkeypatch):
    monkeypatch.setenv('SCHOLAR_RUN_UNIFIED_ACCEPTANCE', '1')
    monkeypatch.setenv('DASHSCOPE_API_KEY', 'offline-secret')
    return AcceptanceClient(tmp_path / 'calls.json', data_scope='synthetic selected unit')


def invoke(instance):
    return instance.complete_with_tools('system', '{"evidence":[]}', [],
        authorization=LLMAuthorization.for_tasks([INITIAL_GENERATION_REPAIR_TASK],
            external_processing_allowed=True, source='offline-audit-test'), task=INITIAL_GENERATION_REPAIR_TASK)


def test_no_activity_flag_prevents_client_creation(tmp_path, monkeypatch):
    monkeypatch.delenv('SCHOLAR_RUN_UNIFIED_ACCEPTANCE', raising=False)
    with pytest.raises(ValueError, match='显式'):
        AcceptanceClient(tmp_path / 'calls.json', data_scope='synthetic')
    assert not (tmp_path / 'calls.json').exists()


##### 调用占用与失败停止板块 #####

def test_request_is_claimed_before_network_and_audit_has_no_content(tmp_path, monkeypatch):
    instance = client(tmp_path, monkeypatch)

    def complete(*args, **kwargs):
        records = json.loads((tmp_path / 'calls.json').read_text(encoding='utf-8'))
        assert len(records) == 1 and records[0]['status'] == 'requested'
        return {'status': 'finish', 'detail': 'done'}

    monkeypatch.setattr('llm.client.LLMClient.complete_with_tools', complete)
    assert invoke(instance)['status'] == 'finish'
    record = json.loads((tmp_path / 'calls.json').read_text(encoding='utf-8'))[0]
    assert record['status'] == 'succeeded' and len(record['request_sha256']) == 64
    assert record['model'] == 'deepseek-v4-pro-0813'
    assert 'offline-secret' not in json.dumps(record) and 'evidence' not in json.dumps(record)
    assert instance.s.llm_max_retries == 0 and instance.s.llm_timeout == 120
    assert instance.s.llm_max_tokens == 8192


def test_failure_consumes_one_call_and_blocks_later_requests(tmp_path, monkeypatch):
    instance = client(tmp_path, monkeypatch)
    calls = []

    def fail(*args, **kwargs):
        calls.append(True)
        raise TimeoutError('sensitive server path and prompt')

    monkeypatch.setattr('llm.client.LLMClient.complete_with_tools', fail)
    with pytest.raises(TimeoutError):
        invoke(instance)
    with pytest.raises(ValueError, match='失败或结果未知'):
        invoke(instance)
    records = json.loads((tmp_path / 'calls.json').read_text(encoding='utf-8'))
    assert len(records) == len(calls) == 1 and records[0]['status'] == 'failed'
    assert 'sensitive' not in json.dumps(records)


def test_token_ceiling_replaces_the_obsolete_call_count_limit(tmp_path, monkeypatch):
    instance = client(tmp_path, monkeypatch)
    monkeypatch.setattr('llm.client.LLMClient.complete_with_tools', lambda *args, **kwargs: {'status': 'finish'})
    for _ in range(28):
        invoke(instance)
    other = client(tmp_path, monkeypatch)
    assert invoke(other)['status'] == 'finish'
    records = json.loads((tmp_path / 'calls.json').read_text(encoding='utf-8'))
    assert len(records) == 29
    assert sum(record['charged_tokens'] for record in records) < 1000000


def test_empty_response_is_a_failed_call_without_retry(tmp_path, monkeypatch):
    instance = client(tmp_path, monkeypatch)
    monkeypatch.setattr('llm.client.LLMClient.complete_text', lambda *args, **kwargs: '')
    with pytest.raises(ValueError, match='空响应'):
        instance.complete_text('system', 'user', authorization=LLMAuthorization.for_tasks(
            [INITIAL_GENERATION_REPAIR_TASK], external_processing_allowed=True, source='test'),
            task=INITIAL_GENERATION_REPAIR_TASK)
    records = json.loads((tmp_path / 'calls.json').read_text(encoding='utf-8'))
    assert len(records) == 1 and records[0]['status'] == 'failed'


def test_case_validation_failure_blocks_new_calls_without_changing_call_count(tmp_path, monkeypatch):
    from tests.support.real_model_acceptance import record_failed_case

    instance = client(tmp_path, monkeypatch)
    monkeypatch.setattr('llm.client.LLMClient.complete_with_tools', lambda *args, **kwargs: {'status': 'finish'})
    invoke(instance)
    record_failed_case(instance.audit_path, 'test_synthetic_gate')
    with pytest.raises(ValueError, match='验收用例失败'):
        invoke(instance)
    assert len(json.loads(instance.audit_path.read_text(encoding='utf-8'))) == 1
    failure = json.loads(instance.audit_path.with_suffix('.failure.json').read_text(encoding='utf-8'))
    assert failure == {'case': 'test_synthetic_gate', 'calls_at_failure': 1}


def approve_case_resume(instance):
    from agents.quality_repair.feedback_normalizer import NORMALIZATION_PROMPT
    failure_path = instance.audit_path.with_suffix('.failure.json')
    records = json.loads(instance.audit_path.read_text(encoding='utf-8'))
    approval = {'approved': True, 'after_call_number': len(records),
        'failure_sha256': hashlib.sha256(failure_path.read_bytes()).hexdigest(),
        'ledger_prefix_sha256': hashlib.sha256(json.dumps(records, sort_keys=True,
            ensure_ascii=False).encode('utf-8')).hexdigest(),
        'normalization_prompt_sha256': hashlib.sha256(NORMALIZATION_PROMPT.encode('utf-8')).hexdigest()}
    instance.audit_path.with_suffix('.resume.json').write_text(json.dumps(approval), encoding='utf-8')
    return failure_path.read_bytes()


def test_explicit_resume_preserves_old_calls_and_failure_then_stops_on_new_failure(tmp_path, monkeypatch):
    from tests.support.real_model_acceptance import record_failed_case
    instance = client(tmp_path, monkeypatch)
    monkeypatch.setattr('llm.client.LLMClient.complete_with_tools', lambda *args, **kwargs: {'status': 'finish'})
    invoke(instance)
    record_failed_case(instance.audit_path, 'original_case')
    failure = approve_case_resume(instance)
    previous = json.loads(instance.audit_path.read_text(encoding='utf-8'))
    invoke(instance)
    assert json.loads(instance.audit_path.read_text(encoding='utf-8'))[:1] == previous
    record_failed_case(instance.audit_path, 'new_case')
    assert instance.audit_path.with_suffix('.failure.json').read_bytes() == failure
    with pytest.raises(ValueError, match='验收用例失败'):
        invoke(instance)
    assert len(json.loads(instance.audit_path.read_text(encoding='utf-8'))) == 2


@pytest.mark.parametrize('field', ['failure_sha256', 'ledger_prefix_sha256', 'normalization_prompt_sha256'])
def test_resume_rejects_changed_failure_ledger_or_prompt(tmp_path, monkeypatch, field):
    from tests.support.real_model_acceptance import record_failed_case
    instance = client(tmp_path, monkeypatch)
    monkeypatch.setattr('llm.client.LLMClient.complete_with_tools', lambda *args, **kwargs: {'status': 'finish'})
    invoke(instance)
    record_failed_case(instance.audit_path, 'original_case')
    approve_case_resume(instance)
    path = instance.audit_path.with_suffix('.resume.json')
    approval = json.loads(path.read_text(encoding='utf-8'))
    approval[field] = 'incorrect'
    path.write_text(json.dumps(approval), encoding='utf-8')
    with pytest.raises(ValueError, match='授权'):
        invoke(instance)
    assert len(json.loads(instance.audit_path.read_text(encoding='utf-8'))) == 1


def test_audit_write_failure_keeps_original_network_error_and_claim(tmp_path, monkeypatch):
    instance = client(tmp_path, monkeypatch)
    save, writes = instance._save, []

    def fail_save(records):
        writes.append(True)
        if len(writes) == 2:
            raise OSError('audit unavailable')
        save(records)

    def fail_network(*args, **kwargs):
        raise TimeoutError('original timeout')

    monkeypatch.setattr(instance, '_save', fail_save)
    monkeypatch.setattr('llm.client.LLMClient.complete_with_tools', fail_network)
    with pytest.raises(TimeoutError, match='original timeout') as caught:
        invoke(instance)
    assert caught.value.__notes__
    assert json.loads((tmp_path / 'calls.json').read_text(encoding='utf-8'))[0]['status'] == 'requested'


@pytest.mark.parametrize('user', ['D:\\private\\paper.tex', '/home/person/paper.tex', 'offline-secret'])
def test_private_paths_and_key_never_reach_network(tmp_path, monkeypatch, user):
    instance = client(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match='敏感'):
        instance.complete_with_tools('system', user, [],
            authorization=LLMAuthorization.for_tasks([INITIAL_GENERATION_REPAIR_TASK],
                external_processing_allowed=True, source='test'), task=INITIAL_GENERATION_REPAIR_TASK)
    assert not (tmp_path / 'calls.json').exists()


##### 调整配置后的续验与累计用量板块 #####

def approve_profile_resume(instance):
    from tests.support.real_model_acceptance import CALL_PROFILE
    from agents.quality_repair.feedback_normalizer import NORMALIZATION_PROMPT
    from agents.quality_repair.planner import SYSTEM_PROMPT
    records = json.loads(instance.audit_path.read_text(encoding='utf-8'))
    failures = sorted(instance.audit_path.parent.glob(instance.audit_path.stem + '.failure*.json'))
    approval = {'approved': True, 'after_call_number': len(records),
        'failure_hashes': {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in failures},
        'ledger_prefix_sha256': hashlib.sha256(json.dumps(records, sort_keys=True,
            ensure_ascii=False).encode('utf-8')).hexdigest(),
        'normalization_prompt_sha256': hashlib.sha256(NORMALIZATION_PROMPT.encode('utf-8')).hexdigest(),
        'call_profile': CALL_PROFILE,
        'planning_prompt_sha256': hashlib.sha256(SYSTEM_PROMPT.encode('utf-8')).hexdigest()}
    path = instance.audit_path.with_suffix(f'.resume-{len(records)}.json')
    path.write_text(json.dumps(approval), encoding='utf-8')
    return path


def test_profile_resume_preserves_failed_calls_and_blocks_the_next_failure(tmp_path, monkeypatch):
    from tests.support.real_model_acceptance import record_failed_case
    instance = client(tmp_path, monkeypatch)
    monkeypatch.setattr('llm.client.LLMClient.complete_with_tools', lambda *a, **k: {'status': 'finish'})
    invoke(instance)
    record_failed_case(instance.audit_path, 'first_failure')
    approve_case_resume(instance)
    monkeypatch.setattr('llm.client.LLMClient.complete_with_tools', lambda *a, **k: '')
    with pytest.raises(ValueError, match='空响应'):
        invoke(instance)
    record_failed_case(instance.audit_path, 'truncated_plan')
    previous = json.loads(instance.audit_path.read_text(encoding='utf-8'))
    approve_profile_resume(instance)
    monkeypatch.setattr('llm.client.LLMClient.complete_with_tools', lambda *a, **k: {'status': 'finish'})
    invoke(instance)
    assert json.loads(instance.audit_path.read_text(encoding='utf-8'))[:2] == previous
    record_failed_case(instance.audit_path, 'new_failure')
    with pytest.raises(ValueError, match='验收用例失败'):
        invoke(instance)
    assert len(json.loads(instance.audit_path.read_text(encoding='utf-8'))) == 3


@pytest.mark.parametrize('change', ['profile', 'failure', 'unknown_call', 'planning_prompt'])
def test_profile_resume_rejects_changed_evidence_or_unknown_result(tmp_path, monkeypatch, change):
    from tests.support.real_model_acceptance import record_failed_case
    instance = client(tmp_path, monkeypatch)
    monkeypatch.setattr('llm.client.LLMClient.complete_with_tools', lambda *a, **k: {'status': 'finish'})
    invoke(instance)
    record_failed_case(instance.audit_path, 'old_failure')
    records = json.loads(instance.audit_path.read_text(encoding='utf-8'))
    records.append(dict(records[0], call_number=2, status='requested' if change == 'unknown_call' else 'failed'))
    instance._save(records)
    record_failed_case(instance.audit_path, 'current_failure')
    path = approve_profile_resume(instance)
    if change == 'profile':
        approval = json.loads(path.read_text(encoding='utf-8'))
        approval['call_profile']['max_tokens'] = 2048
        path.write_text(json.dumps(approval), encoding='utf-8')
    if change == 'failure':
        instance.audit_path.with_suffix('.failure.json').write_text('{}', encoding='utf-8')
    if change == 'planning_prompt':
        monkeypatch.setattr('agents.quality_repair.planner.SYSTEM_PROMPT', 'unapproved change')
    with pytest.raises(ValueError, match='授权|结果未知'):
        invoke(instance)
    assert len(json.loads(instance.audit_path.read_text(encoding='utf-8'))) == 2


def test_token_limit_includes_prior_clients_and_reserves_the_next_output(tmp_path, monkeypatch):
    instance = client(tmp_path, monkeypatch)
    instance._save([{'call_number': 1, 'status': 'succeeded', 'total_tokens': 995000}])
    calls = []
    monkeypatch.setattr('llm.client.LLMClient.complete_with_tools', lambda *a, **k: calls.append(True))
    with pytest.raises(ValueError, match='Token'):
        invoke(client(tmp_path, monkeypatch))
    assert not calls and len(json.loads(instance.audit_path.read_text(encoding='utf-8'))) == 1


def test_audit_counts_actual_tokens_including_failed_responses(tmp_path, monkeypatch):
    instance = client(tmp_path, monkeypatch)

    def truncated(*args, **kwargs):
        instance._record_call(attempt=1, started=time.monotonic(), success=False,
            usage={'prompt_tokens': 100, 'completion_tokens': 8192, 'total_tokens': 8292},
            finish_reason='length')
        raise ValueError('truncated')

    monkeypatch.setattr('llm.client.LLMClient.complete_with_tools', truncated)
    with pytest.raises(ValueError, match='truncated'):
        invoke(instance)
    record = json.loads(instance.audit_path.read_text(encoding='utf-8'))[0]
    assert record['total_tokens'] == 8292 and record['status'] == 'failed'
    assert record['token_reservation'] >= 8192 and record['max_tokens'] == 8192
    assert record['charged_tokens'] == 8292


def test_unknown_provider_usage_keeps_reservation_in_cumulative_ceiling(tmp_path, monkeypatch):
    instance = client(tmp_path, monkeypatch)
    instance._save([{'call_number': 1, 'status': 'succeeded', 'total_tokens': 0, 'charged_tokens': 990000}])
    calls = []
    monkeypatch.setattr('llm.client.LLMClient.complete_with_tools', lambda *a, **k: calls.append(True))
    with pytest.raises(ValueError, match='Token'):
        invoke(instance)
    assert not calls
