# -*- coding: utf-8 -*-
"""把一次 Word 抽取结果保存为后续识别和生成共用的中间结果。"""

from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path
from typing import Callable

from content_extraction.content_units import CONTENT_UNITS_SCHEMA_VERSION
from pipeline.results import ExtractResult


##### 路径与异常板块 #####


EXTRACTION_DIRECTORY = "extraction"
EXTRACTION_FILENAME = "content_units.json"


class PersistedExtractionError(ValueError):
    """持久化抽取结果缺失或不再匹配当前项目。"""


def extraction_path(project_dir: str | Path) -> Path:
    return Path(project_dir).resolve() / "reports" / EXTRACTION_DIRECTORY


##### 序列化板块 #####


def _validate_result(result: ExtractResult, expected_source_sha256: str) -> None:
    report = result.extraction_report
    if report.get("schema_version") != CONTENT_UNITS_SCHEMA_VERSION:
        raise PersistedExtractionError("抽取结果的数据结构版本不受支持。")
    source_hash = report.get("metadata", {}).get("source_sha256")
    if source_hash != expected_source_sha256:
        raise PersistedExtractionError("抽取结果与项目源 Word 不匹配。")


def save_extraction(
    result: ExtractResult, project_dir: str | Path, *, source_sha256: str,
) -> Path:
    """首次保存完整抽取结果；已有结果由调用方直接读取。"""
    _validate_result(result, source_sha256)
    target = extraction_path(project_dir)
    if target.exists():
        raise PersistedExtractionError("项目已经存在抽取结果。")

    reports = target.parent
    reports.mkdir(parents=True, exist_ok=True)
    temporary = reports / f".{EXTRACTION_DIRECTORY}-{uuid.uuid4().hex}"
    media_dir = temporary / "media"
    media_dir.mkdir(parents=True)
    try:
        media_files: dict[str, str] = {}
        for unit_id, blob in result.media_assets.items():
            relative = Path("media") / f"{unit_id}.bin"
            (temporary / relative).write_bytes(blob)
            media_files[unit_id] = relative.as_posix()
        payload = {
            "schema_version": CONTENT_UNITS_SCHEMA_VERSION,
            "source_path": result.extraction_report["source_path"],
            "metadata": result.extraction_report["metadata"],
            "units": result.content_units,
            "issues": result.extraction_report["issues"],
            "paragraphs": result.paragraphs,
            "tables": result.tables,
            "media_files": media_files,
        }
        (temporary / EXTRACTION_FILENAME).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        temporary.replace(target)
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    return target


##### 读取与复用板块 #####


def load_extraction(
    project_dir: str | Path, *, source_sha256: str,
) -> ExtractResult:
    root = extraction_path(project_dir)
    data_file = root / EXTRACTION_FILENAME
    if not data_file.is_file():
        raise PersistedExtractionError("项目缺少完整的 Word 抽取结果。")
    try:
        payload = json.loads(data_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PersistedExtractionError("Word 抽取结果不可读取。") from exc
    if payload.get("schema_version") != CONTENT_UNITS_SCHEMA_VERSION:
        raise PersistedExtractionError("Word 抽取结果的数据结构版本已失效。")
    if payload.get("metadata", {}).get("source_sha256") != source_sha256:
        raise PersistedExtractionError("Word 抽取结果不属于当前源文件。")
    if set(payload) != {
        "schema_version", "source_path", "metadata", "units", "issues",
        "paragraphs", "tables", "media_files",
    }:
        raise PersistedExtractionError("Word 抽取结果不符合当前数据结构。")

    media_assets: dict[str, bytes] = {}
    for unit_id, relative_value in payload.get("media_files", {}).items():
        relative = Path(relative_value)
        media_path = (root / relative).resolve()
        if media_path.parent != (root / "media").resolve() or not media_path.is_file():
            raise PersistedExtractionError(f"Word 图片中间结果缺失：{unit_id}")
        media_assets[str(unit_id)] = media_path.read_bytes()
    counts: dict[str, int] = {}
    for unit in payload["units"]:
        kind = unit["unit_type"]
        counts[kind] = counts.get(kind, 0) + 1
    result = ExtractResult(
        paragraphs=list(payload.get("paragraphs", [])),
        tables=list(payload.get("tables", [])),
        content_units=payload["units"],
        extraction_report={
            "schema_version": payload["schema_version"],
            "source_path": payload["source_path"],
            "metadata": payload["metadata"],
            "unit_counts": counts,
            "issue_count": len(payload["issues"]),
            "issues": payload["issues"],
        },
        media_assets=media_assets,
    )
    _validate_result(result, source_sha256)
    return result


def load_or_extract(
    project_dir: str | Path,
    *,
    source_sha256: str,
    extractor: Callable[[], ExtractResult],
) -> ExtractResult:
    """已有中间结果时直接复用，否则只执行一次抽取并保存。"""
    if (extraction_path(project_dir) / EXTRACTION_FILENAME).is_file():
        return load_extraction(project_dir, source_sha256=source_sha256)
    result = extractor()
    save_extraction(result, project_dir, source_sha256=source_sha256)
    return result

