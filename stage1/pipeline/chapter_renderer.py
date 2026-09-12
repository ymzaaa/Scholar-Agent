# -*- coding: utf-8 -*-
"""带内容单元追踪的通用章节正文渲染器。"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

from models.render_trace import RenderTrace, fragment_hash
from pipeline.object_renderer import render_content_table, render_figure, render_table
from pipeline.inline_renderer import build_bookmark_index, render_inline_tokens
from pipeline.text_processing import escape_text


##### 追踪标记板块 #####


TRACE_BEGIN = "% SCHOLAR_UNIT_BEGIN"
TRACE_END = "% SCHOLAR_UNIT_END"


def _wrap_trace(unit_id: str, role: str, fragment: str) -> str:
    return f"{TRACE_BEGIN} {unit_id} {role}\n{fragment}\n{TRACE_END} {unit_id}"


##### 文本转换板块 #####


def _render_inline_sequence(
    unit: dict[str, Any],
    by_id: dict[str, dict[str, Any]],
    cite_map: dict,
    stats: dict,
    asset_manifest: dict[str, dict[str, Any]],
    bookmark_index: dict[str, Any],
    citation_decisions: dict[str, dict[str, Any]],
    bound_object_ids: set[str],
) -> tuple[str, dict[str, Any]]:
    tokens = unit.get("payload", {}).get("inline_tokens", [])
    if not tokens:
        tokens = [{"kind": "text", "text": unit.get("text", "")}]
    tokens = [token for token in tokens if not (
        token.get("kind") == "image" and token.get("unit_id") in bound_object_ids
    )]
    return render_inline_tokens(
        tokens, by_id, cite_map, stats, asset_manifest, bookmark_index,
        citation_decisions=citation_decisions,
    )


def _bookmark_anchors(
    tokens: list[dict[str, Any]],
    by_id: dict[str, dict[str, Any]],
    bookmark_index: dict[str, Any],
) -> str:
    labels = bookmark_index.get("labels", {})
    anchors = []
    for token in tokens:
        if token.get("kind") != "bookmark_start":
            continue
        name = by_id.get(token.get("unit_id"), {}).get("properties", {}).get("name")
        if name in labels:
            anchors.append(rf"\label{{{labels[name]}}}")
    return "\n".join(anchors)


##### 追踪详情板块 #####


def _child_details(
    children: list[dict],
    cite_map: dict,
    citation_decisions: dict[str, dict[str, Any]] | None = None,
) -> dict:
    formulas = []
    citations = []
    for child in children:
        if child["unit_type"] == "formula":
            formulas.append({
                "unit_id": child["unit_id"],
                "source_syntax": child.get("payload", {}).get("source_syntax"),
                "status": child.get("status"),
            })
        elif child["unit_type"] == "citation":
            numbers = child.get("payload", {}).get("numbers", [])
            keys = [cite_map[number] for number in numbers if number in cite_map]
            decision = (citation_decisions or {}).get(child["unit_id"], {})
            citations.append({
                "unit_id": child["unit_id"],
                "numbers": numbers,
                "keys": keys,
                "decision": decision.get("decision", "unconfirmed"),
                "resolved": (
                    decision.get("decision") == "body_citation"
                    and len(keys) == len(numbers)
                    and bool(numbers)
                ),
            })
    return {"formulas": formulas, "citations": citations}


def _add_record(
    trace: RenderTrace,
    paragraph_unit: dict,
    children: list[dict],
    role: str,
    status: str,
    target_file: str,
    fragment: str | None,
    transformations: list[str],
    cite_map: dict,
    extra_source_ids: list[str] | None = None,
    extra_details: dict | None = None,
    citation_decisions: dict[str, dict[str, Any]] | None = None,
    hash_fragment: str | None = None,
) -> None:
    source_ids = [paragraph_unit["unit_id"]]
    source_ids.extend(child["unit_id"] for child in children)
    source_ids.extend(extra_source_ids or [])
    trace.add(
        marker_unit_id=paragraph_unit["unit_id"],
        source_unit_ids=list(dict.fromkeys(source_ids)),
        source_order=paragraph_unit["order"],
        role=role,
        status=status,
        target_file=target_file,
        transformations=transformations,
        details={
            **_child_details(children, cite_map, citation_decisions),
            **(extra_details or {}),
        },
        output_hash=fragment_hash(hash_fragment if hash_fragment is not None else fragment) if fragment is not None else None,
    )


##### 章节渲染板块 #####


def render_chapter_files(
    *,
    chapters: list[dict],
    object_bindings: list[dict],
    paragraphs: list[dict],
    content_units: list[dict],
    cite_map: dict,
    contents_dir: str | Path,
    stats: dict,
    source_schema_version: str,
    asset_manifest: dict[str, dict[str, Any]] | None = None,
    confirmed_headings: dict[str, dict[str, Any]] | None = None,
    template_adapter: Any,
    content_directory: str,
    citation_decisions: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[str], dict]:
    """只消费确认标题和题注到对象的映射，不运行结构识别。"""
    contents = Path(contents_dir)
    by_id = {unit["unit_id"]: unit for unit in content_units}
    children_by_parent: dict[str, list[dict]] = defaultdict(list)
    for content_unit in content_units:
        parent_id = content_unit.get("relations", {}).get("parent_unit_id")
        if parent_id:
            children_by_parent[parent_id].append(content_unit)
    def descendants(unit_id: str) -> list[dict]:
        result = []
        for child in children_by_parent.get(unit_id, []):
            result.append(child)
            result.extend(descendants(child["unit_id"]))
        return result

    descendants_by_parent = {unit_id: descendants(unit_id) for unit_id in by_id}
    binding_by_caption = {
        item["caption_unit_id"]: item for item in object_bindings if item.get("caption_unit_id")
    }
    if any(item.get("status") != "bound" or not item.get("object_unit_ids") for item in object_bindings):
        raise RuntimeError("题注尚未绑定确认对象，不能进入章节渲染。")
    bound_object_ids = {
        object_id for item in object_bindings
        for object_id in item.get("object_unit_ids", [])
    }
    unbound_content_tables = sorted(
        (
            unit for unit in content_units
            if unit.get("unit_type") == "table"
            and unit.get("properties", {}).get("semantic_role") == "data"
            and unit.get("unit_id") not in bound_object_ids
        ),
        key=lambda unit: unit.get("source", {}).get("block_index", -1),
    )
    english_caption_unit_ids = {
        item["caption_en_unit_id"] for item in object_bindings
        if item.get("caption_en_unit_id")
    }
    trace = RenderTrace(source_schema_version=source_schema_version)
    asset_manifest = asset_manifest or {}
    confirmed_headings = confirmed_headings or {}
    citation_decisions = citation_decisions or {}
    bookmark_index = build_bookmark_index(content_units)
    chapter_files = []
    heading_records: list[dict[str, Any]] = []

    for chapter in chapters:
        lines: list[str] = []
        heading_written = False
        filename = f'section_{chapter["num"]:02d}.tex'
        target_file = f"{content_directory}/{filename}"
        start, end = chapter["range"]
        start_block = paragraphs[start].get("block_index", -1)
        end_block = (
            paragraphs[end].get("block_index", float("inf"))
            if end < len(paragraphs) else float("inf")
        )
        pending_tables = [
            unit for unit in unbound_content_tables
            if start_block <= unit.get("source", {}).get("block_index", -1) < end_block
        ]
        table_cursor = 0

        def render_pending_tables(before_block: float) -> None:
            nonlocal table_cursor
            while table_cursor < len(pending_tables):
                table_unit = pending_tables[table_cursor]
                block = table_unit.get("source", {}).get("block_index", -1)
                if block >= before_block:
                    break
                fragment, details, rendered = render_content_table(
                    table_unit, by_id, asset_manifest, cite_map, stats, bookmark_index,
                    template_adapter=template_adapter, citation_decisions=citation_decisions,
                )
                lines.extend([
                    "", _wrap_trace(table_unit["unit_id"], "content_table", fragment), "",
                ])
                if rendered:
                    stats["tables"] += 1
                _add_record(
                    trace, table_unit, descendants_by_parent.get(table_unit["unit_id"], []),
                    "content_table", "rendered" if rendered else "failed",
                    target_file, fragment, ["word_block_order", "no_invented_caption"],
                    cite_map, extra_details=details, citation_decisions=citation_decisions,
                )
                table_cursor += 1

        for index in range(start, min(end, len(paragraphs))):
            paragraph = paragraphs[index]
            render_pending_tables(paragraph.get("block_index", float("inf")))
            unit_id = paragraph.get("unit_id")
            if not unit_id or unit_id not in by_id:
                raise RuntimeError(f"段落缺少可追踪内容单元：paragraph_index={index}")
            unit = by_id[unit_id]
            children = list(descendants_by_parent.get(unit_id, []))
            text = paragraph["text"].strip()
            tokens = unit.get("payload", {}).get("inline_tokens", [])
            has_semantic = any(
                token.get("kind") in {"formula", "footnote", "field", "hyperlink", "text_box", "image"}
                and token.get("unit_id") not in bound_object_ids
                for token in tokens
            )
            if not text and not has_semantic:
                _add_record(trace, unit, children, "empty_paragraph", "skipped_empty", target_file, None, [], cite_map)
                continue

            if unit_id in english_caption_unit_ids:
                _add_record(
                    trace, unit, children, "english_caption", "bound_to_caption",
                    target_file, None, ["paired_bilingual_caption"], cite_map,
                )
                continue

            confirmed_heading = confirmed_headings.get(unit_id)
            heading = (
                (confirmed_heading["level"], confirmed_heading.get("number"), confirmed_heading["title"])
                if confirmed_heading and confirmed_heading["level"] != "body" else None
            )
            if heading:
                level = heading[0]
                if level == "chapter" and not heading_written:
                    title = chapter["title"]
                    rendered_heading = template_adapter.render_heading(level, escape_text(title), unit_id)
                    fragment = rendered_heading["primary"]
                    anchors = _bookmark_anchors(tokens, by_id, bookmark_index)
                    if anchors:
                        fragment += "\n" + anchors
                    lines.append(_wrap_trace(unit_id, "chapter_heading", fragment))
                    if rendered_heading.get("supplementary"):
                        lines.append(rendered_heading["supplementary"])
                        heading_records.append({
                            "unit_id": unit_id, "level": level,
                            "text_zh": title, "text_en": None,
                            "translation_status": "missing",
                            "target_file": target_file,
                        })
                    heading_written = True
                    _add_record(trace, unit, children, "chapter_heading", "rendered", target_file, fragment, ["heading_number_generated"], cite_map)
                    continue
                if level in {"section", "subsection"}:
                    title = heading[2]
                    rendered_heading = template_adapter.render_heading(level, escape_text(title), unit_id)
                    fragment = rendered_heading["primary"]
                    anchors = _bookmark_anchors(tokens, by_id, bookmark_index)
                    if anchors:
                        fragment += "\n" + anchors
                    lines.append(_wrap_trace(unit_id, f"{level}_heading", fragment))
                    if rendered_heading.get("supplementary"):
                        lines.append(rendered_heading["supplementary"])
                        heading_records.append({
                            "unit_id": unit_id, "level": level,
                            "text_zh": title, "text_en": None,
                            "translation_status": "missing",
                            "target_file": target_file,
                        })
                    lines.append("")
                    _add_record(trace, unit, children, f"{level}_heading", "rendered", target_file, fragment, ["heading_number_generated"], cite_map)
                    continue

            binding = binding_by_caption.get(unit_id, {})
            sources = list(binding.get("object_unit_ids", []))
            if binding.get("caption_en_unit_id"):
                sources.append(binding["caption_en_unit_id"])
            for source_id in sources:
                children.extend(descendants_by_parent.get(source_id, []))
            if binding.get("kind") == "figure":
                if binding.get("status") == "bound":
                    fragment, details, rendered = render_figure(
                        binding["number"], binding["caption_zh"], binding,
                        asset_manifest, caption_en=binding.get("caption_en", ""),
                        template_adapter=template_adapter,
                    )
                    anchors = _bookmark_anchors(tokens, by_id, bookmark_index)
                    if anchors:
                        fragment = anchors + "\n" + fragment
                    lines.extend(["", _wrap_trace(unit_id, "figure_caption", fragment), ""])
                    if rendered:
                        stats["figures"] += 1
                    _add_record(trace, unit, children, "figure_caption", "rendered" if rendered else "failed", target_file, fragment, ["caption_to_word_relationship_images"], cite_map, sources, details, citation_decisions,
                        hash_fragment=fragment if binding.get("caption_en_unit_id") else template_adapter.mask_language_slots(fragment))
                else:
                    _add_record(trace, unit, children, "figure_caption", "failed", target_file, None, [], cite_map)
                continue

            if binding.get("kind") == "table":
                table_ids = binding.get("object_unit_ids", [])
                table_unit = by_id.get(table_ids[0]) if table_ids else None
                if binding.get("status") == "bound" and table_unit:
                    fragment, details, rendered = render_table(
                        binding["number"], binding["caption_zh"], table_unit, by_id,
                        asset_manifest, cite_map, stats, bookmark_index,
                        caption_en=binding.get("caption_en", ""),
                        template_adapter=template_adapter, citation_decisions=citation_decisions,
                    )
                    anchors = _bookmark_anchors(tokens, by_id, bookmark_index)
                    if anchors:
                        fragment = anchors + "\n" + fragment
                    lines.extend(["", _wrap_trace(unit_id, "table_caption", fragment), ""])
                    if rendered:
                        stats["tables"] += 1
                    _add_record(trace, unit, children, "table_caption", "rendered" if rendered else "failed", target_file, fragment, ["word_xml_table_merges"], cite_map, sources, details, citation_decisions,
                        hash_fragment=fragment if binding.get("caption_en_unit_id") else template_adapter.mask_language_slots(fragment))
                else:
                    _add_record(trace, unit, children, "table_caption", "failed", target_file, None, [], cite_map)
                continue

            fragment, semantic_details = _render_inline_sequence(
                unit, by_id, cite_map, stats, asset_manifest, bookmark_index,
                citation_decisions, bound_object_ids,
            )
            children = [child for child in children if child["unit_id"] not in bound_object_ids]
            transformations = ["latex_escape"]
            if unit.get("properties", {}).get("semantic_role") == "equation_explanation":
                fragment = rf"\noindent {fragment}"
                transformations.append("preserve_zero_first_line_indent")
                semantic_details["paragraph_role"] = "equation_explanation"
            if any(
                child["unit_type"] == "citation"
                and citation_decisions.get(child["unit_id"], {}).get("decision")
                == "body_citation"
                for child in children
            ):
                transformations.append("citation_to_bibtex")
            if any(child["unit_type"] == "formula" for child in children):
                transformations.append("formula_preserved")
            lines.extend([_wrap_trace(unit_id, "body_paragraph", fragment), ""])
            _add_record(
                trace, unit, children, "body_paragraph", "rendered", target_file,
                fragment, transformations, cite_map,
                extra_details=semantic_details,
                citation_decisions=citation_decisions,
            )

        render_pending_tables(end_block)

        content = "\n".join(lines)
        path = contents / filename
        path.write_text(content, encoding="utf-8")
        chapter_files.append(target_file)

    trace_data = trace.to_dict()
    trace_data["asset_manifest"] = asset_manifest
    trace_data["bookmark_index"] = bookmark_index
    trace_data["headings"] = heading_records
    return chapter_files, trace_data
