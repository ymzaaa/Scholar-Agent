# -*- coding: utf-8 -*-
"""P3/W03：集中确认后在两个固定模板中保持标题和无题注表格一致。"""

from __future__ import annotations

from pathlib import Path

import pytest
from pypdf import PdfReader

from pipeline.minimal_registered_generation import run_minimal_registered_generation
from template_registry.registry import resolve_template
from template_registry.registry import load_builtin_template_registry
from pipeline_api import PipelineRunner
from tests.support.fake_llm import FakeLLMClient
from tests.support.paths import REPO_ROOT
from tests.support.template_source import source_snapshot


##### 确认夹具板块 #####


def _decisions() -> dict[str, str]:
    runner = PipelineRunner()
    extracted = runner.extract(
        str(REPO_ROOT / "tests/fixtures/word/W03_ambiguous_structure.docx")
    )
    recognized = runner.recognize(extracted)
    return {
        item["unit_id"]: "section"
        for item in recognized.review["heading_candidates"]
        if item["requires_review"]
    }


##### 节与正文确认板块 #####


@pytest.mark.parametrize("template_id", ["ouc-graduate", "ouc-bachelor"])
def test_w03_confirmed_headings_and_uncaptioned_table_render_in_word_order(
    tmp_path: Path, template_id: str
) -> None:
    registry = load_builtin_template_registry(REPO_ROOT)
    resolved = resolve_template(registry, template_id)
    source = registry.source_root(resolved.manifest)
    before = source_snapshot(source)
    output = tmp_path / template_id
    report = run_minimal_registered_generation(
        runner=PipelineRunner(), resolved_template=resolved,
        repository_root=REPO_ROOT,
        docx_path=REPO_ROOT / "tests/fixtures/word/W03_ambiguous_structure.docx",
        output_directory=output, case_id="W03",
        translation_client=FakeLLMClient(), report_filename="p3-report.json",
        heading_decisions=_decisions(),
    )

    content_dir = resolved.adapter.content_directory
    tex = (output / content_dir / "section_01.tex").read_text(encoding="utf-8")
    assert tex.count("section_heading") == 3
    assert tex.index("研究方法") < tex.index("温度") < tex.index("实验结果")
    assert r"\tablecaption" not in tex
    assert r"\label{tab" not in tex
    assert report["content"]["role_counts"]["content_table"] == 1
    assert report["gates"]["logical_structure"] is True
    assert report["gates"]["structural_counts"]["section_count"] == (3, 3)
    assert report["gates"]["content_fidelity"] is True
    assert report["published"] is True
    assert len(PdfReader(str(output / "main.pdf")).pages) > 0
    assert before == source_snapshot(source)


def test_w03_cannot_generate_before_all_ambiguous_headings_are_confirmed(
    tmp_path: Path,
) -> None:
    registry = load_builtin_template_registry(REPO_ROOT)
    with pytest.raises(Exception, match="低置信度标题候选尚未确认"):
        run_minimal_registered_generation(
            runner=PipelineRunner(),
            resolved_template=resolve_template(registry, "ouc-bachelor"),
            repository_root=REPO_ROOT,
            docx_path=REPO_ROOT / "tests/fixtures/word/W03_ambiguous_structure.docx",
            output_directory=tmp_path / "unconfirmed", case_id="W03",
            translation_client=FakeLLMClient(),
        )


@pytest.mark.parametrize("template_id", ["ouc-graduate", "ouc-bachelor"])
def test_w03_explicit_keep_body_is_a_valid_safe_decision(tmp_path: Path, template_id: str) -> None:
    registry = load_builtin_template_registry(REPO_ROOT)
    resolved = resolve_template(registry, template_id)
    source = registry.source_root(resolved.manifest)
    before = source_snapshot(source)
    decisions = {unit_id: "body" for unit_id in _decisions()}
    output = tmp_path / "keep-body"
    report = run_minimal_registered_generation(
        runner=PipelineRunner(),
        resolved_template=resolved,
        repository_root=REPO_ROOT,
        docx_path=REPO_ROOT / "tests/fixtures/word/W03_ambiguous_structure.docx",
        output_directory=output, case_id="W03-body",
        translation_client=FakeLLMClient(), heading_decisions=decisions,
    )
    tex = (
        output / resolved.adapter.content_directory / "section_01.tex"
    ).read_text(encoding="utf-8")
    assert "研究方法" in tex and "实验结果" in tex
    assert tex.count("section_heading") == 1
    assert report["gates"]["structural_counts"]["section_count"] == (1, 1)
    assert report["content"]["role_counts"]["content_table"] == 1
    assert before == source_snapshot(source)
    assert report["published"] is True


##### 章与小节确认板块 #####


@pytest.mark.parametrize("template_id", ["ouc-graduate", "ouc-bachelor"])
@pytest.mark.parametrize("level", ["chapter", "subsection"])
def test_w03_changed_level_uses_same_confirmed_counts(tmp_path, template_id, level):
    registry = load_builtin_template_registry(REPO_ROOT)
    resolved = resolve_template(registry, template_id)
    source = registry.source_root(resolved.manifest)
    before = source_snapshot(source)
    report = run_minimal_registered_generation(
        runner=PipelineRunner(), resolved_template=resolved, repository_root=REPO_ROOT,
        docx_path=REPO_ROOT / "tests/fixtures/word/W03_ambiguous_structure.docx",
        output_directory=tmp_path / template_id, case_id=f"W03-{level}",
        translation_client=FakeLLMClient(),
        heading_decisions={unit_id: level for unit_id in _decisions()},
    )
    assert report["published"] is True
    counts = report["gates"]["structural_counts"]
    assert counts["chapter_count"] == ((3, 3) if level == "chapter" else (1, 1))
    assert counts["subsection_count"] == ((2, 2) if level == "subsection" else (0, 0))
    assert report["content"]["role_counts"] == report["content"]["expected_roles"]
    assert report["content"]["role_counts"]["content_table"] == 1
    assert before == source_snapshot(source)
