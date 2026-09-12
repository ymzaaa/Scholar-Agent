# -*- coding: utf-8 -*-
"""P3/W09：异常公式在模板渲染前统一阻断。"""

from pathlib import Path

import pytest

from content_extraction.formula_validation import FormulaPreflightBlocked
from template_registry.registry import resolve_template
from template_registry.registry import load_builtin_template_registry
from pipeline_api import PipelineRunner
from tests.support.paths import REPO_ROOT
from tests.support.template_source import source_snapshot


def _recognized_w09():
    runner = PipelineRunner()
    extracted = runner.extract(
        str(REPO_ROOT / "tests/fixtures/word/W09_formulas_malformed.docx")
    )
    return runner, extracted, runner.recognize(extracted)


def test_w09_reports_three_candidates_and_keeps_following_body() -> None:
    _, extracted, recognized = _recognized_w09()
    review = recognized.review["formula_review"]
    assert review["status"] == "blocked"
    assert review["valid_formula_count"] == 0
    assert [item["raw_text"] for item in review["candidates"]] == [
        "$x+y", "$$x+y$", r"$$\frac{a}{b$$",
    ]
    assert [item["error_code"] for item in review["candidates"]] == [
        "unclosed_delimiter", "mismatched_delimiter", "unbalanced_braces",
    ]
    assert extracted.paragraphs[-1]["text"] == "错误公式之后的正文必须继续保留。"
    assert extracted.paragraphs[-1].get("semantic_role") is None
    assert all(
        unit["status"] == "degraded"
        and unit["payload"]["source_syntax"] == "latex_malformed"
        for unit in extracted.content_units if unit["unit_type"] == "formula"
    )


@pytest.mark.parametrize("template_id", ["ouc-graduate", "ouc-bachelor"])
def test_w09_blocks_before_template_materialization(
    tmp_path: Path, template_id: str
) -> None:
    runner, extracted, recognized = _recognized_w09()
    registry = load_builtin_template_registry(REPO_ROOT)
    resolved = resolve_template(registry, template_id)
    source = registry.source_root(resolved.manifest)
    before = source_snapshot(source)
    output = tmp_path / template_id

    with pytest.raises(FormulaPreflightBlocked) as caught:
        runner.render(
            extracted, recognized, str(source), str(output), None,
            reference_source="none",
        )

    assert caught.value.review["malformed_formula_count"] == 3
    assert not output.exists()
    assert before == source_snapshot(source)
