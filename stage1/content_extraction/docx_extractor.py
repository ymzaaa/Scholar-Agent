# -*- coding: utf-8 -*-
"""按 WordprocessingML 正文顺序抽取内容单元。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph
from lxml import etree

from content_extraction.content_units import ContentUnit, ExtractionBundle, ExtractionIssue
from content_extraction.word_objects import extract_inline_sequence, extract_physical_table
from content_extraction.text_fragments import _extract_text_fragments, _bind_text_fragment_tokens
from content_extraction.word_semantics import build_semantic_context


##### 编号分配板块 #####


@dataclass
class _UnitFactory:
    order: int = 0

    def create(self, unit_type: str, **kwargs: Any) -> ContentUnit:
        unit = ContentUnit(
            unit_id=f"u-{self.order:06d}",
            unit_type=unit_type,
            order=self.order,
            **kwargs,
        )
        self.order += 1
        return unit


##### XML 辅助板块 #####


def _local_name(element: Any) -> str:
    return etree.QName(element).localname


def _body_blocks(document: Any) -> Iterator[Any]:
    """只展开块级内容控件；段落和表格内部由各自抽取器消费。"""
    def walk(container: Any) -> Iterator[Any]:
        for child in container:
            if child.tag == qn("w:sdt"):
                content = child.find(qn("w:sdtContent"))
                if content is not None:
                    yield from walk(content)
                else:
                    yield child
            elif child.tag != qn("w:sectPr"):
                yield child
    yield from walk(document.element.body)


def _twips(value: Any) -> int | None:
    """把 python-docx 的长度值归一为 twip，保留 Word 原始布局量纲。"""
    if value is None:
        return None
    twips = getattr(value, "twips", None)
    return int(round(twips)) if twips is not None else None


def _style_paragraph_value(paragraph: Paragraph, attribute: str) -> tuple[Any, str]:
    """沿样式继承链读取段落属性；均未声明时回到 Word 的零缩进默认值。"""
    style = paragraph.style
    visited: set[str] = set()
    while style is not None and style.style_id not in visited:
        visited.add(style.style_id)
        value = getattr(style.paragraph_format, attribute)
        if value is not None:
            return value, f"style:{style.style_id}"
        style = style.base_style
    return None, "word_default"


def _paragraph_layout(paragraph: Paragraph, p_pr: Any | None) -> dict[str, Any]:
    """提取模板无关的段落布局事实，不在此阶段生成 LaTeX 命令。"""
    direct_indent = paragraph.paragraph_format.first_line_indent
    indent_source = "direct"
    effective_indent = direct_indent
    if effective_indent is None:
        effective_indent, indent_source = _style_paragraph_value(
            paragraph, "first_line_indent"
        )
    indent_twips = _twips(effective_indent)
    if indent_twips is None:
        indent_twips = 0
    indent_state = "zero" if indent_twips == 0 else (
        "hanging" if indent_twips < 0 else "first_line"
    )

    alignment = paragraph.alignment
    alignment_source = "direct"
    if alignment is None:
        alignment, alignment_source = _style_paragraph_value(paragraph, "alignment")

    spacing = p_pr.find(qn("w:spacing")) if p_pr is not None else None
    indent = p_pr.find(qn("w:ind")) if p_pr is not None else None
    return {
        "first_line_indent": {
            "state": indent_state,
            "twips": indent_twips,
            "characters_hundredths": (
                int(indent.get(qn("w:firstLineChars")))
                if indent is not None
                and str(indent.get(qn("w:firstLineChars"), "")).lstrip("-").isdigit()
                else None
            ),
            "source": indent_source,
        },
        "alignment": {
            "value": str(alignment) if alignment is not None else None,
            "source": alignment_source,
        },
        "spacing": {
            "before_twips": int(spacing.get(qn("w:before")))
            if spacing is not None and str(spacing.get(qn("w:before"), "")).isdigit()
            else _twips(paragraph.paragraph_format.space_before),
            "after_twips": int(spacing.get(qn("w:after")))
            if spacing is not None and str(spacing.get(qn("w:after"), "")).isdigit()
            else _twips(paragraph.paragraph_format.space_after),
            "line": spacing.get(qn("w:line")) if spacing is not None else None,
            "line_rule": spacing.get(qn("w:lineRule")) if spacing is not None else None,
        },
    }


def _paragraph_presentation(paragraph: Paragraph) -> dict[str, Any]:
    """记录可复核的 Word 展示事实，不在抽取阶段把字体外观等同于标题。"""
    visible_runs = [(run, len(run.text.strip())) for run in paragraph.runs if run.text.strip()]
    visible_characters = sum(length for _, length in visible_runs)
    bold_characters = sum(length for run, length in visible_runs if run.bold is True)
    sizes: dict[float, int] = {}
    colors: set[str] = set()
    for run, length in visible_runs:
        if run.font.size is not None:
            size = round(float(run.font.size.pt), 2)
            sizes[size] = sizes.get(size, 0) + length
        color = run.font.color.rgb
        if color is not None:
            colors.add(str(color))
    dominant_size = max(sizes, key=sizes.get) if sizes else None
    return {
        "visible_characters": visible_characters,
        "bold_characters": bold_characters,
        "bold_ratio": round(bold_characters / visible_characters, 3)
        if visible_characters else 0.0,
        "dominant_font_size_pt": dominant_size,
        "explicit_font_sizes_pt": sorted(sizes),
        "explicit_colors": sorted(colors),
    }


def _paragraph_properties(paragraph: Paragraph, element: Any) -> dict[str, Any]:
    p_pr = element.find(qn("w:pPr"))
    outline_level = None
    num_id = None
    num_level = None
    if p_pr is not None:
        outline = p_pr.find(qn("w:outlineLvl"))
        if outline is not None:
            outline_level = outline.get(qn("w:val"))
        num_pr = p_pr.find(qn("w:numPr"))
        if num_pr is not None:
            num_id_el = num_pr.find(qn("w:numId"))
            level_el = num_pr.find(qn("w:ilvl"))
            num_id = num_id_el.get(qn("w:val")) if num_id_el is not None else None
            num_level = level_el.get(qn("w:val")) if level_el is not None else None
    galleries = [
        node.get(qn("w:val"), "")
        for ancestor in element.iterancestors(qn("w:sdt"))
        for node in ancestor.findall("./" + qn("w:sdtPr") + "/" + qn("w:docPartObj") + "/" + qn("w:docPartGallery"))
    ]
    return {
        # 自动目录同样保留抽取内容，但其缓存标题不能参与正文结构推断。
        "generated_contents": "Table of Contents" in galleries,
        "style": paragraph.style.name if paragraph.style else "None",
        "outline_level": int(outline_level) if str(outline_level).isdigit() else None,
        "numbering_id": num_id,
        "numbering_level": int(num_level) if str(num_level).isdigit() else None,
        "layout": _paragraph_layout(paragraph, p_pr),
        "presentation": _paragraph_presentation(paragraph),
    }


def _mark_equation_explanations(units: list[ContentUnit]) -> None:
    """用公式邻接和 Word 零缩进识别解释段，关键词不作为唯一判据。"""
    paragraphs = sorted(
        (unit for unit in units if unit.unit_type == "paragraph"),
        key=lambda item: item.source.get("block_index", -1),
    )
    display_parents = {
        unit.relations.get("parent_unit_id")
        for unit in units
        if unit.unit_type == "formula"
        and unit.status == "extracted"
        and unit.properties.get("display")
    }
    for previous, current in zip(paragraphs, paragraphs[1:]):
        previous_block = previous.source.get("block_index")
        current_block = current.source.get("block_index")
        layout = current.properties.get("layout", {})
        indent = layout.get("first_line_indent", {})
        style = str(current.properties.get("style", "")).lower()
        is_heading = current.properties.get("outline_level") is not None or any(
            token in style for token in ("heading", "标题")
        )
        if (
            previous.unit_id in display_parents
            and current_block == previous_block + 1
            and current.text.strip()
            and indent.get("state") == "zero"
            and not is_heading
        ):
            current.properties["semantic_role"] = "equation_explanation"


##### 段落内对象板块 #####


##### 表格抽取板块 #####


def _table_semantic_role(data: list[list[str]]) -> str:
    nonempty = [cell.strip() for row in data for cell in row if cell.strip()]
    if not nonempty:
        return "empty"
    header = [cell.strip().replace(" ", "") for cell in data[0]] if data else []
    if {"英文缩写", "英文全称", "中文全称"}.issubset(set(header)):
        return "abbreviation"
    metadata_keys = {
        row[0].strip().rstrip("：:")
        for row in data
        if row and row[0].strip()
    }
    if metadata_keys & {"作者", "指导教师", "学位类型", "专业名称", "研究方向"}:
        return "cover_metadata"
    return "data"


def _extract_table(
    element: Any,
    document: Any,
    block_index: int,
    table_index: int,
    factory: _UnitFactory,
    issues: list[ExtractionIssue],
    media_assets: dict[str, bytes],
    semantic_context: Any,
) -> tuple[ContentUnit, dict[str, Any], list[ContentUnit]]:
    table = Table(element, document)
    data = [[cell.text.strip() for cell in row.cells] for row in table.rows]
    legacy = {
        "table_index": table_index,
        "rows": len(table.rows),
        "cols": len(table.columns),
        "data": data,
        "block_index": block_index,
    }
    unit = factory.create(
        "table",
        source={"part": "word/document.xml", "block_index": block_index, "table_index": table_index},
        properties={
            "rows": len(table.rows),
            "columns": len(table.columns),
            "semantic_role": _table_semantic_role(data),
        },
        payload={"data": data, "physical_rows": []},
    )
    physical_rows, children, unsupported = extract_physical_table(
        element, unit, document, factory, issues, media_assets, semantic_context
    )
    unit.payload["physical_rows"] = physical_rows
    unit.properties["unsupported_features"] = unsupported
    unit.status = "degraded" if unsupported else "extracted"
    unit.refresh_content_hash()
    legacy["unit_id"] = unit.unit_id
    legacy["physical_rows"] = physical_rows
    legacy["unsupported_features"] = unsupported
    return unit, legacy, children


##### 正式入口板块 #####


def extract_docx_ordered(docx_path: str) -> ExtractionBundle:
    """抽取 DOCX，同时生成新内容单元和旧接口兼容投影。"""
    source = Path(docx_path).resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Word 文件不存在：{source}")

    document = Document(str(source))
    semantic_context = build_semantic_context(document)
    factory = _UnitFactory()
    units: list[ContentUnit] = []
    paragraphs: list[dict[str, Any]] = []
    tables: list[dict[str, Any]] = []
    issues: list[ExtractionIssue] = []
    media_assets: dict[str, bytes] = {}
    paragraph_index = 0
    table_index = 0

    for block_index, element in enumerate(_body_blocks(document)):
        if element.tag == qn("w:p"):
            paragraph = Paragraph(element, document)
            properties = _paragraph_properties(paragraph, element)
            legacy = {
                "index": paragraph_index,
                "style": properties["style"],
                "text": paragraph.text,
                "block_index": block_index,
                "outline_level": properties["outline_level"],
                "numbering_id": properties["numbering_id"],
                "numbering_level": properties["numbering_level"],
                "layout": properties["layout"],
                "presentation": properties["presentation"],
                "generated_contents": properties["generated_contents"],
            }
            parent = factory.create(
                "paragraph",
                text=paragraph.text,
                source={
                    "part": "word/document.xml",
                    "block_index": block_index,
                    "paragraph_index": paragraph_index,
                },
                properties=properties,
            )
            units.append(parent)
            legacy["unit_id"] = parent.unit_id
            text_children = _extract_text_fragments(parent, factory, issues, paragraph)
            units.extend(text_children)
            object_children, inline_tokens = extract_inline_sequence(
                element, parent, document, factory, issues, media_assets,
                semantic_context=semantic_context,
            )
            text_fragments = [
                item for item in text_children
                if item.unit_type == "citation"
                or (
                    item.unit_type == "formula"
                    and item.payload.get("source_syntax") == "latex"
                )
            ]
            parent.payload["inline_tokens"] = _bind_text_fragment_tokens(
                inline_tokens, text_fragments
            )
            parent.refresh_content_hash()
            units.extend(object_children)
            paragraphs.append(legacy)
            paragraph_index += 1
        elif element.tag == qn("w:tbl"):
            unit, legacy, children = _extract_table(
                element, document, block_index, table_index, factory,
                issues, media_assets, semantic_context,
            )
            units.append(unit)
            # 表格单元格内部也可能包含公式、图片或 OLE，必须纳入同一抽取模型。
            units.extend(children)
            tables.append(legacy)
            table_index += 1
        else:
            issues.append(ExtractionIssue(
                code="BODY_BLOCK_UNSUPPORTED",
                message=f"正文节点尚无法解析：{_local_name(element)}。",
                severity="error",
                source={"part": "word/document.xml", "block_index": block_index},
            ))

    _mark_equation_explanations(units)
    unit_by_id = {unit.unit_id: unit for unit in units}
    for paragraph in paragraphs:
        unit = unit_by_id[paragraph["unit_id"]]
        paragraph["semantic_role"] = unit.properties.get("semantic_role")

    metadata = {
        "body_block_count": paragraph_index + table_index,
        "paragraph_count": paragraph_index,
        "table_count": table_index,
        "source_size": source.stat().st_size,
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "footnote_part_count": len(semantic_context.footnotes),
        "referenced_footnote_count": len(semantic_context.referenced_footnotes),
    }
    for name, unit_ids in semantic_context.bookmark_names.items():
        if name and len(unit_ids) > 1:
            issues.append(ExtractionIssue(
                code="BOOKMARK_NAME_DUPLICATE",
                message=f"书签名称重复，交叉引用目标不唯一：{name}",
                severity="error",
                unit_id=unit_ids[0],
                source={},
            ))
    return ExtractionBundle(
        source_path=str(source),
        units=units,
        paragraphs=paragraphs,
        tables=tables,
        issues=issues,
        metadata=metadata,
        media_assets=media_assets,
    )
