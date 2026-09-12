# -*- coding: utf-8 -*-
"""CLI 对内容、结构和显式模型任务失败的传播测试。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from config import Settings
from llm.client import LLMClient, LLMError
from llm.policy import (
    HEADING_TRANSLATION_TASK,
    LLMAuthorization,
    LLMTaskResult,
    STATUS_FAILED,
)
import run_pipeline
from adapters.ouc import OUCTemplateAdapter
from pipeline.structural_gate import FatalChapterReachability


##### 公共参数板块 #####


def _argv(workspace: Path, *, with_llm: bool = False) -> list[str]:
    values = [
        "run_pipeline.py",
        "--docx", "dummy.docx",
        "--bib", "dummy.bib",
        "--template-id", "ouc-graduate",
        "--workspace-root", str(workspace),
    ]
    if with_llm:
        values.extend(["--llm-task", HEADING_TRANSLATION_TASK, "--allow-external-llm"])
    return values


def _report(workspace: Path) -> dict:
    path = next(workspace.glob("projects/*/reports/*/pipeline_log.json"))
    return json.loads(path.read_text(encoding="utf-8"))


##### 内容与结构失败板块 #####


def test_missing_formula_review_blocks_cli_with_explicit_not_run_report(tmp_path):
    class FakeRunner:
        def __init__(self, *args, **kwargs):
            self.template_adapter = OUCTemplateAdapter()

        def extract(self, _path):
            return SimpleNamespace(paragraphs=[], tables=[])

        def recognize(self, *_args):
            return SimpleNamespace(review={}, internal={})

        def render(self, *_args):
            raise AssertionError("预检缺失不得渲染")

    workspace = tmp_path / "workspace"
    with patch.object(sys, "argv", _argv(workspace)), patch.object(run_pipeline, "PipelineRunner", FakeRunner):
        assert run_pipeline.main() == 1
    report = _report(workspace)
    assert report["formula_preflight"] == {}
    assert report["compile"] == {"success": False, "status": "not_run"}
    assert report["published"] is False


def test_fidelity_failure_skips_structural_compile_and_publish(tmp_path: Path) -> None:
    calls = {"compile": 0}

    class FakeRunner:
        def __init__(self, *args, **kwargs):
            self.template_adapter = OUCTemplateAdapter()

        def extract(self, _path):
            return SimpleNamespace(paragraphs=[], tables=[])

        def recognize(self, _extract):
            return SimpleNamespace(review={"formula_review": {"status": "passed"}}, internal={})

        def render(self, *_args):
            return SimpleNamespace(
                stats={}, chapter_files=[], render_trace={}, fidelity_report={}
            )

        def compile(self, _output):
            calls["compile"] += 1
            raise AssertionError("内容保真失败后不应编译")

    workspace = tmp_path / "workspace"
    failed_report = {
        "all_passed": False,
        "blocking_or_unimplemented": ["image_object_coverage"],
    }
    with (
        patch.object(sys, "argv", _argv(workspace)),
        patch.object(run_pipeline, "PipelineRunner", FakeRunner),
        patch("pipeline.confirmed_structure.resolve_render_structure", side_effect=lambda _extracted, recognized: recognized),
        patch(
            "pipeline.translate.translate_headings_for_toc",
            return_value=LLMTaskResult.disabled(HEADING_TRANSLATION_TASK),
        ),
        patch(
            "pipeline.validation_runner.build_content_fidelity_report",
            return_value=failed_report,
        ),
        patch("pipeline.validation_runner.write_fidelity_artifacts", return_value={}),
        patch(
            "pipeline.validation_runner.structural_fidelity_gate",
            side_effect=AssertionError("内容保真失败后不应执行结构门禁"),
        ),
    ):
        exit_code = run_pipeline.main()

    report = _report(workspace)
    assert exit_code == 1
    assert calls["compile"] == 0
    assert report["published"] is False
    assert report["compile"]["status"] == "not_run"


def test_structural_failure_skips_compile_and_format(tmp_path: Path) -> None:
    calls = {"compile": 0, "check": 0}

    class FakeRunner:
        def __init__(self, *args, **kwargs):
            self.template_adapter = OUCTemplateAdapter()

        def extract(self, _path):
            return SimpleNamespace(
                paragraphs=[], tables=[], word_structure={"paragraphs": [], "tables": []}
            )

        def recognize(self, _extract):
            return SimpleNamespace(review={"formula_review": {"status": "passed"}}, internal={})

        def render(self, *_args):
            return SimpleNamespace(
                stats={}, chapter_files=[], render_trace={}, fidelity_report={}
            )

        def compile(self, _output):
            calls["compile"] += 1
            raise AssertionError("结构失败后不应编译")

        def check_format(self, _output):
            calls["check"] += 1
            raise AssertionError("结构失败后不应检查格式")

    workspace = tmp_path / "workspace"
    with (
        patch.object(sys, "argv", _argv(workspace)),
        patch.object(run_pipeline, "PipelineRunner", FakeRunner),
        patch("pipeline.confirmed_structure.resolve_render_structure", side_effect=lambda _extracted, recognized: recognized),
        patch(
            "pipeline.translate.translate_headings_for_toc",
            return_value=LLMTaskResult.disabled(HEADING_TRANSLATION_TASK),
        ),
        patch(
            "pipeline.validation_runner.build_content_fidelity_report",
            return_value={"all_passed": True, "blocking_or_unimplemented": []},
        ),
        patch("pipeline.validation_runner.write_fidelity_artifacts", return_value={}),
        patch(
            "pipeline.validation_runner.structural_fidelity_gate",
            side_effect=FatalChapterReachability("injected mismatch"),
        ),
    ):
        exit_code = run_pipeline.main()

    report = _report(workspace)
    staging = list(workspace.glob("projects/*/generations/.staging-*"))
    assert exit_code == 1
    assert calls == {"compile": 0, "check": 0}
    assert report["structural_gate"]["error"] == "injected mismatch"
    assert staging == []


##### 大语言模型失败板块 #####


def test_explicit_translation_failure_prevents_publish_without_hidden_retry(tmp_path: Path) -> None:
    """CLI 与网页使用同一授权边界，失败不发布，也不继承共享网络重试。"""
    configured = Settings(llm_provider='openai', llm_api_key='synthetic-key',
        llm_base_url='https://fake.invalid/v1', llm_model='fake-model', llm_max_retries=2)
    clients = []

    def create_client(settings):
        client = LLMClient(settings)
        clients.append(client)
        return client

    class FakeRunner:
        def __init__(self, *args, **kwargs):
            self.template_adapter = OUCTemplateAdapter()

        def extract(self, _path):
            return SimpleNamespace(
                paragraphs=[], tables=[], word_structure={"paragraphs": [], "tables": []}
            )

        def recognize(self, _extract):
            return SimpleNamespace(review={"formula_review": {"status": "passed"}}, internal={})

        def render(self, *_args):
            return SimpleNamespace(
                stats={}, chapter_files=[], render_trace={}, fidelity_report={}
            )

        def compile(self, output):
            return SimpleNamespace(
                success=True,
                pdf_path=str(Path(output) / "main.pdf"),
                errs=0,
                raw_error="",
                started_at_ns=1,
                finished_at_ns=2,
                process_results=[],
                gates={},
                warnings=[],
            )

        def check_format(self, _output):
            return SimpleNamespace(all_passed=True, publish_allowed=True, quality_status="passed", fails=[], degradations=[], counts={}, template_profile={}, results={})

    failed = LLMTaskResult(
        task=HEADING_TRANSLATION_TASK,
        status=STATUS_FAILED,
        requested_count=1,
        external_call_attempted=True,
        blocking=True,
        error_code="network_error",
    )
    workspace = tmp_path / "workspace"
    with (
        patch.object(sys, "argv", _argv(workspace, with_llm=True)),
        patch.object(run_pipeline, "PipelineRunner", FakeRunner),
        patch("config.get_settings", return_value=configured),
        patch("llm.client.LLMClient", side_effect=create_client),
        patch("pipeline.confirmed_structure.resolve_render_structure", side_effect=lambda _extracted, recognized: recognized),
        patch("pipeline.translate.translate_headings_for_toc", return_value=failed),
        patch(
            "pipeline.validation_runner.build_content_fidelity_report",
            return_value={"all_passed": True, "blocking_or_unimplemented": []},
        ),
        patch("pipeline.validation_runner.write_fidelity_artifacts", return_value={}),
        patch("pipeline.validation_runner.structural_fidelity_gate", return_value={}),
    ):
        exit_code = run_pipeline.main()

    report = _report(workspace)
    published = list(workspace.glob("projects/*/generations/*"))
    assert exit_code == 1
    assert report["published"] is False
    assert report["llm_tasks"][0]["status"] == STATUS_FAILED
    assert published == []
    assert len(clients) == 1 and configured.llm_max_retries == 2
    authorization = LLMAuthorization.for_tasks([HEADING_TRANSLATION_TASK], external_processing_allowed=True)
    with patch('llm.client.requests.post', return_value=SimpleNamespace(status_code=503)) as transport:
        with pytest.raises(LLMError):
            clients[0].complete_text('system', 'synthetic heading',
                authorization=authorization, task=HEADING_TRANSLATION_TASK)
    assert transport.call_count == 1
    assert len(clients[0].call_records) == 1
