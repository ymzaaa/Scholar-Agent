# -*- coding: utf-8 -*-
"""数据表 PDF 可见性检查；不分类反馈、不规划补丁，也不负责交付。"""

import re
from pathlib import Path
from typing import Any

from pipeline.source_content_check import strip_latex_comments
from pipeline.pdf_layout_regression import evaluate_pdf_layout_regression


##### 逻辑行读取板块 #####

def table_rows(text: str) -> list[str]:
    """按顶层行结束符分组，保留单元格标记和单元格内的手动换行。"""
    rows = []
    start = index = depth = 0
    while index < len(text):
        character = text[index]
        if character == '%':
            end = text.find('\n', index)
            index = len(text) if end < 0 else end + 1
            continue
        if character == '\\':
            if text[index:index + 2] == r'\\' and depth == 0:
                end = index + 2
                spacing = re.match(r'\[[^\]]*\]', text[end:])
                if spacing:
                    end += spacing.end()
                rows.append(text[start:end].strip())
                start = index = end
            else:
                index += 2
            continue
        if character == '{':
            depth += 1
        elif character == '}':
            depth -= 1
        index += 1
    if text[start:].strip() or depth:
        raise ValueError('数据行缺少完整的顶层行结束符。')
    return rows



##### PDF 可见性板块 #####

def _plain(value: str) -> str:
    value = re.sub(r"\\(?:textbf|emph|makecell|multicolumn)\{[^{}]*\}", "", value)
    value = re.sub(r"\\[A-Za-z]+(?:\[[^]]*\])?", "", value)
    value = value.replace(r"\&", "&").replace("{", "").replace("}", "")
    return re.sub(r"[^0-9A-Za-z\u3400-\u9fff]+", "", value).lower()


def table_row_signatures(source: str, unit_id: str) -> list[str]:
    block = re.search(
        rf"% SCHOLAR_UNIT_BEGIN {re.escape(unit_id)} table_caption\n(.*?)"
        rf"% SCHOLAR_UNIT_END {re.escape(unit_id)}",
        source, re.DOTALL,
    )
    if not block:
        return []
    rows = re.search(r"(?:\\endlastfoot|\\midrule(?:\[[^]]*\])?)\s*(.*?)\\end\{longtable\}", block.group(1), re.DOTALL)
    if not rows:
        return []
    signatures = []
    for row in table_rows(re.sub(r"\\bottomrule(?:\[[^]]*\])?\s*$", "", rows.group(1))):
        line = strip_latex_comments(row).strip()
        if not line.strip().endswith(r"\\"):
            continue
        cells = [_plain(cell) for cell in line.rsplit(r"\\", 1)[0].split("&")]
        candidates = [cell for cell in cells if len(cell) >= 3]
        if candidates:
            signatures.append(max(candidates, key=len))
    return signatures


def evaluate_table_pdf(
    root: Path, target_file: str, unit_id: str,
    baseline_pdf_path: str | Path | None = None,
) -> dict[str, Any]:
    """用每个数据行的最长文本签名检查 PDF 可见性，不只统计 LaTeX 源码行。"""
    source = (root / target_file).read_text(encoding="utf-8")
    signatures = table_row_signatures(source, unit_id)
    if not signatures:
        return {"passed": False, "visible": 0, "total": 0, "detail": "未解析到跨页表格数据行。"}
    from pypdf import PdfReader
    pdf_text = "".join(page.extract_text() or "" for page in PdfReader(str(root / "main.pdf")).pages)
    normalized_pdf = _plain(pdf_text)
    visible = sum(signature in normalized_pdf for signature in signatures)
    result = {
        "passed": visible == len(signatures),
        "visible": visible,
        "total": len(signatures),
        "ratio": round(visible / len(signatures), 6),
        "detail": f"PDF 可见数据行 {visible}/{len(signatures)}。",
    }
    if baseline_pdf_path:
        layout = evaluate_pdf_layout_regression(
            baseline_pdf_path, root / "main.pdf", signatures,
        )
        result["layout_regression"] = layout
        result["passed"] = bool(result["passed"] and layout["passed"])
        result["detail"] += f" {layout['detail']}"
    return result
