# -*- coding: utf-8 -*-
"""默认离线、显式授权、翻译原子性与失败传播测试。"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.support.fake_llm import FakeLLMClient
from tests.support.paths import STAGE1_DIR


##### 测试环境板块 #####


from llm.policy import (
    HEADING_TRANSLATION_TASK,
    RECOGNITION_REVIEW_TASK,
    LLMAuthorization,
    STATUS_FAILED,
    STATUS_NOT_CONFIGURED,
    STATUS_PARTIAL,
    STATUS_REJECTED,
    STATUS_SUCCEEDED,
)
from config import Settings
from adapters.ouc import OUCTemplateAdapter
from llm.client import LLMClient, LLMError
from pipeline.translate import translate_headings_for_toc


def _make_tex_project(root: Path, chapter_count: int = 1) -> tuple[Path, list[dict]]:
    contents = root / "contents"
    contents.mkdir(parents=True)
    adapter = OUCTemplateAdapter()
    blocks = []
    records = []
    for index in range(chapter_count):
        unit_id = f"u-{index:06d}"
        rendered = adapter.render_heading("chapter", f"章节{index}", unit_id)
        blocks.extend([rendered["primary"], rendered["supplementary"]])
        records.append({
            "unit_id": unit_id, "level": "chapter", "text_zh": f"章节{index}",
            "target_file": "contents/section_01.tex",
        })
    if chapter_count == 1:
        for offset, (level, title) in enumerate((
            ("section", "研究方法"), ("subsection", "数据来源")
        ), start=1):
            unit_id = f"u-{offset:06d}"
            rendered = adapter.render_heading(level, title, unit_id)
            blocks.extend([rendered["primary"], rendered["supplementary"]])
            records.append({
                "unit_id": unit_id, "level": level, "text_zh": title,
                "target_file": "contents/section_01.tex",
            })
        blocks.extend([
            "\\figurecaption{图1-1}{总体流程}{placeholder}",
            "\\tablecaption{样本信息}{placeholder}",
        ])
    path = contents / "section_01.tex"
    path.write_text("\n".join(blocks), encoding="utf-8")
    return path, records


def _translate(root: str | Path, records: list[dict], **kwargs):
    adapter = OUCTemplateAdapter()
    return translate_headings_for_toc(
        root, profile=adapter.format_contract(), template_adapter=adapter,
        heading_records=records, **kwargs,
    )


def _authorized(*, external=True) -> LLMAuthorization:
    return LLMAuthorization.for_tasks(
        [HEADING_TRANSLATION_TASK],
        external_processing_allowed=external,
        source="test",
    )


##### 授权边界板块 #####


class AuthorizationTests(unittest.TestCase):
    def test_default_offline_never_touches_client_even_if_configured(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path, records = _make_tex_project(Path(temp_dir))
            before = hashlib.sha256(path.read_bytes()).hexdigest()
            client = FakeLLMClient(configured=True)
            result = _translate(temp_dir, records, client=client)
            after = hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertEqual(result.status, "disabled")
        self.assertEqual(client.calls, 0)
        self.assertEqual(before, after)

    def test_authorized_but_not_configured_is_blocking(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _path, records = _make_tex_project(Path(temp_dir))
            client = FakeLLMClient(configured=False)
            result = _translate(
                temp_dir, records, authorization=_authorized(), client=client
            )
        self.assertEqual(result.status, "disabled")
        self.assertFalse(result.blocking)
        self.assertEqual(client.calls, 0)


class ClientGateTests(unittest.TestCase):
    def _client(self) -> LLMClient:
        return LLMClient(
            Settings(
                llm_provider="openai",
                llm_api_key="configured-test-key",
                llm_model="fake-model",
                llm_base_url="https://fake.invalid/v1",
                llm_max_retries=0,
            )
        )

    def test_client_rejects_unapproved_task_before_transport(self):
        client = self._client()
        with patch.object(client, "_complete_text") as transport:
            with self.assertRaises(LLMError) as captured:
                client.complete_text(
                    "system",
                    "sensitive thesis title",
                    authorization=LLMAuthorization.offline(),
                    task=HEADING_TRANSLATION_TASK,
                )
        self.assertEqual(captured.exception.code, "task_not_authorized")
        transport.assert_not_called()

    def test_http_error_does_not_log_response_body(self):
        client = self._client()
        response = SimpleNamespace(status_code=400, text="敏感论文标题")
        with patch("llm.client.requests.post", return_value=response):
            with self.assertRaises(LLMError) as captured:
                client.complete_text(
                    "system",
                    "sensitive thesis title",
                    authorization=_authorized(),
                    task=HEADING_TRANSLATION_TASK,
                )
        self.assertEqual(captured.exception.code, "http_error")
        self.assertNotIn("敏感论文标题", str(captured.exception))
        self.assertEqual(client.call_records[0]["error_code"], "http_error")

    def test_openai_compatible_usage_and_latency_are_recorded(self):
        client = self._client()
        response = SimpleNamespace(
            status_code=200,
            json=lambda: {
                "model": "deepseek-v4-pro",
                "choices": [{"message": {"content": "ok"}}],
                "usage": {
                    "prompt_tokens": 11, "completion_tokens": 7,
                    "total_tokens": 18, "prompt_cache_hit_tokens": 3,
                    "prompt_cache_miss_tokens": 8,
                    "completion_tokens_details": {"reasoning_tokens": 4},
                },
            },
        )
        with patch("llm.client.requests.post", return_value=response) as transport:
            text = client.complete_text(
                "system", "synthetic",
                authorization=_authorized(), task=HEADING_TRANSLATION_TASK,
            )
        self.assertEqual(text, "ok")
        self.assertEqual(client.usage_summary()["total_tokens"], 18)
        self.assertEqual(client.call_records[0]["reasoning_tokens"], 4)
        self.assertGreaterEqual(client.call_records[0]["duration_seconds"], 0)
        self.assertEqual(
            transport.call_args.args[0], "https://fake.invalid/v1/chat/completions"
        )

    def test_empty_responses_keep_usage_and_are_not_double_retried(self):
        client = LLMClient(Settings(
            llm_provider="openai", llm_api_key="configured-test-key",
            llm_model="deepseek-v4-pro",
            llm_base_url="https://api.deepseek.com", llm_max_retries=1,
        ))
        response = SimpleNamespace(
            status_code=200,
            json=lambda: {
                "model": "deepseek-v4-pro",
                "choices": [{"finish_reason": "length", "message": {"content": ""}}],
                "usage": {
                    "prompt_tokens": 20, "completion_tokens": 2048,
                    "total_tokens": 2068,
                    "completion_tokens_details": {"reasoning_tokens": 2048},
                },
            },
        )
        with patch("llm.client.requests.post", return_value=response) as transport:
            with self.assertRaises(LLMError) as captured:
                client.complete_json(
                    "system", "synthetic", authorization=_authorized(),
                    task=HEADING_TRANSLATION_TASK,
                )
        self.assertEqual(captured.exception.code, "empty_response")
        self.assertEqual(transport.call_count, 2)
        self.assertEqual(client.usage_summary()["total_tokens"], 4136)
        self.assertEqual(client.call_records[0]["finish_reason"], "length")

    def test_task_without_external_confirmation_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _path, records = _make_tex_project(Path(temp_dir))
            client = FakeLLMClient(configured=True)
            result = _translate(
                temp_dir, records,
                authorization=_authorized(external=False), client=client,
            )
        self.assertEqual(result.status, "disabled")
        self.assertFalse(result.blocking)
        self.assertEqual(client.calls, 0)

    def test_openai_native_tool_call_is_normalized(self):
        client = self._client()
        authorization = LLMAuthorization.for_tasks(
            [RECOGNITION_REVIEW_TASK], external_processing_allowed=True,
            source="test",
        )
        response = SimpleNamespace(
            status_code=200,
            json=lambda: {
                "model": "fake-model", "usage": {"total_tokens": 12},
                "choices": [{
                    "finish_reason": "tool_calls",
                    "message": {"tool_calls": [{
                        "function": {
                            "name": "locate_content_unit",
                            "arguments": '{"unit_id":"p1"}',
                        }
                    }]},
                }],
            },
        )
        tools = [{
            "name": "locate_content_unit", "description": "read",
            "input_schema": {
                "type": "object", "properties": {
                    "unit_id": {"type": "string"}
                }, "required": ["unit_id"],
            },
        }]
        with patch("llm.client.requests.post", return_value=response) as transport:
            result = client.complete_with_tools(
                "system", "synthetic", tools, authorization=authorization,
                task=RECOGNITION_REVIEW_TASK,
            )
        self.assertEqual(
            result["tool_calls"], [{
                "name": "locate_content_unit",
                "arguments": {"unit_id": "p1"},
            }],
        )
        sent = transport.call_args.kwargs["json"]
        self.assertEqual(sent["tools"][0]["type"], "function")
        self.assertEqual(client.usage_summary()["total_tokens"], 12)

    def test_tool_call_is_blocked_before_transport_without_task_authorization(self):
        client = self._client()
        with patch("llm.client.requests.post") as transport:
            with self.assertRaises(LLMError) as captured:
                client.complete_with_tools(
                    "system", "sensitive", [],
                    authorization=LLMAuthorization.offline(),
                    task=RECOGNITION_REVIEW_TASK,
                )
        self.assertEqual(captured.exception.code, "task_not_authorized")
        transport.assert_not_called()

    def test_terminal_json_is_strict_and_failed_attempt_keeps_usage_without_retry(self):
        authorization = LLMAuthorization.for_tasks([RECOGNITION_REVIEW_TASK],
            external_processing_allowed=True, source='test')
        for content in ('{"status":', '[]', 'Explanation {"status":"finish"}', '```json\n{}\n```'):
            with self.subTest(content=content):
                client = self._client()
                response = SimpleNamespace(status_code=200, json=lambda: {
                    'model': 'fake-model', 'usage': {'total_tokens': 12},
                    'choices': [{'finish_reason': 'stop', 'message': {'content': content}}]})
                with patch('llm.client.requests.post', return_value=response) as transport:
                    with self.assertRaises(LLMError):
                        client.complete_with_tools('system', 'synthetic', [],
                            authorization=authorization, task=RECOGNITION_REVIEW_TASK)
                self.assertEqual(transport.call_count, 1)
                self.assertNotIn('tools', transport.call_args.kwargs['json'])
                self.assertNotIn('tool_choice', transport.call_args.kwargs['json'])
                self.assertEqual(client.usage_summary()['total_tokens'], 12)
                self.assertFalse(client.call_records[0]['success'])

    def test_invalid_tool_envelope_keeps_provider_usage(self):
        """响应已包含用量时，结构或参数解析失败不能把实际消耗记录为零。"""
        authorization = LLMAuthorization.for_tasks([RECOGNITION_REVIEW_TASK],
            external_processing_allowed=True, source='test')
        invalid_choices = [[], [{'finish_reason': 'tool_calls', 'message': {'tool_calls': [
            {'function': {'name': 'read_source', 'arguments': '{invalid'}}]}}]]
        for choices in invalid_choices:
            with self.subTest(choices=choices):
                client = self._client()
                response = SimpleNamespace(status_code=200, json=lambda: {
                    'model': 'fake-model', 'usage': {'prompt_tokens': 8, 'completion_tokens': 4, 'total_tokens': 12},
                    'choices': choices})
                with patch('llm.client.requests.post', return_value=response) as transport:
                    with self.assertRaises(LLMError):
                        client.complete_with_tools('system', 'synthetic', [],
                            authorization=authorization, task=RECOGNITION_REVIEW_TASK)
                self.assertEqual(transport.call_count, 1)
                self.assertEqual(client.usage_summary()['total_tokens'], 12)
                self.assertEqual(client.call_records[0]['response_model'], 'fake-model')
                self.assertFalse(client.call_records[0]['success'])


##### 翻译完整性板块 #####


class TranslationIntegrityTests(unittest.TestCase):
    def test_complete_response_updates_all_fields_and_real_counts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path, records = _make_tex_project(Path(temp_dir))
            client = FakeLLMClient()
            result = _translate(
                temp_dir, records, authorization=_authorized(), client=client
            )
            content = path.read_text(encoding="utf-8")
        self.assertEqual(result.status, "disabled")
        self.assertEqual(result.requested_count, 3)
        self.assertEqual(result.completed_count, 0)
        self.assertEqual(client.calls, 0)
        self.assertNotIn("\\enchapter{EN-0}", content)
        self.assertIn("\\tablecaption{样本信息}{placeholder}", content)

    def test_invalid_response_does_not_modify_any_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path, records = _make_tex_project(Path(temp_dir))
            before = path.read_bytes()
            client = FakeLLMClient(["ID=0: only-one"])
            result = _translate(
                temp_dir, records, authorization=_authorized(), client=client
            )
            after = path.read_bytes()
        self.assertEqual(result.status, "disabled")
        self.assertFalse(result.blocking)
        self.assertEqual(before, after)

    def test_later_batch_failure_keeps_original_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path, records = _make_tex_project(Path(temp_dir), chapter_count=16)
            before = path.read_bytes()
            first = "\n".join(f"ID={index}: EN-{index}" for index in range(15))
            client = FakeLLMClient([first, RuntimeError("injected network failure")])
            result = _translate(
                temp_dir, records, authorization=_authorized(), client=client
            )
            after = path.read_bytes()
        self.assertEqual(result.status, "disabled")
        self.assertEqual(result.completed_count, 0)
        self.assertFalse(result.blocking)
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main(verbosity=2)
