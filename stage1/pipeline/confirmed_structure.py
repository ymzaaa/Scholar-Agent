# -*- coding: utf-8 -*-
"""建立、确认并应用唯一结构快照；生成阶段不重新识别标题或题注。"""

from __future__ import annotations

import copy
from typing import Any

from content_extraction.content_units import CONTENT_UNITS_SCHEMA_VERSION
from content_extraction.citations import apply_citation_overrides
from pipeline.results import RecognizeResult
from pipeline.structure_recognizer import chapters_from_headings


##### 快照建立板块 #####


class ConfirmedStructureError(ValueError):
    """结构快照与源 Word、内容模型或确认规则不一致。"""


def build_structure_snapshot(extracted, recognized: RecognizeResult, source_sha256: str) -> dict[str, Any]:
    """保存一次识别的全部事实；对象索引只保存确认校验所需的信息。"""
    review = recognized.review
    snapshot = {
        "project_source_sha256": source_sha256,
        "schema_version": extracted.extraction_report.get("schema_version"),
        "revision": 0, "confirmed": False,
        "headings": copy.deepcopy(review["heading_candidates"]),
        "chapters": copy.deepcopy(review["chapters"]),
        "regions": copy.deepcopy(review["regions"]),
        "object_bindings": copy.deepcopy(review["object_bindings"]),
        "reference_region": copy.deepcopy(review["reference_region"]),
        "citation_review": copy.deepcopy(review["citation_review"]),
        "formula_review": copy.deepcopy(review["formula_review"]),
        "issues": copy.deepcopy(extracted.extraction_report.get("issues", [])),
        "unit_index": {
            unit["unit_id"]: {
                "unit_type": unit["unit_type"],
                "semantic_role": unit.get("properties", {}).get("semantic_role"),
                "block_index": unit.get("source", {}).get("block_index"),
            } for unit in extracted.content_units
        },
        "paragraph_evidence": [
            {key: copy.deepcopy(item.get(key)) for key in (
                "unit_id", "text", "style", "index", "block_index", "layout", "presentation"
            )} for item in extracted.paragraphs
        ],
        "counts": {
            "paragraphs": len(extracted.paragraphs), "tables": len(extracted.tables),
            "content_units": len(extracted.content_units),
            "malformed_formulas": review["formula_review"].get("malformed_formula_count", 0),
        },
    }
    update_structure_counts(snapshot)
    return snapshot


##### 确认规则板块 #####


def update_structure_counts(snapshot: dict[str, Any]) -> None:
    """统计只用于展示，不参与决定结构事实。"""
    headings = snapshot.get("headings", [])
    bindings = snapshot.get("object_bindings", [])
    snapshot.setdefault("counts", {}).update({
        "headings": len(headings),
        "heading_reviews": sum(item.get("requires_review", False) and item.get("review_status") != "resolved" for item in headings),
        "object_bindings": len(bindings),
        "object_binding_reviews": sum(item.get("status") != "bound" for item in bindings),
        "citation_reviews": snapshot.get("citation_review", {}).get("counts", {}).get("needs_review", 0),
    })


def validate_object_bindings(bindings: list[dict[str, Any]], unit_index: dict[str, Any], *, require_resolved: bool = True) -> None:
    """正文题注必须有唯一归属；数据表不能由布局表替代。"""
    used: set[str] = set()
    captions: set[str] = set()
    for item in bindings:
        caption_id = item.get("caption_unit_id")
        if caption_id in captions or unit_index.get(caption_id, {}).get("unit_type") != "paragraph":
            raise ConfirmedStructureError(f"题注单元无效或重复：{caption_id}")
        captions.add(caption_id)
        object_ids = item.get("object_unit_ids", [])
        if require_resolved and (item.get("status") != "bound" or not object_ids):
            raise ConfirmedStructureError(f"正文图表关系尚未确认：{item.get('number', caption_id)}")
        if item.get("kind") not in {"figure", "table"}:
            raise ConfirmedStructureError("题注类型无效")
        expected = "image" if item["kind"] == "figure" else "table"
        if expected == "table" and len(object_ids) > 1:
            raise ConfirmedStructureError("一个表题只能绑定一个数据表")
        for object_id in object_ids:
            info = unit_index.get(object_id, {})
            if info.get("unit_type") != expected:
                raise ConfirmedStructureError(f"对象类型不匹配：{object_id}")
            if expected == "table" and info.get("semantic_role") != "data":
                raise ConfirmedStructureError(f"表题只能绑定数据表：{object_id}")
            if object_id in used:
                raise ConfirmedStructureError(f"同一对象不能重复绑定：{object_id}")
            used.add(object_id)


def finalize_structure_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """确认时计算完整章节范围；原始快照始终保持不变。"""
    result = copy.deepcopy(snapshot)
    if result.get("schema_version") != CONTENT_UNITS_SCHEMA_VERSION:
        raise ConfirmedStructureError("确认快照的内容模型版本不一致")
    unresolved = [item["unit_id"] for item in result["headings"] if item.get("requires_review") and item.get("review_status") != "resolved"]
    if unresolved:
        raise ConfirmedStructureError(f"低置信度标题候选尚未确认：{unresolved[:5]}")
    for order, item in enumerate(sorted(result["headings"], key=lambda value: value["source"]["paragraph_index"])):
        if item.get("level") not in {"chapter", "section", "subsection", "body"} or "title" not in item:
            raise ConfirmedStructureError("确认标题层级或规范文字缺失")
        item["order"] = order
    validate_object_bindings(result["object_bindings"], result["unit_index"])
    try:
        result["citation_review"] = apply_citation_overrides(result["citation_review"], [])
        result["chapters"] = chapters_from_headings(result["headings"], result["regions"], validate=True)
    except ValueError as exc:
        raise ConfirmedStructureError(str(exc)) from exc
    result["confirmed"] = True
    update_structure_counts(result)
    return result


##### 应用校验板块 #####


def _validate_snapshot(extracted, snapshot, source_sha256, structure_revision):
    if not snapshot.get("confirmed"):
        raise ConfirmedStructureError("结构尚未确认，禁止生成。")
    if snapshot.get("project_source_sha256") != source_sha256:
        raise ConfirmedStructureError("确认快照与当前 Word 哈希不一致。")
    if extracted.extraction_report.get("metadata", {}).get("source_sha256") != source_sha256:
        raise ConfirmedStructureError("抽取结果与当前 Word 哈希不一致。")
    if snapshot.get("schema_version") != CONTENT_UNITS_SCHEMA_VERSION or snapshot["schema_version"] != extracted.extraction_report.get("schema_version"):
        raise ConfirmedStructureError("确认快照的内容模型版本不一致。")
    if snapshot.get("revision") != structure_revision or structure_revision < 1:
        raise ConfirmedStructureError("确认快照修订号已变化，请重新加载。")
    units = {unit["unit_id"]: unit for unit in extracted.content_units}
    for item in snapshot.get("headings", []):
        unit = units.get(item["unit_id"], {})
        if unit.get("unit_type") != "paragraph" or unit.get("text") != item.get("text"):
            raise ConfirmedStructureError(f"确认标题单元已失效：{item['unit_id']}")
        if item.get("requires_review") and item.get("review_status") != "resolved":
            raise ConfirmedStructureError("低置信度标题候选尚未确认")
    index = {key: {"unit_type": unit["unit_type"], "semantic_role": unit.get("properties", {}).get("semantic_role")} for key, unit in units.items()}
    validate_object_bindings(snapshot.get("object_bindings", []), index)
    chapters = snapshot.get("chapters", [])
    if not chapters:
        raise ConfirmedStructureError("确认快照缺少完整章节范围")
    headings = {item["unit_id"]: item for item in snapshot["headings"]}
    last_end = None
    for chapter in chapters:
        start, end = chapter["range"]
        item = headings.get(chapter.get("unit_id"), {})
        if item.get("level") != "chapter" or chapter.get("title") != item.get("title") or start != item.get("source", {}).get("paragraph_index"):
            raise ConfirmedStructureError("确认章节与标题不一致")
        if not 0 <= start < end <= len(extracted.paragraphs) or (last_end is not None and last_end != start):
            raise ConfirmedStructureError("确认章节范围无效")
        last_end = end
    if last_end != snapshot["regions"]["body_range"][1]:
        raise ConfirmedStructureError("确认章节与正文边界不一致")
    citations = snapshot.get("citation_review", {})
    actual_ids = {key for key, unit in units.items() if unit["unit_type"] == "citation"}
    if actual_ids != {item["unit_id"] for item in citations.get("decisions", [])}:
        raise ConfirmedStructureError("确认引用单元与当前抽取结果不一致")
    try:
        apply_citation_overrides(citations, [])
    except ValueError as exc:
        raise ConfirmedStructureError(str(exc)) from exc


##### 渲染结果板块 #####


def apply_confirmed_structure(
    extract_result, recognize_result: RecognizeResult | None,
    snapshot: dict[str, Any], *, source_sha256: str, structure_revision: int,
) -> RecognizeResult:
    """直接还原完整确认结构，不调用识别器，不修改任何输入对象。"""
    _validate_snapshot(extract_result, snapshot, source_sha256, structure_revision)
    review = {
        name: copy.deepcopy(snapshot[name]) for name in (
            "chapters", "regions", "object_bindings", "reference_region",
            "citation_review", "formula_review",
        )
    }
    review["heading_candidates"] = copy.deepcopy(snapshot["headings"])
    internal = copy.deepcopy(recognize_result.internal) if recognize_result else {}
    internal["structure_revision"] = structure_revision
    return RecognizeResult(review=review, internal=internal)


def resolve_render_structure(extracted, recognized: RecognizeResult) -> RecognizeResult:
    """CLI/库调用仅接受全部已解决的候选，并使用与网页相同的确认规则。"""
    source = extracted.extraction_report.get("metadata", {}).get("source_sha256", "")
    snapshot = build_structure_snapshot(extracted, recognized, source)
    if recognized.internal.get("structure_revision"):
        snapshot.update(confirmed=True, revision=recognized.internal["structure_revision"])
    else:
        snapshot = finalize_structure_snapshot(snapshot)
        snapshot["revision"] = 1
    return apply_confirmed_structure(
        extracted, recognized, snapshot, source_sha256=source,
        structure_revision=snapshot["revision"],
    )
