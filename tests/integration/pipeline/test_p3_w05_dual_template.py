# -*- coding: utf-8 -*-
"""P3/W05：数字引用事务在两个固定模板中的一致性验收。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.minimal_registered_generation import run_minimal_registered_generation
from template_registry.registry import resolve_template
from template_registry.registry import load_builtin_template_registry
from pipeline_api import PipelineRunner
from tests.support.fake_llm import FakeLLMClient
from tests.support.paths import REPO_ROOT
from tests.support.template_source import source_snapshot


##### 双模板验收板块 #####


@pytest.mark.parametrize("template_id", ["ouc-graduate", "ouc-bachelor"])
def test_w05_binds_all_citations_and_preserves_non_citations(
    tmp_path: Path, template_id: str
) -> None:
    expected = json.loads(
        (REPO_ROOT / "tests" / "baselines" / "synthetic"
         / "W05_references_complete.expected.json").read_text(encoding="utf-8")
    )["target_expectations"]["references"]
    registry = load_builtin_template_registry(REPO_ROOT)
    resolved = resolve_template(registry, template_id)
    source = registry.source_root(resolved.manifest)
    before = source_snapshot(source)
    output = tmp_path / template_id

    report = run_minimal_registered_generation(
        runner=PipelineRunner(), resolved_template=resolved,
        repository_root=REPO_ROOT,
        docx_path=(REPO_ROOT / "tests" / "fixtures" / "word"
                   / "W05_references_complete.docx"),
        output_directory=output, case_id="W05",
        translation_client=FakeLLMClient(), report_filename="p3-report.json",
        reference_source="word",
    )
    after = source_snapshot(source)

    content = (
        output / resolved.adapter.content_directory / "section_01.tex"
    ).read_text(encoding="utf-8")
    references = (output / "data" / "references.tex").read_text(encoding="utf-8")
    decisions = report["references"]["citation_review"]

    assert report["published"] is True
    assert report["references"]["transaction_status"] == "committed"
    assert report["references"]["number_to_key"] == expected["number_to_key"]
    assert decisions["counts"] == {
        "body_citation": 2, "non_citation": 1,
        "reference_label": 3, "needs_review": 0,
    }
    assert content.count(r"\cite{") == 2
    assert r"\cite{word-ref-0001}" in content
    assert r"\cite{word-ref-0002,word-ref-0003}" in content
    assert all(text in content for text in expected["non_citation_texts"])
    assert references.count(r"\bibitem[") == 3
    assert all(
        rf"\bibitem[{number}]{{{key}}}" in references
        for number, key in expected["number_to_key"].items()
    )
    pdf = output / "main.pdf"
    assert pdf.read_bytes().startswith(b"%PDF-")
    assert (output / "latex-source.zip").is_file()
    assert before == after
