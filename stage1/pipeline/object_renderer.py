# -*- coding: utf-8 -*-
"""把 Word 对象模型确定性地写入 LaTeX，并生成可审计资产清单。"""

from __future__ import annotations

import hashlib
from collections import Counter
from pathlib import Path
from typing import Any

from pipeline.text_processing import escape_cell, escape_text
from pipeline.inline_renderer import render_inline_tokens


##### 图片资产板块 #####


CONTENT_TYPE_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
}


def materialize_word_assets(
    units: list[dict[str, Any]],
    media_assets: dict[str, bytes],
    output_dir: str | Path,
) -> dict[str, dict[str, Any]]:
    """只写出 DOCX relationship 提供的原始字节，并复核二进制哈希。"""
    target_dir = Path(output_dir) / "assets" / "word"
    target_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, dict[str, Any]] = {}
    for unit in units:
        if unit.get("unit_type") != "image":
            continue
        unit_id = unit["unit_id"]
        blob = media_assets.get(unit_id)
        expected_hash = unit.get("payload", {}).get("binary_sha256")
        content_type = unit.get("payload", {}).get("content_type")
        extension = CONTENT_TYPE_EXTENSIONS.get(content_type)
        if not blob or not expected_hash or not extension:
            manifest[unit_id] = {
                "status": "failed",
                "reason": "missing_binary_or_unsupported_content_type",
            }
            continue
        actual_hash = hashlib.sha256(blob).hexdigest()
        if actual_hash != expected_hash:
            manifest[unit_id] = {"status": "failed", "reason": "source_hash_mismatch"}
            continue
        filename = f"{actual_hash[:24]}{extension}"
        path = target_dir / filename
        if not path.exists():
            path.write_bytes(blob)
        written_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        relative = path.relative_to(Path(output_dir)).as_posix()
        manifest[unit_id] = {
            "status": "verified" if written_hash == expected_hash else "failed",
            "source_sha256": expected_hash,
            "output_sha256": written_hash,
            "size": len(blob),
            "relative_path": relative,
        }
    return manifest


##### 已确认图片渲染板块 #####


def render_figure(
    number: str,
    caption: str,
    binding: dict[str, Any],
    asset_manifest: dict[str, dict[str, Any]],
    *,
    caption_en: str = "",
    template_adapter: Any,
) -> tuple[str, dict[str, Any], bool]:
    object_ids = binding.get("object_unit_ids", [])
    rows = binding.get("layout_rows") or [[unit_id] for unit_id in object_ids]
    verified = [
        unit_id for unit_id in object_ids
        if asset_manifest.get(unit_id, {}).get("status") == "verified"
    ]
    details = {
        "image_unit_ids": object_ids,
        "verified_image_unit_ids": verified,
        "layout_rows": rows,
        "caption_en_source": binding.get("caption_en_source", "missing"),
    }
    if Counter(unit_id for row in rows for unit_id in row) != Counter(object_ids) or len(set(object_ids)) != len(object_ids):
        details['unsupported_features'] = ['image_layout_ownership_mismatch']
        return '% 图片布局与确认对象不一致', details, False
    if not object_ids or len(verified) != len(object_ids):
        return "% 图片资源缺失或哈希校验失败", details, False
    label = number.replace("图", "fig").replace("-", "_").replace(".", "_")
    lines = [r"\begin{figure}[htbp]", r"\centering"]
    for row_index, row in enumerate(rows):
        width = min(0.9, 0.96 / max(len(row), 1))
        for image_index, unit_id in enumerate(row):
            path = asset_manifest[unit_id]["relative_path"]
            if len(row) > 1:
                lines.extend([
                    rf"\begin{{minipage}}[t]{{{width:.3f}\textwidth}}",
                    r"\centering",
                    rf"\includegraphics[width=\textwidth]{{{path}}}",
                    r"\end{minipage}",
                ])
                if image_index < len(row) - 1:
                    lines.append(r"\hfill")
            else:
                lines.append(rf"\includegraphics[width=0.85\linewidth]{{{path}}}")
        if row_index < len(rows) - 1:
            lines.append(r"\\[8pt]")
    safe_caption = escape_text(caption)
    safe_caption_en = escape_text(caption_en)
    lines.extend([
        template_adapter.render_caption("figure", safe_caption, safe_caption_en),
        rf"\label{{{label}}}",
        r"\end{figure}",
    ])
    return "\n".join(lines), details, True


##### 表格单元格板块 #####


def _rowspan(rows: list[dict[str, Any]], row_index: int, grid_col: int) -> int:
    count = 1
    for next_row in rows[row_index + 1:]:
        cell = next(
            (item for item in next_row["cells"] if item["grid_column"] == grid_col),
            None,
        )
        if not cell or cell["vertical_merge"] != "continue":
            break
        count += 1
    return count


def _marked_cell(cell_id: str, fragment: str) -> str:
    """物理单元格标记供内容门禁逐项核对，不改变表格的网格结构。"""
    return f"% SCHOLAR_CELL_BEGIN {cell_id}\n{fragment}\n% SCHOLAR_CELL_END {cell_id}\n"


def render_table(
    number: str,
    caption: str,
    table_unit: dict[str, Any],
    by_id: dict[str, dict[str, Any]],
    asset_manifest: dict[str, dict[str, Any]] | None = None,
    cite_map: dict | None = None,
    stats: dict | None = None,
    bookmark_index: dict[str, Any] | None = None,
    *,
    caption_en: str = "",
    include_caption: bool = True,
    template_adapter: Any,
    citation_decisions: dict | None = None,
) -> tuple[str, dict[str, Any], bool]:
    asset_manifest = asset_manifest or {}
    cite_map = cite_map or {}
    stats = stats or {"equations": 0, "citations": 0}
    bookmark_index = bookmark_index or {"labels": {}}
    rows = table_unit.get("payload", {}).get("physical_rows", [])
    unsupported = list(table_unit.get("properties", {}).get("unsupported_features", []))
    cell_ids = [cell["cell_id"] for row in rows for cell in row["cells"]]
    details = {
        "table_unit_id": table_unit.get("unit_id"),
        "source_cell_ids": cell_ids,
        "rendered_cell_ids": [],
        "rendered_formula_ids": [],
        "verified_image_unit_ids": [],
        "unsupported_features": unsupported,
        "caption_en_source": (
            "word" if caption_en else ("missing" if include_caption else "not_applicable")
        ),
    }
    if unsupported or not rows:
        return "% 表格包含不支持的展示结构，详见内容保真报告", details, False
    columns = int(table_unit.get("properties", {}).get("columns") or 1)
    safe_caption = escape_text(caption)
    safe_caption_en = escape_text(caption_en)
    lines = [r"\begin{table}[htbp]" if include_caption else r"\begin{center}",
        r"\centering",
        *template_adapter.table_preamble(),
    ]
    if include_caption:
        lines.append(template_adapter.render_caption("table", safe_caption, safe_caption_en))
    if columns >= 6:
        lines.append(r"\begin{adjustbox}{max width=\textwidth}")
    lines.extend([rf"\begin{{tabular}}{{{'c' * columns}}}", r"\toprule[1.5pt]"])
    for row_index, row in enumerate(rows):
        output_cells = []
        for cell in row["cells"]:
            rendered, semantic_details = render_inline_tokens(
                cell.get("tokens", []), by_id, cite_map, stats,
                asset_manifest, bookmark_index, context="table",
                citation_decisions=citation_decisions,
            )
            details["rendered_formula_ids"].extend(
                semantic_details["rendered_formula_ids"]
            )
            details["verified_image_unit_ids"].extend(
                semantic_details["verified_image_unit_ids"]
            )
            for key, value in semantic_details.items():
                if key in {"rendered_formula_ids", "verified_image_unit_ids"}:
                    continue
                if isinstance(value, list):
                    details.setdefault(key, []).extend(value)
                elif isinstance(value, int):
                    details[key] = details.get(key, 0) + value
            if (
                semantic_details["degraded_field_ids"]
                or semantic_details["degraded_hyperlink_ids"]
                or semantic_details["degraded_text_box_ids"]
                or len(semantic_details["footnote_ids"])
                != len(semantic_details["rendered_footnote_ids"])
            ):
                details["unsupported_features"].append(
                    f"semantic_cell:{cell['cell_id']}"
                )
            if cell["vertical_merge"] == "continue":
                if rendered.strip():
                    unsupported.append(f"nonempty_vertical_continuation:{cell['cell_id']}")
                output_cells.append(_marked_cell(cell["cell_id"], ""))
                details["rendered_cell_ids"].append(cell["cell_id"])
                continue
            if cell.get("diagonal_border") != "none":
                labels = [escape_cell(item.strip()) for item in cell.get("text", "").splitlines() if item.strip()]
                simple = all(token.get("kind") in {"text", "line_break"} for token in cell.get("tokens", []))
                if len(labels) == 2 and simple:
                    rendered = rf"\diagbox{{{labels[1]}}}{{{labels[0]}}}"
                else:
                    details["unsupported_features"].append(
                        f"diagonal_cell_text:{cell['cell_id']}"
                    )
            if r"\\" in rendered:
                rendered = rf"\makecell[c]{{{rendered}}}"
            span = int(cell.get("grid_span") or 1)
            if span > 1:
                rendered = rf"\multicolumn{{{span}}}{{c}}{{{rendered}}}"
            if cell["vertical_merge"] == "restart":
                rendered = rf"\multirow{{{_rowspan(rows, row_index, cell['grid_column'])}}}{{*}}{{{rendered}}}"
            output_cells.append(_marked_cell(cell["cell_id"], rendered))
            details["rendered_cell_ids"].append(cell["cell_id"])
        lines.append(" & ".join(output_cells) + r" \\")
        if row_index == 0:
            lines.append(r"\midrule[0.5pt]")
    lines.extend([r"\bottomrule[1.5pt]", r"\end{tabular}"])
    if columns >= 6:
        lines.append(r"\end{adjustbox}")
    if include_caption:
        label = number.replace("表", "tab").replace("-", "_").replace(".", "_")
        lines.extend([rf"\label{{{label}}}", r"\end{table}"])
    else:
        lines.append(r"\end{center}")
    return "\n".join(lines), details, not details["unsupported_features"]


def render_content_table(
    table_unit: dict[str, Any],
    by_id: dict[str, dict[str, Any]],
    asset_manifest: dict[str, dict[str, Any]] | None = None,
    cite_map: dict | None = None,
    stats: dict | None = None,
    bookmark_index: dict[str, Any] | None = None,
    *,
    template_adapter: Any,
    citation_decisions: dict | None = None,
) -> tuple[str, dict[str, Any], bool]:
    """原位渲染没有题注绑定的数据表，不虚构表号、题注或交叉引用标签。"""
    return render_table(
        "", "", table_unit, by_id, asset_manifest, cite_map, stats,
        bookmark_index, include_caption=False, template_adapter=template_adapter,
        citation_decisions=citation_decisions,
    )
