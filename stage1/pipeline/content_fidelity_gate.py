# -*- coding: utf-8 -*-
"""G3-A：基于内容单元标记和渲染账本的正文保真门禁。"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from models.render_trace import fragment_hash
from pipeline.text_processing import escape_text
from pipeline.chapter_renderer import TRACE_BEGIN, TRACE_END
from pipeline.source_content_check import check_body_text, check_table_text, ordered_token_ids, strip_latex_comments


##### 指标与范围板块 #####


def _metric(source: int, covered: int, *, status: str | None = None, detail: str = ""):
    if source == 0:
        return {
            "source": 0,
            "covered": 0,
            "coverage": None,
            "status": status or "not_applicable",
            "detail": detail,
        }
    return {
        "source": source,
        "covered": covered,
        "coverage": round(covered / source, 6),
        "status": status or ("pass" if source == covered else "fail"),
        "detail": detail,
    }


def _body_paragraph_indexes(chapters: list[dict]) -> list[int]:
    indexes = []
    for chapter in chapters:
        start, end = chapter["range"]
        indexes.extend(range(start, end))
    return indexes


def _body_block_ranges(paragraphs: list[dict], chapters: list[dict]) -> list[tuple[int, int]]:
    ranges = []
    for chapter in chapters:
        start, end = chapter["range"]
        if start >= len(paragraphs) or end <= start:
            continue
        end_block = paragraphs[end]["block_index"] if end < len(paragraphs) else float("inf")
        ranges.append((paragraphs[start]["block_index"], end_block))
    return ranges


def _in_block_ranges(block_index: int | None, ranges: list[tuple[int, int]]) -> bool:
    return block_index is not None and any(start <= block_index < end for start, end in ranges)


##### LaTeX 标记解析板块 #####


_BEGIN_PATTERN = re.compile(rf"^{re.escape(TRACE_BEGIN)}\s+(u-\d{{6}})\s+([a-z_]+)\s*$")
_END_PATTERN = re.compile(rf"^{re.escape(TRACE_END)}\s+(u-\d{{6}})\s*$")
_DERIVED_HEADING = re.compile(r"^% SCHOLAR_TEMPLATE_HEADING_EN (u-\d{6}) (chapter|section|subsection)$")


def _parse_marked_files(output_dir: str | Path, chapter_files: list[str], allowed_headings=None):
    segments: dict[str, dict[str, Any]] = {}
    order = []
    unexpected = []
    derived_seen = []
    for relative in chapter_files:
        path = Path(output_dir) / relative
        lines = path.read_text(encoding="utf-8").splitlines()
        active_id = None
        active_role = None
        buffer: list[str] = []
        pending_heading = None
        for line_number, line in enumerate(lines, start=1):
            if active_id is None and allowed_headings is not None:
                if pending_heading:
                    unit_id, level = pending_heading
                    expected = allowed_headings.get(unit_id, {})
                    if (expected.get("level") == level and expected.get("target_file") == relative
                        and re.fullmatch(r"\\en" + level + r"\{.*\}", line.strip())):
                        derived_seen.append(unit_id)
                        pending_heading = None
                        continue
                    unexpected.append(f"英文标题插槽不匹配：{relative}:{unit_id}")
                    pending_heading = None
                derived = _DERIVED_HEADING.fullmatch(line.strip())
                if derived:
                    pending_heading = derived.groups()
                    continue
            begin = _BEGIN_PATTERN.match(line)
            end = _END_PATTERN.match(line)
            if begin:
                if active_id is not None or begin.group(1) in segments:
                    raise ValueError(f"追踪标记嵌套或重复：{relative}:{line_number}")
                active_id, active_role = begin.group(1), begin.group(2)
                buffer = []
                continue
            if end:
                if active_id is None or end.group(1) != active_id:
                    raise ValueError(f"追踪结束标记不匹配：{relative}:{line_number}")
                fragment = "\n".join(buffer)
                segments[active_id] = {
                    "role": active_role,
                    "fragment": fragment,
                    "target_file": relative,
                }
                order.append(active_id)
                active_id = None
                active_role = None
                buffer = []
                continue
            if active_id is not None:
                buffer.append(line)
            elif line.strip() and not line.lstrip().startswith("%"):
                unexpected.append(f"{relative}:{line_number}:{line.strip()[:80]}")
        if active_id is not None:
            raise ValueError(f"追踪标记未闭合：{relative}:{active_id}")
    if allowed_headings is not None and Counter(derived_seen) != Counter(allowed_headings.keys()):
        unexpected.append("英文标题插槽与确认标题没有逐一对应。")
    return segments, order, unexpected


##### 正式门禁板块 #####


def build_content_fidelity_report(
    extract_result: Any,
    recognize_result: Any,
    render_result: Any,
) -> dict[str, Any]:
    units = extract_result.content_units
    by_id = {unit["unit_id"]: unit for unit in units}
    paragraphs = extract_result.paragraphs
    chapters = recognize_result.review["chapters"]
    bindings = recognize_result.review.get("object_bindings", [])
    english_owners = {item["caption_en_unit_id"]: item["caption_unit_id"] for item in bindings if item.get("caption_en_unit_id")}
    bound_objects = {unit_id for item in bindings for unit_id in item.get("object_unit_ids", [])}
    paragraph_indexes = _body_paragraph_indexes(chapters)
    body_paragraphs = [
        paragraphs[index]
        for index in paragraph_indexes
        if index < len(paragraphs)
    ]
    expected = [
        item for item in body_paragraphs
        if item.get("text", "").strip()
        or any(
            token.get("kind") in {
                "formula", "footnote", "field", "hyperlink", "text_box", "image"
            }
            and token.get("unit_id") not in bound_objects
            for token in by_id.get(item.get("unit_id"), {}).get(
                "payload", {}
            ).get("inline_tokens", [])
        )
    ]
    expected_ids = [item["unit_id"] for item in expected]
    records = render_result.render_trace["records"]
    records_by_marker = {record["marker_unit_id"]: record for record in records}
    heading_sources = {item["unit_id"]: item for item in recognize_result.review["heading_candidates"] if item["level"] != "body"}
    expected_files = {}
    for chapter, relative in zip(chapters, render_result.chapter_files):
        start, end = chapter["range"]
        expected_files.update({item["unit_id"]: relative for item in paragraphs[start:end]})
    derived_expected = {
        unit_id: {**item, "target_file": expected_files[unit_id]}
        for unit_id, item in heading_sources.items()
        if render_result.template_adapter.render_heading(item["level"], item["title"], unit_id).get("supplementary")
    }
    derived_records = render_result.render_trace.get("headings", [])
    derived_records_ok = Counter(item["unit_id"] for item in derived_records) == Counter(derived_expected.keys())
    derived_records_ok = derived_records_ok and all(
        item.get("level") == derived_expected.get(item["unit_id"], {}).get("level")
        and item.get("text_zh") == derived_expected.get(item["unit_id"], {}).get("title")
        and item.get("target_file") == derived_expected.get(item["unit_id"], {}).get("target_file")
        for item in derived_records
    )

    parse_error = None
    try:
        segments, actual_order, unexpected = _parse_marked_files(
            render_result.output_dir, render_result.chapter_files, derived_expected,
        )
        if render_result.render_trace.get("approved_changes"):
            from agents.quality_repair.patching import project_approved_fragments
            segments = project_approved_fragments(segments, render_result.render_trace["approved_changes"],
                                                 confirmed_replacements=render_result.confirmed_body_replacements)
    except ValueError as exc:
        segments, actual_order, unexpected = {}, [], []
        parse_error = str(exc)

    covered_ids = []
    exact_ids = []
    for paragraph in expected:
        unit_id = paragraph["unit_id"]
        marker_id = english_owners.get(unit_id, unit_id)
        record = records_by_marker.get(marker_id)
        segment = segments.get(marker_id)
        if not record or record["status"] != "rendered" or not segment:
            continue
        if unit_id not in record["source_unit_ids"]:
            continue
        covered_ids.append(unit_id)
        if record["role"] in {"figure_caption", "table_caption"}:
            continue
        if record.get("output_hash") == fragment_hash(segment["fragment"]):
            exact_ids.append(unit_id)

    duplicate_record_ids = [
        unit_id
        for unit_id in set(record["marker_unit_id"] for record in records)
        if sum(1 for record in records if record["marker_unit_id"] == unit_id) > 1
    ]
    rendered_expected_order = [unit_id for unit_id in actual_order if unit_id in set(expected_ids)]
    order_ok = rendered_expected_order == [unit_id for unit_id in expected_ids if unit_id not in english_owners]

    block_ranges = _body_block_ranges(paragraphs, chapters)
    body_parent_ids = set(expected_ids) | {
        unit["unit_id"] for unit in units if unit["unit_type"] == "table"
        and _in_block_ranges(unit.get("source", {}).get("block_index"), block_ranges)
    }
    pending = True
    while pending:
        descendants = {unit["unit_id"] for unit in units
                       if unit.get("relations", {}).get("parent_unit_id") in body_parent_ids}
        pending = bool(descendants - body_parent_ids)
        body_parent_ids.update(descendants)
    bibliography = render_result.render_trace.get("bibliography")
    classified_body_ids = set(
        bibliography.get("body_citation_unit_ids", []) if bibliography else []
    )
    body_citations = [
        unit
        for unit in units
        if unit["unit_type"] == "citation"
        and unit.get("relations", {}).get("parent_unit_id") in body_parent_ids
        and (not bibliography or unit["unit_id"] in classified_body_ids)
    ]
    body_latex_formulas = [
        unit
        for unit in units
        if unit["unit_type"] == "formula"
        and unit.get("payload", {}).get("source_syntax") == "latex"
        and unit.get("relations", {}).get("parent_unit_id") in body_parent_ids
    ]
    traced_child_ids = {
        source_id
        for record in records
        if record["status"] == "rendered"
        for source_id in record["source_unit_ids"]
    }
    citation_resolution = {
        item["unit_id"]: item["resolved"]
        for record in records
        for item in record.get("details", {}).get("citations", [])
    }
    rendered_citation_ids = [unit_id for record in records
        for unit_id in record.get("details", {}).get("rendered_citation_ids", [])]
    covered_citations = sum(
        1
        for unit in body_citations
        if unit["unit_id"] in traced_child_ids and citation_resolution.get(unit["unit_id"])
        and rendered_citation_ids.count(unit["unit_id"]) == 1
    )
    block_ranges = _body_block_ranges(paragraphs, chapters)
    body_images = [
        unit for unit in units
        if unit["unit_type"] == "image"
        and _in_block_ranges(unit.get("source", {}).get("block_index"), block_ranges)
    ]
    body_tables = [
        unit for unit in units
        if unit["unit_type"] == "table"
        and unit.get("properties", {}).get("semantic_role") == "data"
        and _in_block_ranges(unit.get("source", {}).get("block_index"), block_ranges)
    ]
    body_table_ids = {unit["unit_id"] for unit in body_tables}
    body_omml_formulas = [
        unit
        for unit in units
        if unit["unit_type"] == "formula"
        and unit.get("payload", {}).get("source_syntax") == "omml"
        and unit.get("relations", {}).get("parent_unit_id")
        in body_parent_ids | body_table_ids
    ]
    rendered_formula_ids = [
        unit_id
        for record in records
        for unit_id in record.get("details", {}).get("rendered_formula_ids", [])
    ]
    rendered_display_formula_ids = [
        unit_id
        for record in records
        for unit_id in record.get("details", {}).get(
            "rendered_display_formula_ids", []
        )
    ]
    root_units = [by_id[item["unit_id"]] for item in expected if item["unit_id"] not in english_owners]
    root_units.extend(unit for unit in body_tables if unit["unit_id"] not in bound_objects)
    root_units.sort(key=lambda unit: unit.get("source", {}).get("block_index", -1))
    binding_by_caption = {item["caption_unit_id"]: item for item in bindings}
    # 从确认结构和抽取来源重建账本契约，不能信任账本自行声明的角色和归属。
    contract_errors = []
    for table in body_tables:
        for chapter, relative in zip(chapters, render_result.chapter_files):
            if _in_block_ranges(table.get("source", {}).get("block_index"),
                                _body_block_ranges(paragraphs, [chapter])):
                expected_files[table["unit_id"]] = relative
    expected_markers = {unit["unit_id"] for unit in root_units}
    if set(segments) != expected_markers:
        contract_errors.append("实际片段与确认正文根单元不一致")
    for unit_id, segment in segments.items():
        record = records_by_marker.get(unit_id, {})
        binding = binding_by_caption.get(unit_id, {})
        heading = heading_sources.get(unit_id)
        role = (f"{heading['level']}_heading" if heading else
                f"{binding['kind']}_caption" if binding else
                "content_table" if unit_id in body_table_ids else "body_paragraph")
        source_ids = {unit_id, *binding.get("object_unit_ids", [])}
        if binding.get("caption_en_unit_id"):
            source_ids.add(binding["caption_en_unit_id"])
        while True:
            descendants = {item["unit_id"] for item in units
                           if item.get("relations", {}).get("parent_unit_id") in source_ids}
            if not descendants - source_ids:
                break
            source_ids.update(descendants)
        if role == "body_paragraph":
            source_ids.difference_update(bound_objects)
        assets = render_result.render_trace.get('asset_manifest', {})
        expected_images = [assets.get(source_id, {}).get('relative_path') for source_id in source_ids
                           if by_id.get(source_id, {}).get('unit_type') == 'image']
        actual_images = re.findall(r'\\includegraphics(?:\[[^\]]*\])?\{([^{}]+)\}',
                                   strip_latex_comments(segment['fragment']))
        if Counter(expected_images) != Counter(actual_images):
            contract_errors.append(f'{unit_id}: 图片实际出现次数或来源不一致')
        actual_fragment = segment['fragment']
        if binding and not binding.get('caption_en_unit_id'):
            actual_fragment = render_result.template_adapter.mask_language_slots(actual_fragment)
        if record.get('output_hash') != fragment_hash(actual_fragment):
            contract_errors.append(f'{unit_id}: 输出片段与账本不一致')
        if binding:
            caption_command = render_result.template_adapter.render_caption(binding['kind'],
                escape_text(binding['caption_zh']), escape_text(binding.get('caption_en', '')))
            if not binding.get('caption_en_unit_id'):
                caption_command = render_result.template_adapter.mask_language_slots(caption_command)
            if strip_latex_comments(actual_fragment).count(caption_command) != 1:
                contract_errors.append(f'{unit_id}: 确认题注文字不一致')
        if (record.get("status") != "rendered"
            or record.get("role") != role or segment["role"] != role
            or record.get("target_file") != expected_files.get(unit_id)
            or segment["target_file"] != expected_files.get(unit_id)
            or Counter(record.get("source_unit_ids", [])) != Counter(source_ids)):
            contract_errors.append(unit_id)
    formula_roots = []
    for unit in root_units:
        formula_roots.append(unit)
        formula_roots.extend(by_id[unit_id] for unit_id in binding_by_caption.get(unit["unit_id"], {}).get("object_unit_ids", []))
    body_formula_ids = [unit_id for unit in formula_roots for unit_id in ordered_token_ids(unit, by_id, "formula")]
    body_formulas = [by_id[unit_id] for unit_id in body_formula_ids]
    body_formula_id_set = set(body_formula_ids)
    actual_formula_ids = [
        unit_id
        for record in records
        if record.get("status") == "rendered"
        for unit_id in record.get("details", {}).get("rendered_formula_ids", [])
        if unit_id in body_formula_id_set
    ]
    formula_order_ok = actual_formula_ids == body_formula_ids
    formula_order_detail = (
        f"公式 token 顺序一致，共 {len(body_formula_ids)} 项。"
        if formula_order_ok else
        f"expected_count={len(body_formula_ids)}, actual_count={len(actual_formula_ids)}, "
        f"expected_head={body_formula_ids[:20]}, actual_head={actual_formula_ids[:20]}"
    )
    covered_formulas = sum(
        unit.get("status") == "extracted"
        and unit["unit_id"] in rendered_formula_ids
        for unit in body_formulas
    )
    covered_latex_formulas = sum(
        unit.get("status") == "extracted"
        and unit["unit_id"] in rendered_formula_ids
        for unit in body_latex_formulas
    )
    display_formulas = [
        unit for unit in body_formulas
        if unit.get("properties", {}).get("display")
    ]
    covered_display_formulas = sum(
        unit["unit_id"] in rendered_display_formula_ids
        for unit in display_formulas
    )
    verified_image_ids = [
        unit_id
        for record in records
        if record.get("status") == "rendered"
        for unit_id in record.get("details", {}).get("verified_image_unit_ids", [])
    ]
    source_cells = [
        cell
        for table in body_tables
        for row in table.get("payload", {}).get("physical_rows", [])
        for cell in row.get("cells", [])
    ]
    rendered_cell_ids = [
        cell_id
        for record in records
        if record.get("status") == "rendered"
        for cell_id in record.get("details", {}).get("rendered_cell_ids", [])
    ]
    covered_omml = sum(
        1 for unit in body_omml_formulas
        if unit.get("status") == "extracted" and unit["unit_id"] in rendered_formula_ids
    )
    covered_images = sum(
        1 for unit in body_images if unit["unit_id"] in verified_image_ids
    )
    covered_cells = sum(
        1 for cell in source_cells if cell["cell_id"] in rendered_cell_ids
    )
    semantic_types = {"footnote", "field", "hyperlink", "text_box"}
    body_semantic_units = [
        unit for unit in units
        if unit.get("unit_type") in semantic_types
        and _in_block_ranges(unit.get("source", {}).get("block_index"), block_ranges)
    ]
    semantic_by_type = {
        unit_type: [unit for unit in body_semantic_units if unit["unit_type"] == unit_type]
        for unit_type in semantic_types
    }
    detail_sets = {}
    for key in (
        "rendered_footnote_ids", "rendered_field_ids",
        "resolved_cross_reference_ids", "rendered_hyperlink_ids",
        "rendered_text_box_ids", "text_box_layout_degraded_ids",
    ):
        detail_sets[key] = Counter(
            unit_id
            for record in records
            for unit_id in record.get("details", {}).get(key, [])
        )
    cross_reference_units = [
        unit for unit in semantic_by_type["field"]
        if unit.get("properties", {}).get("field_type") in {"REF", "PAGEREF"}
    ]
    bookmark_index = render_result.render_trace.get("bookmark_index", {})
    citation_transaction = bibliography.get("transaction_status") if bibliography else None
    literal_citation_fallback = citation_transaction == "rolled_back"
    checked_text, text_errors = check_body_text(
        expected, by_id, segments, recognize_result.review, render_result.render_trace,
    )
    cell_errors = check_table_text(body_tables, by_id, segments, recognize_result.review, render_result.render_trace)
    occurrences_ok = (
        Counter(unit["unit_id"] for unit in body_images) == Counter(verified_image_ids)
        and Counter(cell["cell_id"] for cell in source_cells) == Counter(rendered_cell_ids)
        and Counter(body_formula_ids) == Counter(rendered_formula_ids)
    )
    for kind, key in {'footnote': 'rendered_footnote_ids', 'field': 'rendered_field_ids',
                      'hyperlink': 'rendered_hyperlink_ids', 'text_box': 'rendered_text_box_ids'}.items():
        occurrences_ok = occurrences_ok and Counter(unit['unit_id'] for unit in semantic_by_type[kind]) == detail_sets[key]
    metrics = {
        "render_source_contract": _metric(1, int(not contract_errors), detail=f"角色、来源和目标文件核对：{contract_errors}"),
        "derived_heading_sources": _metric(1, int(derived_records_ok), detail="派生英文标题必须与确认标题、来源和目标文件逐一对应。"),
        "cell_content_integrity": _metric(1, int(not cell_errors), detail=f"物理单元格内容核对：{cell_errors}"),
        "object_occurrences": _metric(1, int(occurrences_ok), detail="源对象与实际渲染账本逐次对应，不允许重复或无来源对象。"),
        "plain_text_integrity": _metric(checked_text, checked_text - len(text_errors),
            status="fail" if text_errors else "pass", detail=f"逐项来源文字核对：{text_errors}"),
        "body_paragraph_coverage": _metric(len(expected_ids), len(set(covered_ids)), detail="正文非空段落必须恰好进入一个受控渲染片段。"),
        "render_order": _metric(len(expected_ids), len(expected_ids) if order_ok else 0, detail="最终章节中的 unit_id 顺序必须与 Word 章节范围一致。"),
        "duplicate_units": _metric(0, 0, status="pass" if not duplicate_record_ids else "fail", detail=f"重复单元：{duplicate_record_ids}"),
        "unexpected_output": _metric(0, 0, status="pass" if not unexpected and not parse_error else "fail", detail=parse_error or f"无来源正文：{unexpected[:10]}"),
        "citation_coverage": _metric(
            len(body_citations), covered_citations,
            status="degraded" if literal_citation_fallback and body_citations else None,
            detail=(
                "引用事务未满足完整绑定条件，全部保留 Word 原文字面形式。"
                if literal_citation_fallback else
                "每个数字引用必须映射到完整 BibTeX 键集合。"
            ),
        ),
        "bibliography_source_integrity": _metric(
            1 if bibliography else 0,
            1 if bibliography and bibliography.get("all_citations_resolved") else 0,
            status="degraded" if literal_citation_fallback else None,
            detail=(
                "参考文献数据源不完整；未生成引用命令并保持正文原文。"
                if literal_citation_fallback else
                "参考文献数据源、显式映射、歧义和所有正文编号必须同时有效。"
                if bibliography else "当前兼容调用未提供参考文献计划。"
            ),
        ),
        "latex_formula_coverage": _metric(len(body_latex_formulas), covered_latex_formulas, detail="用户原有 LaTeX 公式必须随父段落进入最终片段。"),
        "omml_formula_coverage": _metric(len(body_omml_formulas), covered_omml, detail="Word 原生公式必须成功转换并按行内顺序进入最终片段。"),
        "formula_coverage": _metric(
            len(body_formulas), covered_formulas,
            detail="所有正文 OMML 与 LaTeX 公式必须通过统一 token 在原位置渲染。",
        ),
        "formula_render_order": _metric(
            len(body_formula_ids),
            len(body_formula_ids) if formula_order_ok else 0,
            detail=formula_order_detail,
        ),
        "display_formula_coverage": _metric(
            len(display_formulas), covered_display_formulas,
            detail="所有行间公式必须由明确 display 属性渲染为受控 equation 环境。",
        ),
        "image_object_coverage": _metric(len(body_images), covered_images, detail="正文图片必须来自 Word relationship，且源与输出二进制哈希一致。"),
        "table_cell_coverage": _metric(len(source_cells), covered_cells, detail="逐个物理单元格核验内容、网格跨度与纵向合并渲染。"),
        "footnote_coverage": _metric(
            len(semantic_by_type["footnote"]),
            sum(unit["unit_id"] in detail_sets["rendered_footnote_ids"] for unit in semantic_by_type["footnote"]),
            detail="每个正文脚注引用必须找到脚注正文并在原位置生成footnote。",
        ),
        "field_semantic_coverage": _metric(
            len(semantic_by_type["field"]),
            sum(unit["unit_id"] in detail_sets["rendered_field_ids"] for unit in semantic_by_type["field"]),
            detail="REF、PAGEREF和HYPERLINK域必须安全转换；其他域明确阻断。",
        ),
        "cross_reference_target_coverage": _metric(
            len(cross_reference_units),
            sum(unit["unit_id"] in detail_sets["resolved_cross_reference_ids"] for unit in cross_reference_units),
            status=(
                "fail"
                if bookmark_index.get("missing_names") or bookmark_index.get("duplicate_names")
                else None
            ),
            detail=(
                f"missing={bookmark_index.get('missing_names', [])}, "
                f"duplicate={bookmark_index.get('duplicate_names', [])}"
            ),
        ),
        "hyperlink_coverage": _metric(
            len(semantic_by_type["hyperlink"]),
            sum(unit["unit_id"] in detail_sets["rendered_hyperlink_ids"] for unit in semantic_by_type["hyperlink"]),
            detail="外部链接只允许http/https/mailto；内部链接必须解析到唯一书签。",
        ),
        "text_box_content_coverage": _metric(
            len(semantic_by_type["text_box"]),
            sum(unit["unit_id"] in detail_sets["rendered_text_box_ids"] for unit in semantic_by_type["text_box"]),
            detail="文本框必须作为独立对象在锚点位置渲染，不能混入父段落。",
        ),
        "text_box_layout_equivalence": _metric(
            len(semantic_by_type["text_box"]),
            0,
            status="degraded" if semantic_by_type["text_box"] else None,
            detail="首版保证内容，不保证Word绝对定位、环绕和旋转等价。",
        ),

    }
    blocking = [
        name for name, metric in metrics.items()
        if metric["status"] in {"fail", "not_implemented"}
    ]
    return {
        "report_version": "4.1.0",
        "scope": "recognized_body_to_latex",
        "source_schema_version": extract_result.extraction_report.get("schema_version"),
        "source_sha256": extract_result.extraction_report.get("metadata", {}).get("source_sha256"),
        "metrics": metrics,
        "blocking_or_unimplemented": blocking,
        "all_passed": not blocking,
        "summary": {
            "expected_paragraph_ids": len(expected_ids),
            "rendered_paragraph_ids": len(set(covered_ids)),
            "exact_fragment_ids": len(exact_ids),
            "trace_record_count": len(records),
        },
    }


def write_fidelity_artifacts(reports_dir: str | Path, render_trace: dict, report: dict) -> dict:
    """把追踪账本和保真报告写入明确的报告目录。"""
    reports_dir = Path(reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    trace_path = reports_dir / "render_trace.json"
    report_path = reports_dir / "content_fidelity.json"
    trace_path.write_text(json.dumps(render_trace, ensure_ascii=False, indent=2), encoding="utf-8")
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"render_trace": str(trace_path), "content_fidelity": str(report_path)}
