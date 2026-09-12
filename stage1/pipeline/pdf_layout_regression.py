# -*- coding: utf-8 -*-
"""比较父 PDF 与候选 PDF 的非目标排版，阻断局部修复造成的全局漂移。"""

from __future__ import annotations

import statistics
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


##### 数据模型板块 #####


@dataclass(frozen=True, slots=True)
class PdfLayoutSnapshot:
    page_count: int
    downstream_start_page: int
    dominant_font_size_pt: float
    median_line_gap_pt: float
    image_only_pages: int


##### PDF 指标板块 #####


def _plain(value: str) -> str:
    return "".join(character.lower() for character in value if character.isalnum())


def _downstream_start(reader: Any, row_signatures: list[str]) -> int:
    """从目标表最后一行所在页之后取样，避免把表格字号当成正文字号。"""
    normalized = [_plain(page.extract_text() or "") for page in reader.pages]
    scores = [
        sum(bool(signature and signature in text) for signature in row_signatures)
        for text in normalized
    ]
    peak = max(scores, default=0)
    if peak == 0:
        return 0
    peak_index = scores.index(peak)
    threshold = max(2, round(peak * 0.1))
    last_table_page = peak_index
    while (
        last_table_page + 1 < len(scores)
        and scores[last_table_page + 1] >= threshold
    ):
        last_table_page += 1
    return min(last_table_page + 1, len(reader.pages))


def _quantized_font_size(value: float) -> float:
    return round(float(value) * 2) / 2


def snapshot_pdf_layout(pdf_path: str | Path, row_signatures: list[str]) -> PdfLayoutSnapshot:
    from pypdf import PdfReader

    reader = PdfReader(str(pdf_path))
    start = _downstream_start(reader, row_signatures)
    font_weights: Counter[float] = Counter()
    line_gaps: list[float] = []
    image_only_pages = 0

    # 只观察目标后的固定窗口，避免参考文献的小字号覆盖正文主导字号。
    for page in reader.pages[start:start + 20]:
        page_text = _plain(page.extract_text() or "")
        if page.images and len(page_text) < 180:
            image_only_pages += 1
        y_positions: list[float] = []

        def visit_text(text, _cm, tm, _font, font_size) -> None:
            normalized = _plain(text)
            size = float(font_size)
            if len(normalized) >= 5 and 8.0 <= size <= 14.5:
                font_weights[_quantized_font_size(size)] += len(normalized)
                y_positions.append(round(float(tm[5]), 1))

        page.extract_text(visitor_text=visit_text)
        lines = sorted(set(y_positions), reverse=True)
        line_gaps.extend(
            upper - lower for upper, lower in zip(lines, lines[1:])
            if 3.0 <= upper - lower <= 80.0
        )

    dominant = font_weights.most_common(1)[0][0] if font_weights else 0.0
    median_gap = statistics.median(line_gaps) if line_gaps else 0.0
    return PdfLayoutSnapshot(
        page_count=len(reader.pages), downstream_start_page=start + 1,
        dominant_font_size_pt=round(dominant, 2),
        median_line_gap_pt=round(median_gap, 2),
        image_only_pages=image_only_pages,
    )


##### 回归判定板块 #####


def evaluate_pdf_layout_regression(
    baseline_pdf: str | Path, candidate_pdf: str | Path,
    row_signatures: list[str],
) -> dict[str, Any]:
    """仅阻断明显的全局漂移；局部表格分页允许页数小幅增加。"""
    baseline = snapshot_pdf_layout(baseline_pdf, row_signatures)
    candidate = snapshot_pdf_layout(candidate_pdf, row_signatures)
    failures: list[str] = []
    allowed_page_drop = max(2, round(baseline.page_count * 0.03))
    if candidate.page_count < baseline.page_count - allowed_page_drop:
        failures.append(
            f"总页数由 {baseline.page_count} 降至 {candidate.page_count}，超出局部表格修复边界。"
        )
    if abs(candidate.dominant_font_size_pt - baseline.dominant_font_size_pt) > 0.5:
        failures.append(
            "目标表后的主导正文字号由 "
            f"{baseline.dominant_font_size_pt}pt 变为 {candidate.dominant_font_size_pt}pt。"
        )
    if baseline.median_line_gap_pt and candidate.median_line_gap_pt:
        ratio = candidate.median_line_gap_pt / baseline.median_line_gap_pt
        if not 0.8 <= ratio <= 1.25:
            failures.append(
                "目标表后的正文行距中位数由 "
                f"{baseline.median_line_gap_pt}pt 变为 {candidate.median_line_gap_pt}pt。"
            )
    if candidate.image_only_pages > baseline.image_only_pages:
        failures.append(
            "目标表后图片近乎独占页面的数量由 "
            f"{baseline.image_only_pages} 增至 {candidate.image_only_pages}。"
        )
    return {
        "passed": not failures,
        "baseline": asdict(baseline),
        "candidate": asdict(candidate),
        "failures": failures,
        "detail": "非目标页面排版未发现明显漂移。" if not failures else "；".join(failures),
    }
