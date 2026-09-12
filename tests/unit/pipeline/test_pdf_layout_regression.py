# -*- coding: utf-8 -*-
"""父子 PDF 排版回归门禁的最小确定性测试。"""

import pipeline.pdf_layout_regression as regression


##### 虚拟指标板块 #####


def _snapshot(pages=94, font=12.0, gap=19.9, image_pages=0):
    return regression.PdfLayoutSnapshot(
        page_count=pages, downstream_start_page=60,
        dominant_font_size_pt=font, median_line_gap_pt=gap,
        image_only_pages=image_pages,
    )


def test_layout_regression_rejects_v7_style_global_drift(monkeypatch) -> None:
    snapshots = iter([_snapshot(), _snapshot(86, 10.5, 12.6, 1)])
    monkeypatch.setattr(regression, "snapshot_pdf_layout", lambda *_args: next(snapshots))
    result = regression.evaluate_pdf_layout_regression("v6.pdf", "v7.pdf", ["row52"])
    assert result["passed"] is False
    assert len(result["failures"]) == 4


def test_layout_regression_allows_local_paginate_change(monkeypatch) -> None:
    snapshots = iter([_snapshot(), _snapshot(95, 12.0, 20.1, 0)])
    monkeypatch.setattr(regression, "snapshot_pdf_layout", lambda *_args: next(snapshots))
    result = regression.evaluate_pdf_layout_regression("parent.pdf", "candidate.pdf", ["row52"])
    assert result["passed"] is True
    assert result["failures"] == []
