# -*- coding: utf-8 -*-
"""按 Word 块方向建立题注与图片组、数据表的唯一归属。"""

from __future__ import annotations

import re
from typing import Any


##### 题注文字板块 #####


RE_CAPTION = re.compile(
    r"^\s*([图表])\s*([0-9一二两三四五六七八九十]+)\s*[-—–.．]\s*"
    r"([0-9]+)(?:\s+|[：:]\s*)(\S.*)$"
)
RE_ENGLISH_CAPTION = re.compile(
    r"^\s*(Fig(?:ure)?\.|Tab(?:le)?\.)\s*([0-9]+)\s*[-—–.．]\s*"
    r"([0-9]+)(?:\s+|[：:]\s*)(\S.*)$",
    re.IGNORECASE,
)


def infer_caption(text: str) -> dict[str, Any] | None:
    from pipeline.structure_recognizer import normalize_text, chinese_number_to_int

    match = RE_CAPTION.match(normalize_text(text))
    if not match:
        return None
    chapter = chinese_number_to_int(match.group(2))
    if chapter is None:
        return None
    kind = "figure" if match.group(1) == "图" else "table"
    number = f"{match.group(1)}{chapter}-{int(match.group(3))}"
    return {"kind": kind, "number": number, "caption": match.group(4).strip()}


def infer_english_caption(text: str) -> dict[str, Any] | None:
    """识别 Word 已有英文图表题注；只接受带分章编号的明确格式。"""
    from pipeline.structure_recognizer import normalize_text

    match = RE_ENGLISH_CAPTION.match(normalize_text(text))
    if not match:
        return None
    prefix = match.group(1).lower()
    kind = "figure" if prefix.startswith("fig") else "table"
    label = "图" if kind == "figure" else "表"
    return {
        "kind": kind,
        "number": f"{label}{int(match.group(2))}-{int(match.group(3))}",
        "caption": match.group(4).strip(),
    }


def _caption_metadata(
    caption: dict[str, Any], unit: dict[str, Any],
    block_paragraphs: dict[int | None, dict[str, Any]],
) -> dict[str, Any]:
    """把相邻、同编号的英文题注绑定到中文题注；缺失保持显式状态。"""
    block = unit.get("source", {}).get("block_index")
    english_unit = None
    english = None
    if block is not None:
        for offset in (1, 2):
            candidate = block_paragraphs.get(block + offset)
            if not candidate:
                continue
            inferred = infer_english_caption(candidate.get("text", ""))
            if inferred and inferred["kind"] == caption["kind"] and inferred["number"] == caption["number"]:
                english_unit, english = candidate, inferred["caption"]
                break
            if candidate.get("text", "").strip():
                break
    return {
        **caption,
        "caption_zh": caption["caption"],
        "caption_en": english or "",
        "caption_en_unit_id": english_unit.get("unit_id") if english_unit else None,
        "caption_en_source": "word" if english else "missing",
        "caption_en_status": "provided" if english else "missing",
    }



##### 对象分组板块 #####


def recognize_object_bindings(units: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """依据 Word 块顺序绑定题注；一条图题允许对应多个图片内容单元。"""
    paragraphs = {
        unit["unit_id"]: unit for unit in units if unit.get("unit_type") == "paragraph"
    }
    block_paragraphs = {
        unit.get("source", {}).get("block_index"): unit for unit in paragraphs.values()
    }
    images_by_parent: dict[str, list[dict[str, Any]]] = {}
    for image in (unit for unit in units if unit.get("unit_type") == "image"):
        parent_id = image.get("relations", {}).get("parent_unit_id")
        if parent_id in paragraphs:
            images_by_parent.setdefault(parent_id, []).append(image)

    image_groups = []
    for parent_id, images in images_by_parent.items():
        parent = paragraphs[parent_id]
        image_ids = {image["unit_id"] for image in images}
        rows: list[list[str]] = [[]]
        for token in parent.get("payload", {}).get("inline_tokens", []):
            if token.get("kind") == "line_break" and rows[-1]:
                rows.append([])
            elif token.get("kind") == "image" and token.get("unit_id") in image_ids:
                rows[-1].append(token["unit_id"])
        rows = [row for row in rows if row] or [[image["unit_id"] for image in images]]
        image_groups.append({
            "block_index": parent.get("source", {}).get("block_index"),
            "object_unit_ids": [item for row in rows for item in row],
            "layout_rows": rows,
        })

    # Word 常用无边框表格排布多子图；这类图片仍按图片组处理，而不是数据表。
    tables_by_id = {
        unit["unit_id"]: unit for unit in units if unit.get("unit_type") == "table"
    }
    table_images: dict[str, list[dict[str, Any]]] = {}
    for image in (unit for unit in units if unit.get("unit_type") == "image"):
        parent_id = image.get("relations", {}).get("parent_unit_id")
        if parent_id in tables_by_id:
            table_images.setdefault(parent_id, []).append(image)
    for parent_id, images in table_images.items():
        parent = tables_by_id[parent_id]
        rows_by_index: dict[int, list[dict[str, Any]]] = {}
        for image in images:
            rows_by_index.setdefault(image.get("source", {}).get("row", 0), []).append(image)
        layout_rows = [
            [item["unit_id"] for item in sorted(row_images, key=lambda value: value.get("source", {}).get("grid_column", 0))]
            for _, row_images in sorted(rows_by_index.items())
        ]
        image_groups.append({
            "block_index": parent.get("source", {}).get("block_index"),
            "object_unit_ids": [item for row in layout_rows for item in row],
            "layout_rows": layout_rows,
        })

    data_tables = [
        unit for unit in units
        if unit.get("unit_type") == "table"
        and unit.get("properties", {}).get("semantic_role") == "data"
    ]
    blocks = {
        unit.get("source", {}).get("block_index"): unit
        for unit in units if unit.get("unit_type") in {"paragraph", "table"}
    }
    groups_by_block = {group["block_index"]: group for group in image_groups}
    tables_by_block = {table["source"]["block_index"]: table for table in data_tables}

    def adjacent(block: int, direction: int, english_id: str | None) -> int:
        """只有空段落和该题注的英文对应项不打断相邻关系。"""
        cursor = block + direction
        while cursor in blocks:
            item = blocks[cursor]
            tokens = item.get("payload", {}).get("inline_tokens", [])
            empty = (
                item["unit_type"] == "paragraph" and not item.get("text", "").strip()
                and not any(token.get("kind") in {
                    "image", "formula", "footnote", "field", "hyperlink", "text_box"
                } for token in tokens)
            )
            if not empty and item["unit_id"] != english_id:
                break
            cursor += direction
        return cursor

    used_objects: set[str] = set()
    bindings = []
    for unit in units:
        if unit.get("unit_type") != "paragraph" or unit.get("properties", {}).get("generated_contents"):
            continue
        caption = infer_caption(unit.get("text", ""))
        if not caption:
            continue
        caption = _caption_metadata(caption, unit, block_paragraphs)
        block = unit.get("source", {}).get("block_index")
        options = []
        if block is not None and caption["kind"] == "figure":
            cursor = adjacent(block, -1, None)
            selected_groups = []
            while cursor in groups_by_block:
                group = groups_by_block[cursor]
                if any(value in used_objects for value in group["object_unit_ids"]):
                    break
                selected_groups.insert(0, group)
                cursor = adjacent(cursor, -1, None)
            if selected_groups:
                options = [{
                    "object_unit_ids": [value for group in selected_groups for value in group["object_unit_ids"]],
                    "layout_rows": [row for group in selected_groups for row in group["layout_rows"]],
                }]
        elif block is not None:
            cursor = adjacent(block, 1, caption.get("caption_en_unit_id"))
            while cursor in tables_by_block:
                table = tables_by_block[cursor]
                if table["unit_id"] in used_objects:
                    break
                options.append({"object_unit_ids": [table["unit_id"]], "layout_rows": []})
                cursor = adjacent(cursor, 1, None)
        selected = options[0] if len(options) == 1 else {"object_unit_ids": [], "layout_rows": []}
        selected_ids = selected["object_unit_ids"]
        used_objects.update(selected_ids)
        bindings.append({
            **caption, "caption_unit_id": unit["unit_id"],
            **selected,
            "status": "bound" if selected_ids else ("needs_review" if options else "unbound"),
            "candidates": [
                {**option, "label": f"{'图片组' if caption['kind'] == 'figure' else '数据表'} {index + 1}"}
                for index, option in enumerate(options)
            ],
            "evidence": ["word_block_direction", "unique_adjacent_object"] if selected_ids else ["ambiguous_objects" if options else "caption_without_object"],
        })
    return bindings
