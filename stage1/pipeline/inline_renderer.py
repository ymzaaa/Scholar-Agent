# -*- coding: utf-8 -*-
"""统一渲染正文、单元格、脚注和文本框中的有序行内内容。"""

from __future__ import annotations

from typing import Any

from pipeline.text_processing import escape_text, hyperlink_argument
from content_extraction.word_semantics import bookmark_label, safe_external_url


##### 书签索引板块 #####


def build_bookmark_index(units: list[dict[str, Any]]) -> dict[str, Any]:
    bookmarks: dict[str, list[dict[str, Any]]] = {}
    referenced = set()
    for unit in units:
        if unit.get("unit_type") == "bookmark":
            name = unit.get("properties", {}).get("name", "")
            if name:
                bookmarks.setdefault(name, []).append(unit)
        elif unit.get("unit_type") in {"field", "hyperlink"}:
            target = unit.get("payload", {}).get("target_bookmark")
            if target:
                referenced.add(target)
    labels = {
        name: bookmark_label(name)
        for name in referenced
        if len(bookmarks.get(name, [])) == 1
    }
    return {
        "labels": labels,
        "referenced_names": sorted(referenced),
        "missing_names": sorted(name for name in referenced if name not in bookmarks),
        "duplicate_names": sorted(name for name in referenced if len(bookmarks.get(name, [])) > 1),
    }


##### 详情汇总板块 #####


def _details() -> dict[str, Any]:
    return {
        "rendered_formula_ids": [],
        "rendered_citation_ids": [],
        "rendered_inline_formula_ids": [],
        "rendered_display_formula_ids": [],
        "verified_image_unit_ids": [],
        "footnote_ids": [],
        "rendered_footnote_ids": [],
        "field_ids": [],
        "rendered_field_ids": [],
        "degraded_field_ids": [],
        "cross_reference_ids": [],
        "resolved_cross_reference_ids": [],
        "hyperlink_ids": [],
        "rendered_hyperlink_ids": [],
        "degraded_hyperlink_ids": [],
        "text_box_ids": [],
        "rendered_text_box_ids": [],
        "degraded_text_box_ids": [],
        "text_box_layout_degraded_ids": [],
    }


def _merge_details(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key, value in source.items():
        if isinstance(value, list):
            target[key].extend(value)
        elif isinstance(value, int):
            target[key] += value


##### 语义对象渲染板块 #####


def _render_field(unit: dict, labels: dict[str, str], details: dict) -> str:
    unit_id = unit["unit_id"]
    field_type = unit.get("properties", {}).get("field_type")
    payload = unit.get("payload", {})
    result = escape_text(unit.get("text", ""))
    details["field_ids"].append(unit_id)
    if field_type in {"REF", "PAGEREF"}:
        details["cross_reference_ids"].append(unit_id)
        label = labels.get(payload.get("target_bookmark"))
        if unit.get("status") == "extracted" and label:
            details["rendered_field_ids"].append(unit_id)
            details["resolved_cross_reference_ids"].append(unit_id)
            if field_type == "PAGEREF":
                return rf"\pageref{{{label}}}"
            return rf"\hyperref[{label}]{{{result}}}"
    elif field_type == "HYPERLINK" and unit.get("status") == "extracted":
        target = payload.get("target_bookmark")
        url = payload.get("url")
        if target and target in labels:
            details["rendered_field_ids"].append(unit_id)
            return rf"\hyperref[{labels[target]}]{{{result}}}"
        if safe_external_url(url):
            details["rendered_field_ids"].append(unit_id)
            return rf"\href{{{hyperlink_argument(url)}}}{{{result}}}"
    details["degraded_field_ids"].append(unit_id)
    return result


def _render_hyperlink(unit: dict, labels: dict[str, str], details: dict) -> str:
    unit_id = unit["unit_id"]
    details["hyperlink_ids"].append(unit_id)
    display = escape_text(unit.get("text", ""))
    payload = unit.get("payload", {})
    target = payload.get("target_bookmark")
    url = payload.get("url")
    if unit.get("status") == "extracted" and target in labels:
        details["rendered_hyperlink_ids"].append(unit_id)
        return rf"\hyperref[{labels[target]}]{{{display}}}"
    if unit.get("status") == "extracted" and safe_external_url(url):
        details["rendered_hyperlink_ids"].append(unit_id)
        return rf"\href{{{hyperlink_argument(url)}}}{{{display}}}"
    details["degraded_hyperlink_ids"].append(unit_id)
    return display


def render_inline_tokens(
    tokens: list[dict[str, Any]],
    by_id: dict[str, dict[str, Any]],
    cite_map: dict,
    stats: dict,
    asset_manifest: dict[str, dict[str, Any]],
    bookmark_index: dict[str, Any],
    *,
    context: str = "body",
    citation_decisions: dict[str, dict[str, Any]] | None = None,
) -> tuple[str, dict[str, Any]]:
    parts = []
    details = _details()
    labels = bookmark_index.get("labels", {})
    citation_decisions = citation_decisions or {}
    for token in tokens:
        kind = token.get("kind")
        unit = by_id.get(token.get("unit_id"), {})
        if kind == "text":
            parts.append(escape_text(token.get("text", "")))
        elif kind == "line_break":
            parts.append(r" \\ ")
        elif kind == "formula":
            if unit.get("status") == "extracted" and unit.get("text"):
                if unit.get("properties", {}).get("display") and context == "body":
                    parts.extend(["\n", r"\begin{equation}", "\n", unit["text"], "\n", r"\end{equation}", "\n"])
                    stats["equations"] += 1
                    details["rendered_display_formula_ids"].append(unit["unit_id"])
                else:
                    parts.append(f"${unit['text']}$")
                    details["rendered_inline_formula_ids"].append(unit["unit_id"])
                details["rendered_formula_ids"].append(unit["unit_id"])
        elif kind == "citation":
            unit_id = unit.get("unit_id")
            decision = citation_decisions.get(unit_id, {})
            numbers = unit.get("payload", {}).get("numbers", [])
            keys = [cite_map[number] for number in numbers if number in cite_map]
            resolved = (
                decision.get("decision") == "body_citation"
                and len(keys) == len(numbers)
                and bool(numbers)
            )
            literal = escape_text(unit.get("text", ""))
            if unit.get("payload", {}).get("citation_syntax") == "numeric_superscript":
                literal = rf"\textsuperscript{{{literal}}}"
            parts.append(r"\cite{" + ",".join(keys) + "}" if resolved else literal)
            if resolved:
                stats["citations"] += 1
                details["rendered_citation_ids"].append(unit_id)
        elif kind == "image":
            asset = asset_manifest.get(unit.get("unit_id"), {})
            if asset.get("status") == "verified":
                parts.append(rf"\includegraphics[width=0.9\linewidth]{{{asset['relative_path']}}}")
                details["verified_image_unit_ids"].append(unit["unit_id"])
        elif kind == "bookmark_start":
            name = unit.get("properties", {}).get("name")
            if name in labels:
                parts.append(rf"\label{{{labels[name]}}}")
        elif kind == "field":
            parts.append(_render_field(unit, labels, details))
        elif kind == "hyperlink":
            parts.append(_render_hyperlink(unit, labels, details))
        elif kind == "footnote":
            unit_id = unit.get("unit_id")
            details["footnote_ids"].append(unit_id)
            block_parts = []
            nested_details = _details()
            for block in unit.get("payload", {}).get("blocks", []):
                rendered, child_details = render_inline_tokens(
                    block.get("tokens", []), by_id, cite_map, stats,
                    asset_manifest, bookmark_index, context="footnote",
                    citation_decisions=citation_decisions,
                )
                block_parts.append(rendered)
                _merge_details(nested_details, child_details)
            _merge_details(details, nested_details)
            if unit.get("status") == "extracted":
                parts.append(r"\footnote{" + r"\par ".join(block_parts) + "}")
                details["rendered_footnote_ids"].append(unit_id)
        elif kind == "text_box":
            unit_id = unit.get("unit_id")
            details["text_box_ids"].append(unit_id)
            details["text_box_layout_degraded_ids"].append(unit_id)
            block_parts = []
            nested_details = _details()
            for block in unit.get("payload", {}).get("blocks", []):
                rendered, child_details = render_inline_tokens(
                    block.get("tokens", []), by_id, cite_map, stats,
                    asset_manifest, bookmark_index, context="text_box",
                    citation_decisions=citation_decisions,
                )
                block_parts.append(rendered)
                _merge_details(nested_details, child_details)
            _merge_details(details, nested_details)
            if unit.get("status") == "extracted":
                box = r"\fbox{\parbox{0.92\linewidth}{" + r"\par ".join(block_parts) + "}}"
                parts.append("\n" + r"\begin{quote}\noindent " + box + r"\end{quote}" + "\n")
                details["rendered_text_box_ids"].append(unit_id)
            else:
                details["degraded_text_box_ids"].append(unit_id)
    return "".join(parts), details
