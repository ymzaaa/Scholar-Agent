# -*- coding: utf-8 -*-
"""W03 标题候选只使用确定性强证据和可复核弱展示证据。"""

from pipeline_api import PipelineRunner
from tests.support.paths import REPO_ROOT


def test_w03_consolidates_only_ambiguous_short_headings() -> None:
    runner = PipelineRunner()
    extracted = runner.extract(
        str(REPO_ROOT / "tests/fixtures/word/W03_ambiguous_structure.docx")
    )
    recognized = runner.recognize(extracted)
    candidates = recognized.review["heading_candidates"]

    high = [item["text"] for item in candidates if not item["requires_review"]]
    review = [item for item in candidates if item["requires_review"]]
    assert high == ["1 绪论", "1.1 研究背景"]
    assert [item["text"] for item in review] == ["研究方法", "实验结果"]
    assert all(item["level"] == "body" for item in review)
    assert all(item["suggested_level"] == "section" for item in review)
    assert all(item["review_status"] == "pending" for item in review)
    assert "本段讨论2024年度研究结果，但它本身不是一个标题。" not in {
        item["text"] for item in candidates
    }


def test_w03_records_presentation_as_evidence_not_latex_format() -> None:
    extracted = PipelineRunner().extract(
        str(REPO_ROOT / "tests/fixtures/word/W03_ambiguous_structure.docx")
    )
    method = next(item for item in extracted.paragraphs if item["text"] == "研究方法")
    assert method["presentation"]["bold_ratio"] == 1.0
    assert method["presentation"]["dominant_font_size_pt"] == 14.0
    assert "latex" not in method["presentation"]
