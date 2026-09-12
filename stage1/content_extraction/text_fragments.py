# -*- coding: utf-8 -*-
"""正文与嵌套内容共用的文字片段抽取和 token 绑定。"""

from typing import Any
from docx.text.paragraph import Paragraph
from content_extraction.content_units import ContentUnit, ExtractionIssue
from content_extraction.citations import iter_numeric_citations, iter_superscript_citations


##### 语义片段抽取板块 #####


def _extract_text_fragments(
    parent: ContentUnit,
    factory: Any,
    issues: list[ExtractionIssue],
    paragraph: Paragraph | None = None,
) -> list[ContentUnit]:
    """提取段落中用户原有的 LaTeX 公式和数字引用，不重复计算父段落字符。"""
    children: list[ContentUnit] = []
    formula_ranges: list[tuple[int, int]] = []
    stripped = parent.text.strip()
    from content_extraction.formula_validation import ERROR_MESSAGES, scan_latex_formula_fragments

    for candidate in scan_latex_formula_fragments(parent.text):
        start, end = candidate["start"], candidate["end"]
        formula_ranges.append((start, end))
        valid = candidate["status"] == "valid"
        formula = factory.create(
            "formula",
            text=candidate.get("body", candidate["raw_text"]),
            source={**parent.source, "start_offset": start, "end_offset": end},
            properties={
                "display": candidate["delimiter"] == "$$"
                or (parent.unit_type == "paragraph" and candidate["raw_text"] == stripped),
            },
            relations={"parent_unit_id": parent.unit_id},
            payload={
                "source_syntax": "latex" if valid else "latex_malformed",
                "delimiter": candidate["delimiter"],
                "raw_text": candidate["raw_text"],
                **({} if valid else {"error_code": candidate["error_code"]}),
            },
            status="extracted" if valid else "degraded",
        )
        children.append(formula)
        if not valid:
            issues.append(ExtractionIssue(
                code=f"LATEX_FORMULA_{candidate['error_code'].upper()}",
                message=ERROR_MESSAGES[candidate["error_code"]],
                severity="error", unit_id=formula.unit_id, source=formula.source,
            ))
    citations = list(iter_numeric_citations(parent.text))
    bracket_ranges = [(item["start"], item["end"]) for item in citations]
    citations.extend(
        item for item in (iter_superscript_citations(paragraph) if paragraph is not None else [])
        if not any(item["start"] < end and item["end"] > start for start, end in bracket_ranges)
    )
    for citation in sorted(citations, key=lambda item: item["start"]):
        start, end = citation["start"], citation["end"]
        if any(start < formula_end and end > formula_start for formula_start, formula_end in formula_ranges):
            continue
        children.append(
            factory.create(
                "citation",
                text=citation["raw"],
                source={**parent.source, "start_offset": start, "end_offset": end},
                relations={"parent_unit_id": parent.unit_id},
                payload={
                    "numbers": citation["numbers"],
                    "context_text": parent.text,
                    "citation_syntax": citation.get("citation_syntax", "numeric_brackets"),
                    "parse_error": citation["error"],
                },
                status="extracted" if citation["status"] == "parsed" else "degraded",
            )
        )
    return children


##### 有序绑定板块 #####


def _bind_text_fragment_tokens(
    tokens: list[dict[str, Any]], fragments: list[ContentUnit]
) -> list[dict[str, Any]]:
    """按父段落偏移绑定公式和引用候选，避免渲染阶段再次猜测整段文本。"""
    pending = sorted(
        fragments, key=lambda item: int(item.source.get("start_offset", -1))
    )
    if not pending:
        return tokens
    result: list[dict[str, Any]] = []
    text_offset = 0
    fragment_index = 0
    for token in tokens:
        if token.get("kind") != "text":
            result.append(token)
            if token.get('kind') == 'line_break':
                text_offset += 1
            continue
        text = token.get("text", "")
        token_start = text_offset
        token_end = token_start + len(text)
        cursor = 0
        while fragment_index < len(pending):
            fragment = pending[fragment_index]
            start = int(fragment.source.get("start_offset", -1))
            end = int(fragment.source.get("end_offset", -1))
            if start >= token_end:
                break
            if start < token_start or end > token_end:
                # Word 相邻文本 run 会先合并；跨语义对象的美元片段不应被重组。
                break
            local_start = start - token_start
            local_end = end - token_start
            if local_start > cursor:
                result.append({"kind": "text", "text": text[cursor:local_start]})
            result.append({"kind": fragment.unit_type, "unit_id": fragment.unit_id})
            cursor = local_end
            fragment_index += 1
        if cursor < len(text):
            result.append({"kind": "text", "text": text[cursor:]})
        text_offset = token_end
    return result
