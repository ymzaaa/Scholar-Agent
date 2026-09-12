# -*- coding: utf-8 -*-
"""真实识别快照经统一图暂停、跨实例恢复后提交结构。"""

from __future__ import annotations

import hashlib
from pathlib import Path
import time
from unittest.mock import patch

import pytest
from pypdf import PdfReader

from recognition.review import (
    execute_recognition_review, prepare_recognition_review,
    resume_recognition_review,
)
from infrastructure.stage1_adapter import run_extract_and_recognize
from persistence.store import ProjectStore
from tests.support.paths import REPO_ROOT
from tests.integration.backend.test_g8a_workflow import _client, _w03_bytes
from tests.support.fake_llm import FakeLLMClient
from pipeline.minimal_registered_generation import run_minimal_registered_generation
from template_registry.registry import resolve_template
from template_registry.registry import load_builtin_template_registry
from pipeline_api import PipelineRunner


class W03ToolCallingModel:
    def __init__(self) -> None:
        self.calls = 0

    def invoke_tools(self, *, system, user, tools):
        self.calls += 1
        if self.calls == 1:
            return {
                "tool_calls": [{
                    "name": "locate_content_unit",
                    "arguments": {"unit_id": self.unit_ids[0]},
                }]
            }
        return {
            "status": "finish",
            "suggestions": [
                {
                    "unit_id": unit_id, "suggested_level": "section",
                    "confidence": 0.86, "rationale": "受控证据符合节标题特征。",
                    "evidence": ["bounded_heading_evidence"],
                }
                for unit_id in self.unit_ids
            ]
        }


@pytest.mark.parametrize("with_citation", [False, True])
def test_w03_persistent_interrupt_resume_commits_confirmed_snapshot(
    tmp_path: Path, with_citation: bool,
) -> None:
    workspace = tmp_path / "project"
    (workspace / "reports").mkdir(parents=True)
    source = REPO_ROOT / "tests/fixtures/word/W03_ambiguous_structure.docx"
    if with_citation:
        from docx import Document
        document = Document(source)
        document.add_paragraph("符号[1]位于此处")
        source = tmp_path / "w03-citation.docx"
        document.save(source)
    store = ProjectStore(tmp_path / "backend.sqlite")
    project = store.create(
        project_id="p4-w03", docx_path=str(source), bib_path=None,
        citation_map_path=None, template_id="ouc-graduate", reference_source="auto",
        workspace_dir=str(workspace),
        source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
    )
    run_extract_and_recognize(
        docx_path=project.docx_path, project_dir=project.workspace_dir,
        source_sha256=project.source_sha256,
    )
    run = prepare_recognition_review(store, project.project_id)
    model = W03ToolCallingModel()
    snapshot = __import__("json").loads(
        (workspace / "reports" / "recognition.json").read_text(encoding="utf-8")
    )
    model.unit_ids = [
        item["unit_id"] for item in snapshot["headings"]
        if item.get("requires_review")
    ]

    waiting = execute_recognition_review(store, run.run_id, model=model)
    assert waiting["status"] == "waiting_user"
    assert waiting["model_status"] == "succeeded"
    assert [item["unit_id"] for item in waiting["suggestions"]] == model.unit_ids
    assert (workspace / "reports" / "agent-checkpoints.sqlite").is_file()

    # 服务函数重新构图并从磁盘 checkpoint 恢复，不依赖首轮模型实例。
    persisted = store.get_agent_run(run.run_id)
    assert persisted is not None
    decisions = {unit_id: "section" for unit_id in model.unit_ids}
    citation_overrides = []
    if with_citation:
        with pytest.raises(ValueError, match="引用候选未确认"):
            resume_recognition_review(
                store, run.run_id, expected_state_revision=persisted.state_revision,
                decisions=decisions,
            )
        citation_overrides = [
            {"unit_id": item["unit_id"], "decision": "non_citation"}
            for item in snapshot["citation_review"]["decisions"]
            if item["decision"] == "needs_review"
        ]
    completed, confirmed = resume_recognition_review(
        store, run.run_id, expected_state_revision=persisted.state_revision,
        decisions=decisions,
        citation_overrides=citation_overrides,
    )
    assert completed.status == "ready"
    assert confirmed["confirmed"] is True
    assert confirmed["revision"] == 1
    assert confirmed["citation_review"]["counts"]["needs_review"] == 0
    resolved = {
        item["unit_id"]: item for item in confirmed["headings"]
        if item.get("requires_review")
    }
    assert all(item["level"] == "section" for item in resolved.values())
    assert all(item["review_status"] == "resolved" for item in resolved.values())


def test_http_model_unavailable_keeps_manual_confirmation_without_fake_suggestions(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    client, _store, _tasks = _client(tmp_path)
    try:
        created = client.post(
            "/api/projects",
            files={"docx": (
                "w03.docx", _w03_bytes(),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )},
            data={
                "template_id": "ouc-graduate", "reference_source": "auto",
                "recognition_review_allowed": "true",
            },
        )
        project_id = created.json()["project_id"]
        task_id = client.post(f"/api/projects/{project_id}/extract").json()["task_id"]
        for _ in range(100):
            task = client.get(f"/api/tasks/{task_id}").json()
            if task["status"] in {"done", "failed"}:
                break
            time.sleep(0.05)
        assert task["status"] == "done", task

        with patch("recognition.review._configured_model", return_value=None):
            started = client.post(f"/api/projects/{project_id}/recognition-review")
            assert started.status_code == 202, started.text
            run_id = started.json()["run_id"]
            for _ in range(100):
                review = client.get(f"/api/recognition-reviews/{run_id}").json()
                if review["status"] in {"waiting_user", "ready", "failed"}:
                    break
                time.sleep(0.05)
        assert review["status"] == "waiting_user", review
        assert review["model_status"] == "not_configured"
        assert review["suggestions"] == []
        structure = client.get(f"/api/projects/{project_id}/structure").json()

        resumed = client.post(
            f"/api/recognition-reviews/{run_id}/resume",
            json={
                "expected_state_revision": review["state_revision"],
                "decisions": {
                    item["unit_id"]: "section" for item in structure["headings"] if item.get("requires_review")
                },
            },
        )
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["status"] == "ready"
        assert resumed.json()["structure"]["confirmed"] is True
    finally:
        client.close()


@pytest.mark.parametrize("template_id", ["ouc-graduate", "ouc-bachelor"])
def test_w03_review_decisions_publish_both_fixed_templates(
    tmp_path: Path, template_id: str,
) -> None:
    runner = PipelineRunner()
    source = REPO_ROOT / "tests/fixtures/word/W03_ambiguous_structure.docx"
    recognized = runner.recognize(runner.extract(str(source)))
    decisions = {
        item["unit_id"]: "section"
        for item in recognized.review["heading_candidates"]
        if item.get("requires_review")
    }
    registry = load_builtin_template_registry(REPO_ROOT)
    output = tmp_path / template_id
    report = run_minimal_registered_generation(
        runner=runner,
        resolved_template=resolve_template(registry, template_id),
        repository_root=REPO_ROOT, docx_path=source,
        output_directory=output, case_id="P4-W03",
        translation_client=FakeLLMClient(), heading_decisions=decisions,
        report_filename="p4-report.json",
    )
    assert report["published"] is True
    assert report["gates"]["logical_structure"] is True
    assert report["gates"]["content_fidelity"] is True
    assert len(PdfReader(str(output / "main.pdf")).pages) > 0
