# -*- coding: utf-8 -*-
"""基于多项 Word 证据识别标题、题注及其对象关系。"""

from __future__ import annotations

import re
from typing import Any


##### 文本规范化板块 #####


CHINESE_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}


def normalize_text(text: str) -> str:
    return re.sub(r"[\u00a0\u2002-\u200b\u3000]+", " ", text or "").strip()


def chinese_number_to_int(value: str) -> int | None:
    value = normalize_text(value)
    if value.isdigit():
        return int(value)
    if not value:
        return None
    if value == "十":
        return 10
    if "十" in value:
        left, right = value.split("十", 1)
        tens = CHINESE_DIGITS.get(left, 1) if left else 1
        ones = CHINESE_DIGITS.get(right, 0) if right else 0
        return tens * 10 + ones
    if len(value) == 1:
        return CHINESE_DIGITS.get(value)
    return None


##### 标题识别板块 #####


RE_SUBSECTION = re.compile(
    r"^(\d+)\s*[.．]\s*(\d+)\s*[.．]\s*(\d+)\s*[.．]?\s*(\S.*)$"
)
RE_SECTION = re.compile(r"^(\d+)\s*[.．]\s*(\d+)\s*[.．]?\s*(\S.*)$")
RE_CHAPTER_EXPLICIT = re.compile(
    r"^第\s*([0-9一二两三四五六七八九十〇零]+)\s*章\s*[：:、.．]?\s*(\S.*)$"
)
RE_CHAPTER_ARABIC = re.compile(r"^(\d{1,2})\s+(\S.*)$")
RE_CHAPTER_DOTTED = re.compile(r"^(\d{1,2})\s*[、.．]\s*(\S.*)$")
RE_CHAPTER_CHINESE = re.compile(
    r"^([一二两三四五六七八九十]{1,3})\s*[、.．]\s*(\S.*)$"
)


def _style_level(style: str | None) -> int | None:
    compact = re.sub(r"\s+", "", (style or "").lower())
    patterns = {
        0: ("heading1", "标题1", "标题一", "章标题", "chaptertitle"),
        1: ("heading2", "标题2", "标题二", "节标题", "sectiontitle"),
        2: ("heading3", "标题3", "标题三", "subsectiontitle"),
    }
    for level, names in patterns.items():
        if any(name in compact for name in names):
            return level
    return None


def infer_heading(
    text: str,
    *,
    style: str | None = None,
    outline_level: int | None = None,
    numbering_level: int | None = None,
) -> dict[str, Any] | None:
    """解析单段标题证据；正式层级由文档级候选分析决定。"""
    normalized = normalize_text(text)
    if not normalized or re.search(r"[。！？；!?;]", normalized):
        return None

    match = RE_SUBSECTION.match(normalized)
    if match:
        title = match.group(4).strip()
        if re.match(r"^\d+(?:[.．]\d+){1,2}[节章]", normalized) or re.search(r"[。！？；]", title):
            return None
        return {
            "level": "subsection",
            "number": ".".join(match.groups()[:3]),
            "title": title,
            "confidence": 0.98,
            "evidence": ["text_numbering"],
        }
    match = RE_SECTION.match(normalized)
    if match:
        title = match.group(3).strip()
        if re.match(r"^\d+(?:[.．]\d+){1,2}[节章]", normalized) or re.search(r"[。！？；]", title):
            return None
        return {
            "level": "section",
            "number": ".".join(match.groups()[:2]),
            "title": title,
            "confidence": 0.98,
            "evidence": ["text_numbering"],
        }

    for pattern, evidence in (
        (RE_CHAPTER_EXPLICIT, "explicit_chapter_marker"),
        (RE_CHAPTER_ARABIC, "arabic_numbering"),
        (RE_CHAPTER_DOTTED, "dotted_numbering"),
        (RE_CHAPTER_CHINESE, "chinese_numbering"),
    ):
        match = pattern.match(normalized)
        if match:
            number = chinese_number_to_int(match.group(1))
            if number is None or not 1 <= number <= 99:
                continue
            title = match.group(2).strip()
            # “第一章：……。本章……”属于正文概述，不应因开头章号被误判。
            if evidence == "explicit_chapter_marker" and re.search(r"[。！？；]", title):
                continue
            confidence = 0.99 if evidence == "explicit_chapter_marker" else 0.92
            return {
                "level": "chapter",
                "number": str(number),
                "title": title,
                "confidence": confidence,
                "evidence": [evidence],
            }

    style_level = _style_level(style)
    effective_level = None
    evidence = []
    if style_level is not None:
        effective_level = style_level
        evidence.append("paragraph_style")
    if outline_level is not None and style_level is not None:
        evidence.append("outline_level")
    if numbering_level is not None:
        # 自动编号本身可作为证据；裸 outlineLvl 在真实 Word 中常被摘要等前置页复用。
        effective_level = numbering_level if effective_level is None else effective_level
        evidence.append("word_numbering")
    if effective_level in {0, 1, 2} and evidence:
        names = {0: "chapter", 1: "section", 2: "subsection"}
        return {
            "level": names[effective_level],
            "number": None,
            "title": normalized,
            "confidence": 0.88 if len(evidence) > 1 else 0.82,
            "evidence": evidence,
        }
    return None



def _is_heading_text(text: str) -> bool:
    return bool(1 <= len(normalize_text(text)) <= 40 and not re.search(r"[。！？；!?;]", text) and not infer_caption(text))


##### 正文区域板块 #####


def _back_matter_marker(paragraph: dict[str, Any]) -> str | None:
    """后置区域标题按明确名称识别；编号不会把它变成普通章节。"""
    text = normalize_text(paragraph.get("text", ""))
    numbered = infer_heading(text)
    title = numbered["title"] if numbered else text
    compact = re.sub(r"\s+", "", title).lower()
    if re.fullmatch(r"(?:参考文献|references|bibliography|致谢|附录(?:[A-Za-z一二三四五六七八九十0-9])?|研究成果|攻读学位期间(?:取得的(?:研究)?成果|发表的学术论文))", compact):
        return compact
    if _style_level(paragraph.get("style")) == 0 and re.match(r"^附录(?:\s|[A-Z0-9一二三四五六七八九十：:])", title):
        return "附录"
    return None


def recognize_regions(paragraphs: list[dict[str, Any]], units: list[dict[str, Any]]) -> dict[str, Any]:
    """先固定正文与后置边界，目录及后置标题不进入证据概况。"""
    explicit = []
    formatted = []
    for position, paragraph in enumerate(paragraphs):
        if paragraph.get("generated_contents") or _back_matter_marker(paragraph):
            continue
        text = normalize_text(paragraph.get("text", ""))
        heading = infer_heading(text)
        if heading and heading["level"] == "chapter":
            explicit.append(position)
        elif _is_heading_text(text) and _style_level(paragraph.get("style")) is not None:
            formatted.append(position)
    start = (explicit or formatted or [0])[0]
    back = []
    for position, paragraph in enumerate(paragraphs):
        if position < start or paragraph.get("generated_contents"):
            continue
        marker = _back_matter_marker(paragraph)
        if marker:
            back.append({"paragraph_index": position, "unit_id": paragraph.get("unit_id"), "kind": marker})
    end = back[0]["paragraph_index"] if back else len(paragraphs)
    semantic_positions = {
        unit.get("source", {}).get("paragraph_index") for unit in units
        if unit.get("unit_type") in {"image", "formula", "footnote", "field", "hyperlink", "text_box"}
    }
    # 空段落之后仍有正文表格时必须保留其范围，避免把尾部对象裁到正文之外。
    end_block = paragraphs[end].get("block_index", float("inf")) if end < len(paragraphs) else float("inf")
    last_table_block = max((
        unit.get("source", {}).get("block_index", -1) for unit in units
        if unit.get("unit_type") == "table"
        and unit.get("source", {}).get("block_index", -1) < end_block
    ), default=-1)
    while end > start and not paragraphs[end - 1].get("text", "").strip() and paragraphs[end - 1].get("index", end - 1) not in semantic_positions and paragraphs[end - 1].get("block_index", end - 1) > last_table_block:
        end -= 1
    return {"body_range": [start, end], "back_matter_start": back[0]["paragraph_index"] if back else None, "back_matter": back}


##### 文档证据板块 #####


def _document_evidence(paragraphs: list[dict[str, Any]]) -> tuple[dict, dict]:
    """同一样式或同一自动编号层级跨越明确层级时，不再用它决定层级。"""
    styles: dict[str, set[str]] = {}
    numbering: dict[tuple[Any, Any], set[str]] = {}
    for paragraph in paragraphs:
        heading = infer_heading(paragraph.get("text", ""))
        if not heading:
            continue
        styles.setdefault(paragraph.get("style", ""), set()).add(heading["level"])
        key = (paragraph.get("numbering_id"), paragraph.get("numbering_level"))
        if key[1] is not None:
            numbering.setdefault(key, set()).add(heading["level"])
    return styles, numbering


def recognize_heading_candidates(
    paragraphs: list[dict[str, Any]],
    *, regions: dict[str, Any] | None = None,
    content_units: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """按整篇一致性融合强证据；冲突和弱证据只提出候选，不改变正文。"""
    regions = regions or recognize_regions(paragraphs, [])
    start, end = regions["body_range"]
    # 居中的纯公式已经是明确内容对象，不能因展示特征成为标题候选。
    formula_only_ids = {
        unit["unit_id"] for unit in content_units or []
        if unit.get("unit_type") == "paragraph"
        and any(token.get("kind") == "formula" for token in unit.get("payload", {}).get("inline_tokens", []))
        and not any(token.get("kind") == "text" and token.get("text", "").strip() for token in unit.get("payload", {}).get("inline_tokens", []))
    }
    scoped = [p for p in paragraphs[start:end] if not p.get("generated_contents") and p.get("unit_id") not in formula_only_ids]
    styles, numbering = _document_evidence(scoped)
    names = {0: "chapter", 1: "section", 2: "subsection"}
    body_sizes = sorted(
        float(p["presentation"]["dominant_font_size_pt"]) for p in scoped
        if not infer_heading(p.get("text", ""))
        and isinstance(p.get("presentation", {}).get("dominant_font_size_pt"), (int, float))
    )
    body_size = body_sizes[len(body_sizes) // 2] if body_sizes else None
    strong_sizes = []
    for paragraph in scoped:
        heading = infer_heading(paragraph.get("text", ""))
        size = paragraph.get("presentation", {}).get("dominant_font_size_pt")
        if heading and isinstance(size, (int, float)):
            strong_sizes.append((float(size), heading["level"]))
    candidates = []
    previous_chapter_number = None
    current_chapter_number = None
    current_section_number = None
    current_subsection_number = None
    for position in range(start, end):
        paragraph = paragraphs[position]
        if paragraph.get("generated_contents") or paragraph.get("unit_id") in formula_only_ids:
            continue
        text = paragraph.get("text", "")
        explicit = infer_heading(text)
        if not explicit and not _is_heading_text(text):
            continue
        style = paragraph.get("style", "")
        style_level = _style_level(style)
        num_level = paragraph.get("numbering_level")
        num_key = (paragraph.get("numbering_id"), num_level)
        evidence = list(explicit["evidence"]) if explicit else []
        levels = [explicit["level"]] if explicit else []
        if style_level in names:
            if len(styles.get(style, set())) <= 1:
                levels.append(names[style_level])
                evidence.append("paragraph_style")
            else:
                evidence.append("mixed_level_style")
        if num_level in names:
            if len(numbering.get(num_key, set())) <= 1:
                levels.append(names[num_level])
                evidence.append("word_numbering")
            else:
                evidence.append("mixed_level_word_numbering")
        suggestion = explicit["level"] if explicit else (
            names.get(num_level) or names.get(style_level)
        )
        strong = bool(explicit or (num_level in names and "word_numbering" in evidence))
        conflict = len(set(levels)) > 1
        number = explicit.get("number") if explicit else None
        if explicit:
            components = tuple(int(value) for value in number.split("."))
            if explicit["level"] == "chapter":
                if previous_chapter_number is not None and components[0] <= previous_chapter_number:
                    conflict = True
                    evidence.append("source_number_order_conflict")
                previous_chapter_number = components[0]
                current_chapter_number, current_section_number = components[0], None
                current_subsection_number = None
            else:
                if current_chapter_number is not None and components[0] != current_chapter_number:
                    conflict = True
                    evidence.append("source_number_parent_conflict")
                if explicit["level"] == "section":
                    if current_section_number is not None and components <= current_section_number:
                        conflict = True
                        evidence.append("source_number_order_conflict")
                    current_section_number = components[:2]
                    current_subsection_number = None
                else:
                    if current_section_number is not None and components[:2] != current_section_number:
                        conflict = True
                        evidence.append("source_number_parent_conflict")
                    if current_subsection_number is not None and components <= current_subsection_number:
                        conflict = True
                        evidence.append("source_number_order_conflict")
                    current_subsection_number = components
        if conflict:
            evidence.append("heading_evidence_conflict")
        if not evidence:
            presentation = paragraph.get("presentation", {})
            size = presentation.get("dominant_font_size_pt")
            bold = float(presentation.get("bold_ratio") or 0)
            alignment = str(paragraph.get("layout", {}).get("alignment", {}).get("value") or "").upper()
            nearest = min(strong_sizes, key=lambda value: abs(value[0] - size)) if isinstance(size, (int, float)) and strong_sizes else None
            if bold >= 0.8:
                evidence.append("uniform_bold")
            larger = body_size is not None and isinstance(size, (int, float)) and size > body_size + 0.75
            if nearest and abs(nearest[0] - size) <= 0.75 and (bold >= 0.8 or larger):
                evidence.append("font_size_matches_confirmed_heading")
                suggestion = nearest[1]
            elif larger:
                evidence.append("font_size_larger_than_body")
            if "CENTER" in alignment or alignment == "1":
                evidence.append("center_alignment")
            # 弱证据只提出短标题候选；常见正文字号本身不构成标题证据。
            if not evidence:
                continue
        review = conflict or not strong
        candidates.append({
            "unit_id": paragraph["unit_id"], "text": text,
            "title": explicit["title"] if explicit else normalize_text(text),
            "level": "body" if review else suggestion,
            "suggested_level": suggestion or "section", "number": number,
            "confidence": 0.65 if review else (explicit["confidence"] if explicit else 0.88),
            "evidence": evidence, "requires_review": review,
            "review_status": "pending" if review else "auto_resolved",
            "source": {"block_index": paragraph.get("block_index"), "paragraph_index": position},
        })
    return candidates


##### 章节范围板块 #####


def chapters_from_headings(headings: list[dict[str, Any]], regions: dict[str, Any], *, validate: bool = False) -> list[dict[str, Any]]:
    """出现顺序决定文件顺序；来源编号只保存为证据，不强制连续。"""
    start, end = regions["body_range"]
    ordered = sorted(headings, key=lambda item: item["source"]["paragraph_index"])
    chapter_items = []
    has_section = False
    for item in ordered:
        level = item["level"]
        position = item["source"]["paragraph_index"]
        if level == "body":
            continue
        if not start <= position < end:
            raise ValueError("标题不在正文范围内")
        if level == "chapter":
            chapter_items.append(item)
            has_section = False
        elif validate:
            if not chapter_items:
                raise ValueError("节和小节必须位于某一章内")
            if level == "subsection" and not has_section:
                raise ValueError("小节不能先于当前章中的首个节")
            if level == "section":
                has_section = True
    if validate and not chapter_items:
        raise ValueError("确认结构中至少需要一个章级标题")
    return [{
        "num": index + 1, "title": item["title"], "unit_id": item["unit_id"],
        "source_number": item.get("number"),
        "source_paragraph_index": item["source"]["paragraph_index"],
        "range": [item["source"]["paragraph_index"], chapter_items[index + 1]["source"]["paragraph_index"] if index + 1 < len(chapter_items) else end],
        "confidence": item["confidence"], "evidence": list(item["evidence"]),
    } for index, item in enumerate(chapter_items)]


from pipeline.object_bindings import infer_caption, infer_english_caption, recognize_object_bindings
