# -*- coding: utf-8 -*-
"""从 Word 源事实确定性解析首版元数据并集中报告缺失字段。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


##### 元数据模型板块 #####


PLACEHOLDER = "【待填写】"
@dataclass(frozen=True, slots=True)
class MetadataResolution:
    values: dict[str, str]
    items: tuple[dict[str, Any], ...]
    missing_fields: tuple[str, ...]


##### 确定性解析板块 #####


def resolve_minimal_metadata(
    paragraphs: list[dict[str, Any]], *, required_fields: tuple[str, ...]
) -> MetadataResolution:
    """解析通用标题，并按适配器声明的字段集中生成占位。"""
    title_candidates = [
        paragraph for paragraph in paragraphs
        if str(paragraph.get("style", "")).replace(" ", "").lower() == "title"
        and paragraph.get("text", "").strip()
    ]
    if len(title_candidates) != 1:
        raise ValueError("首版最小转换要求唯一 Word Title 样式标题。")
    title_item = title_candidates[0]
    missing = list(dict.fromkeys(
        field for field in required_fields if field != "title"
    ))
    if not all(field and isinstance(field, str) for field in missing):
        raise ValueError("模板元数据字段名必须是非空字符串。")
    values = {"title": title_item["text"].strip()}
    values.update({field: PLACEHOLDER for field in missing})
    items = ({
        "field": "title", "value": values["title"],
        "source_unit_id": title_item.get("unit_id"),
        "confidence": 1.0, "status": "resolved",
    }, *(
        {
            "field": field, "value": values[field], "source_unit_id": None,
            "confidence": 1.0, "status": "placeholder",
        }
        for field in missing
    ))
    return MetadataResolution(values, tuple(items), tuple(missing))
