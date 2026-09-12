# -*- coding: utf-8 -*-
"""从 WordprocessingML 提取有序行内对象、媒体资源和物理表格结构。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Any

from docx.oxml.ns import qn
from lxml import etree

from content_extraction.content_units import ContentUnit, ExtractionIssue
from content_extraction.omml import OmmlParseError, omml_to_latex
from content_extraction.text_fragments import _extract_text_fragments, _bind_text_fragment_tokens
from content_extraction.word_semantics import (
    SemanticContext,
    create_field_unit,
    resolve_hyperlink,
)


##### 命名空间与基础工具板块 #####


M_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
WP_NS = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
O_NS = "urn:schemas-microsoft-com:office:office"


def _name(element: Any) -> str:
    return etree.QName(element).localname


def _namespace(element: Any) -> str | None:
    return etree.QName(element).namespace


def _visible_token_text(tokens: list[dict], units: list[ContentUnit]) -> str:
    """语义 token 化后仍保留嵌套容器的原始可见文字。"""
    by_id = {unit.unit_id: unit for unit in units}
    parts = []
    for token in tokens:
        if token.get("kind") == "text":
            parts.append(token.get("text", ""))
        elif token.get("kind") == "line_break":
            parts.append("\n")
        else:
            child = by_id.get(token.get("unit_id"))
            if child and child.unit_type in {"formula", "citation"}:
                parts.append(child.payload.get("raw_text", child.text))
    return "".join(parts)


def _drawing_container(element: Any) -> tuple[str, Any | None]:
    current = element
    while current is not None:
        if _namespace(current) == WP_NS and _name(current) in {"inline", "anchor"}:
            return _name(current), current
        current = current.getparent()
    return "unknown", None


def _source(parent: ContentUnit, token_index: int, extra: dict | None) -> dict:
    return {**parent.source, **(extra or {}), "token_index": token_index}


##### 图片与公式抽取板块 #####


def _extract_formula(
    node: Any,
    parent: ContentUnit,
    factory: Any,
    issues: list[ExtractionIssue],
    token_index: int,
    extra_source: dict | None,
) -> ContentUnit:
    raw_xml = etree.tostring(node, encoding="unicode")
    try:
        latex = omml_to_latex(raw_xml)
        status = "extracted"
    except OmmlParseError as exc:
        latex = ""
        status = "degraded"
        issues.append(
            ExtractionIssue(
                code="OMML_UNSUPPORTED",
                message=str(exc),
                severity="error",
                unit_id=parent.unit_id,
                source=_source(parent, token_index, extra_source),
            )
        )
    return factory.create(
        "formula",
        text=latex,
        source=_source(parent, token_index, extra_source),
        properties={"display": _name(node) == "oMathPara"},
        relations={"parent_unit_id": parent.unit_id},
        payload={"source_syntax": "omml", "omml_xml": raw_xml},
        status=status,
    )


def _extract_image(
    blip: Any,
    parent: ContentUnit,
    document: Any,
    factory: Any,
    issues: list[ExtractionIssue],
    media_assets: dict[str, bytes],
    token_index: int,
    extra_source: dict | None,
) -> ContentUnit:
    relationship_id = blip.get(qn("r:embed")) or blip.get(qn("r:link"))
    placement, container = _drawing_container(blip)
    target = content_type = None
    blob: bytes | None = None
    if relationship_id and relationship_id in document.part.rels:
        relation = document.part.rels[relationship_id]
        target = relation.target_ref
        target_part = getattr(relation, "target_part", None)
        content_type = getattr(target_part, "content_type", None)
        blob = getattr(target_part, "blob", None)
    name = description = ""
    width_emu = height_emu = None
    if container is not None:
        doc_properties = next(container.iter(qn("wp:docPr")), None)
        extent = next(container.iter(qn("wp:extent")), None)
        if doc_properties is not None:
            name = doc_properties.get("name", "")
            description = doc_properties.get("descr", "")
        if extent is not None:
            width_emu = int(extent.get("cx", "0") or 0)
            height_emu = int(extent.get("cy", "0") or 0)
    status = "extracted" if blob else "degraded"
    image = factory.create(
        "image",
        text=description or name,
        source=_source(parent, token_index, extra_source),
        properties={
            "placement": placement,
            "name": name,
            "description": description,
            "width_emu": width_emu,
            "height_emu": height_emu,
        },
        relations={"parent_unit_id": parent.unit_id, "relationship_id": relationship_id},
        payload={
            "target_part": target,
            "content_type": content_type,
            "binary_sha256": hashlib.sha256(blob).hexdigest() if blob else None,
            "binary_size": len(blob) if blob else None,
            "embedded": bool(blip.get(qn("r:embed"))),
        },
        status=status,
    )
    if blob:
        media_assets[image.unit_id] = bytes(blob)
    else:
        issues.append(
            ExtractionIssue(
                code="IMAGE_BINARY_UNAVAILABLE",
                message="图片不是可读取的 Word 内嵌资源；外部链接图片必须另行提供。",
                severity="error",
                unit_id=image.unit_id,
                source=image.source,
            )
        )
    return image


##### 有序行内序列板块 #####


##### Word语义对象板块 #####


def extract_inline_sequence(
    element: Any,
    parent: ContentUnit,
    document: Any,
    factory: Any,
    issues: list[ExtractionIssue],
    media_assets: dict[str, bytes],
    extra_source: dict | None = None,
    semantic_context: SemanticContext | None = None,
) -> tuple[list[ContentUnit], list[dict[str, Any]]]:
    """按XML顺序抽取文字、媒体、脚注、域、链接、书签和文本框。"""
    context = semantic_context or SemanticContext()
    children: list[ContentUnit] = []
    tokens: list[dict[str, Any]] = []
    field_stack: list[dict[str, Any]] = []

    def current_source(token_index: int | None = None, **values: Any) -> dict[str, Any]:
        result = {**parent.source, **(extra_source or {}), **values}
        if token_index is not None:
            result["token_index"] = token_index
        return result

    def append_text(kind: str, value: str = "") -> None:
        if kind == "text" and tokens and tokens[-1].get("kind") == "text":
            tokens[-1]["text"] += value
        else:
            token = {"kind": kind}
            if value:
                token["text"] = value
            tokens.append(token)

    def finish_field(state: dict[str, Any], *, malformed: bool = False) -> ContentUnit:
        unit = create_field_unit(
            factory=factory,
            parent=parent,
            source=current_source(state["token_index"]),
            instruction="".join(state["instruction"]),
            result_text="".join(state["result"]),
            locked=state["locked"],
            dirty=state["dirty"],
            malformed=malformed,
            nested=state["nested"],
            issues=issues,
        )
        return unit

    def extract_footnote(node: Any) -> None:
        note_id = node.get(qn("w:id"), "")
        note_data = context.footnotes.get(note_id)
        source = current_source(len(tokens), part="word/footnotes.xml", footnote_id=note_id)
        unit = factory.create(
            "footnote",
            text="",
            source=source,
            properties={"note_kind": "footnote", "note_id": note_id},
            relations={"parent_unit_id": parent.unit_id},
            payload={"blocks": [], "unsupported_features": []},
            status="extracted" if note_data else "failed",
        )
        children.append(unit)
        tokens.append({"kind": "footnote", "unit_id": unit.unit_id})
        if not note_data:
            issues.append(ExtractionIssue(
                code="FOOTNOTE_TARGET_MISSING",
                message=f"脚注引用找不到footnotes.xml正文：id={note_id}",
                severity="error",
                unit_id=unit.unit_id,
                source=source,
            ))
            return
        context.referenced_footnotes.add(note_id)
        note_element = note_data["element"]
        if note_element.find(".//" + qn("w:tbl")) is not None:
            unit.payload["unsupported_features"].append("table_in_footnote")
        if note_element.find(".//" + qn("w:txbxContent")) is not None:
            unit.payload["unsupported_features"].append("text_box_in_footnote")
        blocks = []
        note_children = []
        for block_index, paragraph in enumerate(note_element.findall("./" + qn("w:p"))):
            extracted, block_tokens = extract_inline_sequence(
                paragraph,
                unit,
                note_data["document"],
                factory,
                issues,
                media_assets,
                {"part": "word/footnotes.xml", "footnote_id": note_id, "footnote_paragraph_index": block_index},
                context,
            )
            blocks.append({"block_index": block_index, "tokens": block_tokens})
            note_children.extend(extracted)
        unit.payload["blocks"] = blocks
        unit.text = "\n".join(
            _visible_token_text(block["tokens"], note_children)
            for block in blocks
        )
        if unit.payload["unsupported_features"]:
            unit.status = "degraded"
            issues.append(ExtractionIssue(
                code="FOOTNOTE_COMPLEX_CONTENT_UNSUPPORTED",
                message=f"脚注包含首版不能安全渲染的对象：{unit.payload['unsupported_features']}",
                severity="error",
                unit_id=unit.unit_id,
                source=source,
            ))
        unit.refresh_content_hash()
        children.extend(note_children)

    def extract_text_box(node: Any) -> None:
        placement, container = _drawing_container(node)
        extent = next(container.iter(qn("wp:extent")), None) if container is not None else None
        unsupported = []
        if node.find(".//" + qn("w:tbl")) is not None:
            unsupported.append("table_in_text_box")
        if node.find(".//" + qn("w:txbxContent")) is not None:
            unsupported.append("nested_text_box")
        unit = factory.create(
            "text_box",
            text="",
            source=current_source(len(tokens)),
            properties={
                "placement": placement,
                "width_emu": int(extent.get("cx", "0")) if extent is not None else None,
                "height_emu": int(extent.get("cy", "0")) if extent is not None else None,
                "presentation_status": "layout_degraded",
            },
            relations={"parent_unit_id": parent.unit_id},
            payload={"blocks": [], "unsupported_features": unsupported},
            status="degraded" if unsupported else "extracted",
        )
        children.append(unit)
        tokens.append({"kind": "text_box", "unit_id": unit.unit_id})
        blocks = []
        box_children = []
        for block_index, paragraph in enumerate(node.findall("./" + qn("w:p"))):
            extracted, block_tokens = extract_inline_sequence(
                paragraph, unit, document, factory, issues, media_assets,
                {**(extra_source or {}), "text_box_paragraph_index": block_index}, context,
            )
            blocks.append({"block_index": block_index, "tokens": block_tokens})
            box_children.extend(extracted)
        unit.payload["blocks"] = blocks
        unit.text = "\n".join(
            _visible_token_text(block["tokens"], box_children)
            for block in blocks
        )
        if unsupported:
            issues.append(ExtractionIssue(
                code="TEXT_BOX_COMPLEX_CONTENT_UNSUPPORTED",
                message=f"文本框包含首版不能安全渲染的对象：{unsupported}",
                severity="error",
                unit_id=unit.unit_id,
                source=unit.source,
            ))
        unit.refresh_content_hash()
        children.extend(box_children)

    def walk(node: Any) -> None:
        namespace = _namespace(node)
        name = _name(node)
        parent_node = node.getparent()

        if namespace == W_NS and name == "fldSimple":
            instruction = node.get(qn("w:instr"), "")
            result_text = "".join(value.text or "" for value in node.iter(qn("w:t")))
            unit = create_field_unit(
                factory=factory,
                parent=parent,
                source=current_source(len(tokens)),
                instruction=instruction,
                result_text=result_text,
                locked=node.get(qn("w:fldLock")) in {"1", "true"},
                dirty=node.get(qn("w:dirty")) in {"1", "true"},
                malformed=False,
                nested=bool(node.find(".//" + qn("w:fldSimple"))),
                issues=issues,
            )
            children.append(unit)
            tokens.append({"kind": "field", "unit_id": unit.unit_id})
            return

        if namespace == W_NS and name == "fldChar":
            field_type = node.get(qn("w:fldCharType"), "")
            if field_type == "begin":
                if field_stack:
                    field_stack[-1]["nested"] = True
                field_stack.append({
                    "instruction": [], "result": [], "phase": "instruction",
                    "token_index": len(tokens),
                    "locked": node.get(qn("w:fldLock")) in {"1", "true"},
                    "dirty": node.get(qn("w:dirty")) in {"1", "true"},
                    "nested": False,
                })
            elif field_type == "separate" and field_stack:
                field_stack[-1]["phase"] = "result"
            elif field_type == "end" and field_stack:
                state = field_stack.pop()
                if field_stack:
                    field_stack[-1]["result"].extend(state["result"])
                    field_stack[-1]["nested"] = True
                else:
                    unit = finish_field(state)
                    children.append(unit)
                    tokens.append({"kind": "field", "unit_id": unit.unit_id})
            else:
                issues.append(ExtractionIssue(
                    code="FIELD_STRUCTURE_MALFORMED",
                    message=f"Word域边界异常：fldCharType={field_type}",
                    severity="error",
                    unit_id=parent.unit_id,
                    source=current_source(len(tokens)),
                ))
            return

        if field_stack:
            state = field_stack[-1]
            if namespace == W_NS and name == "instrText":
                state["instruction"].append(node.text or "")
                return
            if namespace == W_NS and name == "t" and state["phase"] == "result":
                state["result"].append(node.text or "")
                return
            if namespace == W_NS and name in {"tab", "br", "cr"} and state["phase"] == "result":
                state["result"].append("\t" if name == "tab" else "\n")
                return
            for child in node:
                walk(child)
            return

        if namespace == W_NS and name == "footnoteReference":
            extract_footnote(node)
            return
        if namespace == W_NS and name == "endnoteReference":
            note_id = node.get(qn("w:id"), "")
            unit = factory.create(
                "footnote", text="", source=current_source(len(tokens), endnote_id=note_id),
                properties={"note_kind": "endnote", "note_id": note_id},
                relations={"parent_unit_id": parent.unit_id}, status="degraded",
            )
            children.append(unit)
            tokens.append({"kind": "footnote", "unit_id": unit.unit_id})
            issues.append(ExtractionIssue(
                code="ENDNOTE_UNSUPPORTED", message="当前尚不支持尾注内容转换。",
                severity="error", unit_id=unit.unit_id, source=unit.source,
            ))
            return
        if namespace == W_NS and name == "hyperlink":
            resolved = resolve_hyperlink(node, document)
            display_text = "".join(value.text or "" for value in node.iter(qn("w:t")))
            supported = bool(resolved["target_bookmark"] or resolved["safe_external"])
            unit = factory.create(
                "hyperlink", text=display_text, source=current_source(len(tokens)),
                properties={"internal": bool(resolved["target_bookmark"])},
                relations={"parent_unit_id": parent.unit_id}, payload=resolved,
                status="extracted" if supported else "degraded",
            )
            children.append(unit)
            tokens.append({"kind": "hyperlink", "unit_id": unit.unit_id})
            if not supported:
                issues.append(ExtractionIssue(
                    code="HYPERLINK_TARGET_UNSAFE_OR_MISSING",
                    message="链接目标缺失或协议不在http/https/mailto允许列表。",
                    severity="error", unit_id=unit.unit_id, source=unit.source,
                ))
            return
        if namespace == W_NS and name == "bookmarkStart":
            bookmark_name = node.get(qn("w:name"), "")
            unit = factory.create(
                "bookmark", text=bookmark_name, source=current_source(len(tokens)),
                properties={"bookmark_id": node.get(qn("w:id")), "name": bookmark_name},
                relations={"parent_unit_id": parent.unit_id},
            )
            children.append(unit)
            tokens.append({"kind": "bookmark_start", "unit_id": unit.unit_id})
            context.bookmark_names.setdefault(bookmark_name, []).append(unit.unit_id)
            return
        if namespace == W_NS and name == "bookmarkEnd":
            tokens.append({"kind": "bookmark_end", "bookmark_id": node.get(qn("w:id"))})
            return
        if namespace == W_NS and name == "txbxContent":
            extract_text_box(node)
            return
        if namespace == M_NS and name in {"oMath", "oMathPara"} and (
            parent_node is None or _namespace(parent_node) != M_NS
        ):
            unit = _extract_formula(node, parent, factory, issues, len(tokens), extra_source)
            children.append(unit)
            tokens.append({"kind": "formula", "unit_id": unit.unit_id})
            return
        if name == "blip":
            unit = _extract_image(
                node, parent, document, factory, issues, media_assets, len(tokens), extra_source
            )
            children.append(unit)
            tokens.append({"kind": "image", "unit_id": unit.unit_id})
            return
        if namespace == O_NS and name == "OLEObject":
            unit = factory.create(
                "ole_object", text=node.get("ProgID", "OLE Object"),
                source=current_source(len(tokens)),
                properties={"program_id": node.get("ProgID")},
                relations={"parent_unit_id": parent.unit_id, "relationship_id": node.get(qn("r:id"))},
                status="degraded",
            )
            children.append(unit)
            tokens.append({"kind": "unsupported_object", "unit_id": unit.unit_id})
            issues.append(ExtractionIssue(
                code="OLE_REQUIRES_MANUAL_CONVERSION",
                message="OLE对象不属于当前公式支持范围。", severity="warning",
                unit_id=unit.unit_id, source=unit.source,
            ))
            return
        if namespace == W_NS and name == "t":
            append_text("text", node.text or "")
            return
        if namespace == W_NS and name == "tab":
            append_text("text", "\t")
            return
        if namespace == W_NS and name in {"br", "cr"}:
            append_text("line_break")
            return
        for child in node:
            walk(child)

    walk(element)
    while field_stack:
        state = field_stack.pop(0)
        unit = finish_field(state, malformed=True)
        children.append(unit)
        tokens.append({"kind": "field", "unit_id": unit.unit_id})
    if parent.unit_type in {"table", "footnote", "text_box"}:
        # 逐个连续文字 token 扫描，禁止跨脚注、链接或公式重组美元片段。
        bound_tokens = []
        offset = 0
        for token in tokens:
            if token.get("kind") != "text":
                bound_tokens.append(token)
                continue
            raw = token.get("text", "")
            proxy = replace(parent, text=raw, source=current_source())
            fragments = _extract_text_fragments(proxy, factory, issues)
            bound_tokens.extend(_bind_text_fragment_tokens([token], fragments))
            for fragment in fragments:
                fragment.source["token_offset"] = offset
            children.extend(fragments)
            offset += len(raw)
        tokens = bound_tokens
    return children, tokens


##### 物理表格模型板块 #####


def _unsupported_table_features(table_element: Any) -> list[str]:
    features = []
    if table_element.find(".//" + qn("w:tc") + "/" + qn("w:tbl")) is not None:
        features.append("nested_table")
    if table_element.find("./" + qn("w:tblPr") + "/" + qn("w:tblpPr")) is not None:
        features.append("floating_table_position")
    return sorted(set(features))


def extract_physical_table(
    table_element: Any,
    parent: ContentUnit,
    document: Any,
    factory: Any,
    issues: list[ExtractionIssue],
    media_assets: dict[str, bytes],
    semantic_context: SemanticContext | None = None,
) -> tuple[list[dict[str, Any]], list[ContentUnit], list[str]]:
    """读取物理 w:tc，而不是 python-docx 展开后的重复合并单元格。"""
    rows: list[dict[str, Any]] = []
    children: list[ContentUnit] = []
    unsupported = _unsupported_table_features(table_element)
    for row_index, row in enumerate(table_element.findall("./" + qn("w:tr"))):
        grid_col = 0
        cells = []
        for physical_col, cell in enumerate(row.findall("./" + qn("w:tc"))):
            tc_pr = cell.find("./" + qn("w:tcPr"))
            span_node = tc_pr.find("./" + qn("w:gridSpan")) if tc_pr is not None else None
            merge_node = tc_pr.find("./" + qn("w:vMerge")) if tc_pr is not None else None
            grid_span = int(span_node.get(qn("w:val"), "1")) if span_node is not None else 1
            if merge_node is None:
                vertical_merge = "none"
            else:
                vertical_merge = merge_node.get(qn("w:val")) or "continue"
            borders = tc_pr.find("./" + qn("w:tcBorders")) if tc_pr is not None else None
            diagonal_border = "none"
            if borders is not None and borders.find("./" + qn("w:tl2br")) is not None:
                diagonal_border = "top_left_to_bottom_right"
            elif borders is not None and borders.find("./" + qn("w:tr2bl")) is not None:
                diagonal_border = "top_right_to_bottom_left"
            cell_tokens: list[dict[str, Any]] = []
            for paragraph_index, paragraph in enumerate(cell.findall("./" + qn("w:p"))):
                if paragraph_index:
                    cell_tokens.append({"kind": "line_break"})
                extracted, tokens = extract_inline_sequence(
                    paragraph,
                    parent,
                    document,
                    factory,
                    issues,
                    media_assets,
                    {
                        "row": row_index,
                        "physical_column": physical_col,
                        "grid_column": grid_col,
                        "cell_paragraph_index": paragraph_index,
                    },
                    semantic_context,
                )
                children.extend(extracted)
                cell_tokens.extend(tokens)
            text = _visible_token_text(cell_tokens, children).strip("\n")
            canonical = json.dumps(
                {"text": text, "tokens": cell_tokens, "grid_span": grid_span, "vertical_merge": vertical_merge, "diagonal_border": diagonal_border},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            cells.append(
                {
                    "cell_id": f"{parent.unit_id}:r{row_index}:c{grid_col}",
                    "row": row_index,
                    "physical_column": physical_col,
                    "grid_column": grid_col,
                    "grid_span": grid_span,
                    "vertical_merge": vertical_merge,
                    "diagonal_border": diagonal_border,
                    "text": text,
                    "tokens": cell_tokens,
                    "content_hash": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
                }
            )
            grid_col += grid_span
        rows.append({"row": row_index, "cells": cells})
    for feature in unsupported:
        issues.append(
            ExtractionIssue(
                code="TABLE_PRESENTATION_UNSUPPORTED",
                message=f"表格包含当前 LaTeX 渲染器不能等价表达的 Word 展示特征：{feature}",
                severity="error",
                unit_id=parent.unit_id,
                source=parent.source,
            )
        )
    return rows, children, unsupported
