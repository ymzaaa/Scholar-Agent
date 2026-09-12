# -*- coding: utf-8 -*-
"""结构快照读取、校验与原子确认事务。"""

from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path
from typing import Any

from persistence.store import Project, ProjectStore


##### 异常与快照板块 #####


class StructureValidationError(ValueError):
    def __init__(
        self, code: str, detail: str, *, unit_ids: list[str] | None = None
    ) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.unit_ids = unit_ids or []


def recognition_path(project: Project) -> Path:
    return Path(project.workspace_dir) / "reports" / "recognition.json"


def read_structure_snapshot(project: Project) -> dict[str, Any] | None:
    confirmed = Path(project.workspace_dir) / "reports" / "confirmed_structure.json"
    recognition = recognition_path(project)
    if not confirmed.is_file() and not recognition.is_file():
        return None
    if not confirmed.is_file():
        return json.loads(recognition.read_text(encoding="utf-8"))
    confirmed_snapshot = json.loads(confirmed.read_text(encoding="utf-8"))
    if not recognition.is_file():
        return confirmed_snapshot
    recognition_snapshot = json.loads(recognition.read_text(encoding="utf-8"))
    same_source = (
        confirmed_snapshot.get("project_source_sha256")
        == recognition_snapshot.get("project_source_sha256")
    )
    same_schema = (
        confirmed_snapshot.get("schema_version")
        == recognition_snapshot.get("schema_version")
    )
    return confirmed_snapshot if same_source and same_schema else recognition_snapshot


def write_confirmed_snapshot(project: Project, snapshot: dict[str, Any]) -> None:
    target = Path(project.workspace_dir) / "reports" / "confirmed_structure.json"
    temporary = target.with_name(f".tmp-{uuid.uuid4().hex}")
    temporary.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(target)


##### 确认事务板块 #####


def commit_structure_confirmation(
    *, project: Project, store: ProjectStore, base_revision: int,
    heading_overrides: list[dict[str, Any]],
    object_binding_overrides: list[dict[str, Any]] | None = None,
    citation_overrides: list[dict[str, Any]] | None = None,
    confirmed: bool = True,
    validate_only: bool = False,
) -> dict[str, Any]:
    """人工接口和 Agent 恢复共用同一确定性提交路径。"""
    from infrastructure.stage1_adapter import _ensure_stage1_on_path

    _ensure_stage1_on_path()
    from content_extraction.citations import apply_citation_overrides
    from content_extraction.content_units import CONTENT_UNITS_SCHEMA_VERSION
    from pipeline.confirmed_structure import (
        finalize_structure_snapshot, update_structure_counts, validate_object_bindings,
        ConfirmedStructureError,
    )

    current = read_structure_snapshot(project)
    if current is None:
        raise StructureValidationError("structure_not_ready", "提取尚未完成")
    if base_revision != current["revision"]:
        raise StructureValidationError("revision_conflict", "结构已被其他修订更新")
    if current.get("project_source_sha256") != project.source_sha256:
        raise StructureValidationError("source_changed", "结构快照与源文件不匹配")
    if current.get('schema_version') != CONTENT_UNITS_SCHEMA_VERSION:
        raise StructureValidationError('schema_changed', '结构快照的内容模型版本不一致')
    snapshot = copy.deepcopy(current)
    unit_index = snapshot.get("unit_index", {})
    heading_by_id = {item["unit_id"]: item for item in snapshot["headings"]}
    binding_by_id = {
        item["caption_unit_id"]: item for item in snapshot["object_bindings"]
    }
    try:
        snapshot["citation_review"] = apply_citation_overrides(
            snapshot.get("citation_review", {}), citation_overrides or [],
            require_resolved=confirmed,
        )
    except ValueError as exc:
        raise StructureValidationError("invalid_citation_review", str(exc)) from exc
    for override in heading_overrides:
        unit_id = str(override.get("unit_id", ""))
        level = str(override.get("level", ""))
        if unit_id not in heading_by_id:
            raise StructureValidationError("unknown_heading", unit_id)
        if level not in {"chapter", "section", "subsection", "body"}:
            raise StructureValidationError("invalid_heading_level", level)
        heading_by_id[unit_id]["level"] = level
        heading_by_id[unit_id]["overridden"] = True
        heading_by_id[unit_id]["review_status"] = "resolved"
    for override in object_binding_overrides or []:
        caption_id = str(override.get("caption_unit_id", ""))
        if caption_id not in binding_by_id:
            raise StructureValidationError("unknown_caption", caption_id)
        binding = binding_by_id[caption_id]
        object_ids = [str(value) for value in override.get("object_unit_ids", [])]
        expected_type = "image" if binding.get("kind") == "figure" else "table"
        if any(unit_index.get(item, {}).get('unit_type') != expected_type for item in object_ids):
            raise StructureValidationError("unknown_object", caption_id)
        binding["object_unit_ids"] = object_ids
        binding["status"] = "bound" if object_ids else "unbound"
        binding["layout_rows"] = next((option.get("layout_rows", []) for option in binding.get("candidates", []) if option["object_unit_ids"] == object_ids), [object_ids] if expected_type == "image" else [])
        binding["overridden"] = True
    unresolved = [
        item["unit_id"] for item in heading_by_id.values()
        if item.get("requires_review") and item.get("review_status") != "resolved"
    ]
    if confirmed and unresolved:
        raise StructureValidationError(
            "unresolved_structure_review",
            f"仍有 {len(unresolved)} 个低置信度标题候选未确认",
            unit_ids=unresolved,
        )
    snapshot["revision"] += 1
    snapshot["confirmed"] = confirmed
    try:
        if confirmed:
            snapshot = finalize_structure_snapshot(snapshot)
        else:
            validate_object_bindings(snapshot["object_bindings"], unit_index, require_resolved=False)
            update_structure_counts(snapshot)
    except ConfirmedStructureError as exc:
        raise StructureValidationError("invalid_confirmed_structure", str(exc)) from exc
    if validate_only:
        return snapshot
    write_confirmed_snapshot(project, snapshot)
    store.update_structure_state(
        project.project_id, revision=snapshot["revision"], confirmed=confirmed
    )
    return snapshot
