# -*- coding: utf-8 -*-
"""Word 抽取结果持久化与复用单元测试。"""

from __future__ import annotations

import json

import pytest

from content_extraction.persistence import (
    PersistedExtractionError,
    load_extraction,
    load_or_extract,
    save_extraction,
)
from content_extraction.content_units import CONTENT_UNITS_SCHEMA_VERSION
from pipeline.results import ExtractResult


##### 测试数据板块 #####


SOURCE_HASH = "a" * 64


def _result() -> ExtractResult:
    return ExtractResult(
        paragraphs=[{"index": 0, "unit_id": "u-000000", "text": "正文"}],
        tables=[],
        content_units=[{
            "unit_id": "u-000000", "unit_type": "paragraph", "order": 0,
            "text": "正文", "source": {"paragraph_index": 0},
            "properties": {}, "relations": {}, "payload": {},
            "status": "extracted", "content_hash": "unit-hash",
        }, {
            "unit_id": "u-000001", "unit_type": "image", "order": 1,
            "text": "示例图", "source": {"paragraph_index": 0},
            "properties": {}, "relations": {"parent_unit_id": "u-000000"},
            "payload": {"content_type": "image/png"},
            "status": "extracted", "content_hash": "image-hash",
        }],
        extraction_report={
            "schema_version": CONTENT_UNITS_SCHEMA_VERSION,
            "unit_counts": {"paragraph": 1, "image": 1},
            "issue_count": 0, "issues": [], "source_path": "paper.docx",
            "metadata": {"source_sha256": SOURCE_HASH},
        },
        media_assets={"u-000001": b"image-bytes"},
    )


##### 往返与复用板块 #####


def test_complete_result_and_media_round_trip(tmp_path) -> None:
    expected = _result()
    save_extraction(expected, tmp_path, source_sha256=SOURCE_HASH)

    restored = load_extraction(tmp_path, source_sha256=SOURCE_HASH)

    assert restored.paragraphs == expected.paragraphs
    assert restored.tables == expected.tables
    assert restored.content_units == expected.content_units
    assert restored.extraction_report == expected.extraction_report
    assert restored.media_assets == expected.media_assets


def test_existing_result_is_reused_without_calling_extractor(tmp_path) -> None:
    save_extraction(_result(), tmp_path, source_sha256=SOURCE_HASH)

    restored = load_or_extract(
        tmp_path,
        source_sha256=SOURCE_HASH,
        extractor=lambda: pytest.fail("已有结果时不应再次解析 Word"),
    )

    assert restored.paragraphs[0]["text"] == "正文"


##### 失效检查板块 #####


@pytest.mark.parametrize("invalid_version", ["1.6.0", "future-schema"])
def test_source_hash_and_schema_version_mismatch_are_rejected(tmp_path, invalid_version) -> None:
    save_extraction(_result(), tmp_path, source_sha256=SOURCE_HASH)
    with pytest.raises(PersistedExtractionError, match="不属于当前源文件"):
        load_extraction(tmp_path, source_sha256="b" * 64)

    data_file = tmp_path / "reports" / "extraction" / "content_units.json"
    payload = json.loads(data_file.read_text(encoding="utf-8"))
    payload["schema_version"] = invalid_version
    data_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(PersistedExtractionError, match="数据结构版本已失效"):
        load_extraction(tmp_path, source_sha256=SOURCE_HASH)

