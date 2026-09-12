# -*- coding: utf-8 -*-
"""本次真实模型验收专用审计，不向正式 Agent 引入可配置调用预算。"""

import hashlib
import json
import os
from pathlib import Path
import re
import time
import uuid

from config import Settings
from llm.client import LLMClient


##### 已授权验收配置板块 #####

ENDPOINT = 'https://ws-7wlu4mv5kqqd8k64.cn-beijing.maas.aliyuncs.com/compatible-mode/v1'
MODEL = 'deepseek-v4-pro-0813'
CALL_PROFILE = {'model': MODEL, 'endpoint': ENDPOINT, 'temperature': 0,
                'max_tokens': 8192, 'timeout': 120, 'retries': 0,
                'activity_token_limit': 1_000_000}


def record_failed_case(audit_path: Path, case_name: str) -> None:
    """门禁或业务断言失败也停止验收活动；不把一次用例失败算成额外网络调用。"""
    records = json.loads(audit_path.read_text(encoding='utf-8')) if audit_path.exists() else []
    destination = audit_path.with_suffix('.failure.json')
    if destination.exists():
        destination = audit_path.with_suffix(f'.failure-{len(records)}.json')
        if destination.exists():
            return  # 同一次停止不能覆盖先前的用例失败证据。
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name('.failure-' + uuid.uuid4().hex[:12] + '.tmp')
    try:
        temporary.write_text(json.dumps({'case': case_name, 'calls_at_failure': len(records)},
                                        ensure_ascii=False), encoding='utf-8')
        temporary.replace(destination)
    except Exception as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError as cleanup_error:
            exc.add_note(f'验收停止记录清理失败：{type(cleanup_error).__name__}')
        raise


class AcceptanceClient(LLMClient):
    """串行验收共享累计 Token 上限；整个活动的限制也覆盖每个案例。"""

    def __init__(self, audit_path: Path, *, data_scope: str):
        if os.environ.get('SCHOLAR_RUN_UNIFIED_ACCEPTANCE') != '1':
            raise ValueError('需要显式启用本次统一 Agent 验收活动。')
        key = os.environ.get('DASHSCOPE_API_KEY', '').strip()
        if not key:
            raise ValueError('本次验收所需环境密钥未配置。')
        super().__init__(Settings(llm_provider='openai', llm_api_key=key, llm_base_url=ENDPOINT,
            llm_model=MODEL, llm_temperature=0, llm_timeout=120, llm_max_retries=0,
            llm_max_tokens=CALL_PROFILE['max_tokens']))
        self.audit_path = Path(audit_path)
        self.data_scope = data_scope
        self._check_sensitive(data_scope)

    def _check_sensitive(self, value):
        if (self.s.llm_api_key in value
            or re.search(r'[A-Za-z]:[\\/]|/(?:home|Users|mnt|private)/', value)):
            raise ValueError('验收请求含敏感路径或密钥，未发送。')

    def _save(self, records):
        """先关闭同目录临时文件再替换；异常不能覆盖已有调用计数。"""
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.audit_path.with_name('.calls-' + uuid.uuid4().hex[:12] + '.tmp')
        try:
            temporary.write_text(json.dumps(records, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            temporary.replace(self.audit_path)
        except Exception as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError as cleanup_error:
                exc.add_note(f'审计临时文件清理失败：{type(cleanup_error).__name__}')
            raise


    ##### 请求占用与脱敏结果板块 #####

    def _check_case_resume(self, records):
        """仅验收活动使用用户明确批准的恢复凭据；旧失败和额度不清零。"""
        failure = self.audit_path.with_suffix('.failure.json')
        later_failures = list(self.audit_path.parent.glob(self.audit_path.stem + '.failure-*.json'))
        if later_failures:
            return self._check_profile_resume(records, [failure, *later_failures])
        if not failure.exists():
            return 0
        approval_path = self.audit_path.with_suffix('.resume.json')
        if not approval_path.exists():
            raise ValueError('已有验收用例失败，必须先离线复现并取得必要的新授权。')
        from agents.quality_repair.feedback_normalizer import NORMALIZATION_PROMPT
        approval = json.loads(approval_path.read_text(encoding='utf-8'))
        failed = json.loads(failure.read_text(encoding='utf-8'))
        count = approval.get('after_call_number')
        if (approval.get('approved') is not True or type(count) is not int
            or not 0 < count <= len(records) or failed.get('calls_at_failure') != count):
            raise ValueError('恢复授权与已有失败次数不一致。')
        prefix = json.dumps(records[:count], sort_keys=True, ensure_ascii=False).encode('utf-8')
        if (approval.get('failure_sha256') != hashlib.sha256(failure.read_bytes()).hexdigest()
            or approval.get('ledger_prefix_sha256') != hashlib.sha256(prefix).hexdigest()
            or approval.get('normalization_prompt_sha256') != hashlib.sha256(NORMALIZATION_PROMPT.encode('utf-8')).hexdigest()):
            raise ValueError('恢复授权的失败记录、调用历史或提示已经变化。')
        return 0

    def _check_profile_resume(self, records, failures):
        """新批准只覆盖已核验的历史前缀，不能豁免后续失败或未知请求。"""
        from agents.quality_repair.feedback_normalizer import NORMALIZATION_PROMPT
        from agents.quality_repair.planner import SYSTEM_PROMPT
        counts = [json.loads(p.read_text(encoding='utf-8')).get('calls_at_failure') for p in failures]
        if any(type(n) is not int or not 0 < n <= len(records) for n in counts):
            raise ValueError('恢复授权的失败次数不合法。')
        count = max(counts)
        path = self.audit_path.with_suffix(f'.resume-{count}.json')
        if not path.exists():
            raise ValueError('再次出现验收用例失败，必须取得必要的新授权。')
        approval = json.loads(path.read_text(encoding='utf-8'))
        prefix = json.dumps(records[:count], sort_keys=True, ensure_ascii=False).encode('utf-8')
        if (approval.get('approved') is not True or approval.get('after_call_number') != count
            or approval.get('call_profile') != CALL_PROFILE
            or approval.get('planning_prompt_sha256') != hashlib.sha256(SYSTEM_PROMPT.encode('utf-8')).hexdigest()
            or approval.get('failure_hashes') != {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in failures}
            or approval.get('ledger_prefix_sha256') != hashlib.sha256(prefix).hexdigest()
            or approval.get('normalization_prompt_sha256') != hashlib.sha256(NORMALIZATION_PROMPT.encode('utf-8')).hexdigest()):
            raise ValueError('恢复授权的配置、失败记录、调用历史或提示已经变化。')
        if any(item.get('status') not in {'succeeded', 'failed'} for item in records[:count]):
            raise ValueError('历史调用结果未知，不能凭恢复授权清零。')
        return count

    def _audited(self, method, system, user, tools, *, authorization, task):
        self._ensure_authorized(authorization, task)
        request = json.dumps({'system': system, 'user': user, 'tools': tools, 'task': task,
            'model': MODEL, 'temperature': 0, 'max_tokens': self.s.llm_max_tokens, 'timeout': 120},
            sort_keys=True, ensure_ascii=False, allow_nan=False)
        self._check_sensitive(request)
        records = json.loads(self.audit_path.read_text(encoding='utf-8')) if self.audit_path.exists() else []
        if not isinstance(records, list):
            raise ValueError('验收审计不可读取，不能重置调用计数。')
        approved_prefix = self._check_case_resume(records)
        if any(item.get('status') != 'succeeded' for item in records[approved_prefix:]):
            raise ValueError('已有失败或结果未知的调用；停止真实调用并先离线复现。')
        # UTF-8 字节数的两倍并预留协议开销，保守占用输入和完整输出额度；不扩充正式 Agent 状态。
        reservation = len(request.encode('utf-8')) * 2 + 16384 + self.s.llm_max_tokens
        used_tokens = sum(item.get('charged_tokens', item.get('total_tokens', 0)) for item in records)
        if used_tokens + reservation > CALL_PROFILE['activity_token_limit']:
            raise ValueError('累计 Token 及下一次请求预留量将超过 100 万，未发送请求。')
        record = {'call_number': len(records) + 1, 'model': MODEL, 'task': task,
            'max_tokens': self.s.llm_max_tokens, 'token_reservation': reservation,
            'data_scope': self.data_scope, 'status': 'requested',
            'request_sha256': hashlib.sha256(request.encode('utf-8')).hexdigest(),
            'duration_seconds': 0, 'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}
        records.append(record)
        self._save(records)
        started, initial_count = time.monotonic(), len(self.call_records)
        failure = None
        try:
            if tools is None:
                result = method(system, user, authorization=authorization, task=task)
            else:
                result = method(system, user, tools, authorization=authorization, task=task)
            if result is None or result == '' or result == {}:
                raise ValueError('真实模型返回空响应。')
            if tools:
                from agents.quality_repair.planner import parse_plan
                parse_plan(result, tools)
            record['status'] = 'succeeded'
            return result
        except Exception as exc:
            failure = exc
            record.update(status='failed', error_type=type(exc).__name__)
            if getattr(exc, 'code', None):
                record['error_code'] = exc.code
            raise
        finally:
            record['duration_seconds'] = round(time.monotonic() - started, 6)
            telemetry = self.call_records[initial_count:]
            if telemetry:
                record.update({key: telemetry[-1].get(key, 0)
                               for key in ('prompt_tokens', 'completion_tokens', 'total_tokens')})
                record.update({key: telemetry[-1].get(key, '')
                               for key in ('response_model', 'finish_reason', 'error_code')})
            # 提供方缺少用量时保留请求前的保守预留，不能把未知消耗当成零。
            record['charged_tokens'] = record['total_tokens'] or reservation
            try:
                self._save(records)
            except Exception as audit_error:
                if failure is None:
                    raise
                failure.add_note(f'真实调用审计收尾失败：{type(audit_error).__name__}')

    def complete_with_tools(self, system, user, tools, *, authorization, task):
        return self._audited(super().complete_with_tools, system, user, tools,
                             authorization=authorization, task=task)

    def complete_text(self, system, user, *, authorization, task):
        return self._audited(super().complete_text, system, user, None,
                             authorization=authorization, task=task)
