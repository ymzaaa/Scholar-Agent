"""Word 数字引用候选、文末列表和确定性分类。"""

from __future__ import annotations

import copy
import re
from typing import Any, Iterator

from docx.oxml.ns import qn


##### 数字引用解析板块 #####


NUMERIC_CITATION_PATTERN = re.compile(
    r"\[\s*(?P<expression>[1-9]\d*(?:\s*(?:[,，;；、]|[-–—~～])\s*[1-9]\d*)*)\s*\]"
)
_SEPARATOR_PATTERN = re.compile(r"[,，;；、]")
_RANGE_PATTERN = re.compile(r"^(\d+)\s*[-–—~～]\s*(\d+)$")


def expand_numeric_expression(expression: str, *, max_range: int = 100) -> list[str]:
    """把 `1,3-5` 展开为稳定去重的编号；异常区间明确抛错。"""
    numbers: list[str] = []
    for part in _SEPARATOR_PATTERN.split(expression):
        value = part.strip()
        if not value:
            continue
        range_match = _RANGE_PATTERN.match(value)
        if range_match:
            start, end = map(int, range_match.groups())
            if end < start or end - start + 1 > max_range:
                raise ValueError(f"引用区间无效或过大：{value}")
            numbers.extend(str(number) for number in range(start, end + 1))
        elif value.isdigit():
            numbers.append(str(int(value)))
        else:
            raise ValueError(f"无法解析数字引用：{value}")
    return list(dict.fromkeys(numbers))


def iter_numeric_citations(text: str) -> Iterator[dict[str, Any]]:
    for match in NUMERIC_CITATION_PATTERN.finditer(text):
        try:
            numbers = expand_numeric_expression(match.group("expression"))
            status = "parsed"
            error = None
        except ValueError as exc:
            numbers = []
            status = "failed"
            error = str(exc)
        yield {
            "raw": match.group(0),
            "start": match.start(),
            "end": match.end(),
            "numbers": numbers,
            "status": status,
            "error": error,
        }


def iter_superscript_citations(paragraph: Any) -> Iterator[dict[str, Any]]:
    """只读取正文直接 run 的显式数字上标，不进入数学、脚注或域对象。"""
    offset = 0
    grouped: list[tuple[int, str]] = []
    for run in paragraph.runs:
        text = run.text
        alignment = run._r.find("./" + qn("w:rPr") + "/" + qn("w:vertAlign"))
        superscript = alignment is not None and alignment.get(qn("w:val")) == "superscript"
        has_note = any(run._r.find(qn(tag)) is not None for tag in (
            "w:footnoteReference", "w:endnoteReference", "w:footnoteRef",
        ))
        if superscript and text and not has_note:
            if grouped and grouped[-1][0] + len(grouped[-1][1]) == offset:
                start, previous = grouped[-1]
                grouped[-1] = (start, previous + text)
            else:
                grouped.append((offset, text))
        offset += len(text)
    for start, raw in grouped:
        if not re.fullmatch(r"\s*[1-9]\d*(?:\s*(?:[,，;；、]|[-–—~～])\s*[1-9]\d*)*\s*", raw):
            continue
        prefix = paragraph.text[:start]
        # 紧邻字母、数字或数学运算符的上标是指数/单位证据，不能直接当成文献。
        if prefix and re.search(r"[A-Za-z0-9=+*/^_)）]\s*$", prefix):
            continue
        try:
            numbers = expand_numeric_expression(raw)
            error = None
        except ValueError as exc:
            numbers, error = [], str(exc)
        yield {
            "raw": raw, "start": start, "end": start + len(raw),
            "numbers": numbers, "status": "failed" if error else "parsed",
            "error": error, "citation_syntax": "numeric_superscript",
        }


##### Word文末列表识别板块 #####


_REFERENCE_HEADINGS = {"参考文献", "references", "bibliography", "参考资料"}
_NEXT_REGION_HEADINGS = {"致谢", "附录", "攻读学位期间取得的成果", "作者简介"}
_ENTRY_EVIDENCE = re.compile(
    r"(?:\b(?:19|20)\d{2}\b|\bdoi\s*:|\[j\]|\[m\]|\[c\]|\bet\s+al\.?\b)",
    re.IGNORECASE,
)
_NON_CITATION_PREFIX = re.compile(
    r"(?:区间|数组索引(?:示例)?|索引(?:示例)?|坐标|矩阵|向量|集合|下标|"
    r"取值范围|编号示例)(?:为|是|取|=|:|：)?\s*$",
    re.IGNORECASE,
)
_NON_CITATION_EXPLICIT = re.compile(
    r"不(?:是|属于)(?:参考文献)?引用", re.IGNORECASE
)
_CITATION_CONTEXT = re.compile(
    r"(?:研究|文献|报道|分析|方法|结果|学者|作者|资料|参见|指出|认为|发现)",
    re.IGNORECASE,
)
_CITATION_FOLLOWING_CONTEXT = re.compile(
    r"^(?:的|提出|引入|采用|使用|针对|通过|将|公开|计算|证明|开发|构建|设计|实现|首次|率先)",
    re.IGNORECASE,
)


def detect_word_reference_list(paragraphs: list[dict[str, Any]]) -> dict[str, Any]:
    """标题、位置、列表样式和著录特征共同判定，避免仅凭一个固定词。"""
    minimum_index = int(len(paragraphs) * 0.55)
    candidates = []
    for index, paragraph in enumerate(paragraphs):
        normalized = re.sub(r"[\s:：]", "", paragraph.get("text", "")).lower()
        if normalized not in _REFERENCE_HEADINGS:
            continue
        following = [item for item in paragraphs[index + 1:] if item.get("text", "").strip()][:6]
        evidence_count = sum(
            bool(_ENTRY_EVIDENCE.search(item.get("text", "")))
            or str(item.get("style", "")).lower().startswith("list")
            for item in following
            if item.get("text", "").strip()
        )
        score = (0.45 if index >= minimum_index else 0.0) + min(0.5, evidence_count * 0.15)
        candidates.append((score, index, evidence_count))
    if not candidates:
        return {"status": "not_found", "entries": [], "heading_index": None, "evidence": []}
    score, heading_index, evidence_count = max(candidates)
    if score < 0.65:
        return {
            "status": "needs_review",
            "entries": [],
            "heading_index": heading_index,
            "evidence": [f"score={score:.2f}", f"entry_evidence={evidence_count}"],
        }
    entries = []
    for paragraph in paragraphs[heading_index + 1:]:
        text = paragraph.get("text", "").strip()
        normalized = re.sub(r"[\s:：]", "", text).lower()
        if normalized in _NEXT_REGION_HEADINGS:
            break
        if not text:
            continue
        explicit = re.match(r"^\s*\[(\d+)\]\s*(.+)$", text)
        number = explicit.group(1) if explicit else str(len(entries) + 1)
        raw_text = explicit.group(2) if explicit else text
        entries.append({
            "number": str(int(number)),
            "raw_text": raw_text,
            "paragraph_index": paragraph.get("index"),
        })
    number_counts: dict[str, int] = {}
    for entry in entries:
        number_counts[entry["number"]] = number_counts.get(entry["number"], 0) + 1
    duplicate_numbers = sorted(
        number for number, count in number_counts.items() if count > 1
    )
    return {
        "status": "recognized" if entries else "needs_review",
        "entries": entries,
        "heading_index": heading_index,
        "duplicate_numbers": duplicate_numbers,
        "evidence": ["semantic_heading", "document_tail", f"entry_evidence={evidence_count}"],
    }


def classify_numeric_citation_candidates(
    paragraphs: list[dict[str, Any]],
    content_units: list[dict[str, Any]],
    reference_region: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把语法候选分为正文引用、非引用、文末标签和待确认项。"""
    region = reference_region or detect_word_reference_list(paragraphs)
    paragraph_by_index = {
        paragraph.get("index", index): paragraph
        for index, paragraph in enumerate(paragraphs)
    }
    entry_paragraphs = {
        entry.get("paragraph_index") for entry in region.get("entries", [])
    }
    available_numbers = {
        entry["number"] for entry in region.get("entries", [])
    }
    unique_numbers = available_numbers - set(region.get("duplicate_numbers", []))
    decisions = []
    for unit in content_units:
        if unit.get("unit_type") != "citation":
            continue
        source = unit.get("source", {})
        paragraph_index = source.get("paragraph_index")
        numbers = unit.get("payload", {}).get("numbers", [])
        evidence: list[str] = []
        if paragraph_index in entry_paragraphs and source.get("start_offset") == 0:
            decision = "reference_label"
            evidence.append("reference_entry_prefix")
        else:
            paragraph = paragraph_by_index.get(paragraph_index, {})
            text = unit.get("payload", {}).get("context_text", paragraph.get("text", ""))
            start = int(source.get("start_offset", -1))
            end = int(source.get("end_offset", -1))
            context_start = max(0, start - 24)
            context_end = min(len(text), end + 24)
            context = text[context_start:context_end]
            prefix = text[context_start:start]
            suffix_context = text[end:context_end]
            if (
                _NON_CITATION_PREFIX.search(prefix)
                or _NON_CITATION_EXPLICIT.search(suffix_context)
            ):
                decision = "non_citation"
                evidence.append("explicit_non_citation_context")
            else:
                suffix = text[end:].lstrip() if 0 <= end <= len(text) else ""
                sentence_position = not suffix or suffix[0] in "。；;，,！？!?、.)）]】"
                citation_context = bool(_CITATION_CONTEXT.search(context))
                following_context = bool(_CITATION_FOLLOWING_CONTEXT.search(suffix))
                is_superscript = unit.get("payload", {}).get("citation_syntax") == "numeric_superscript"
                uniquely_bound = bool(numbers) and all(number in unique_numbers for number in numbers)
                if numbers and (sentence_position or citation_context or following_context) and (not is_superscript or uniquely_bound):
                    decision = "body_citation"
                    evidence.extend([
                        (
                            "sentence_position" if sentence_position
                            else (
                                "citation_lexical_context" if citation_context
                                else "citation_following_context"
                            )
                        ),
                    ])
                    if all(number in unique_numbers for number in numbers):
                        evidence.append("all_numbers_uniquely_bound")
                    else:
                        evidence.append("binding_pending_selected_source")
                else:
                    decision = "needs_review"
                    evidence.append("plain_text_context_ambiguous")
        decisions.append({
            "unit_id": unit["unit_id"],
            "raw": unit.get("text", ""),
            "numbers": numbers,
            "decision": decision,
            "evidence": evidence,
            "source": source,
            "context": paragraph_by_index.get(paragraph_index, {}).get("text", ""),
        })
    counts = {
        name: sum(item["decision"] == name for item in decisions)
        for name in ("body_citation", "non_citation", "reference_label", "needs_review")
    }
    return {
        "report_version": "1.0.0",
        "reference_region_status": region.get("status"),
        "decisions": decisions,
        "counts": counts,
        "all_candidates_classified": counts["needs_review"] == 0,
    }


##### 人工确认板块 #####


def apply_citation_overrides(
    review: dict[str, Any], overrides: list[dict[str, Any]],
    *, require_resolved: bool = True,
) -> dict[str, Any]:
    """只改变已有候选的决定；原文、编号和来源证据始终沿用识别结果。"""
    result = copy.deepcopy(review)
    by_id = {item["unit_id"]: item for item in result.get("decisions", [])}
    seen = set()
    for override in overrides:
        unit_id, decision = override.get("unit_id"), override.get("decision")
        if set(override) != {"unit_id", "decision"} or unit_id not in by_id or unit_id in seen:
            raise ValueError(f"引用确认单元无效或重复：{unit_id}")
        if decision not in {"body_citation", "non_citation"}:
            raise ValueError(f"引用确认决定无效：{decision}")
        seen.add(unit_id)
        by_id[unit_id]["decision"] = decision
        by_id[unit_id]["overridden"] = True
    counts = {
        name: sum(item["decision"] == name for item in by_id.values())
        for name in ("body_citation", "non_citation", "reference_label", "needs_review")
    }
    if require_resolved and counts["needs_review"]:
        raise ValueError(f"仍有 {counts['needs_review']} 个引用候选未确认")
    result["counts"] = counts
    result["all_candidates_classified"] = not counts["needs_review"]
    return result


