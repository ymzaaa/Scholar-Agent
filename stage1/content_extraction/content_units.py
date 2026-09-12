# -*- coding: utf-8 -*-
"""Word 有序内容单元模型。

模型只描述源文档事实，不在抽取阶段猜测 LaTeX 格式。所有字段均可序列化，
便于后端传输、保真报告和后续版本迁移。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any


##### 模型版本板块 #####


CONTENT_UNITS_SCHEMA_VERSION = "1.7.0"


##### 问题记录板块 #####


@dataclass(slots=True)
class ExtractionIssue:
    """抽取过程中的明确失败或降级，不允许静默丢弃。"""

    code: str
    message: str
    severity: str = "warning"
    unit_id: str | None = None
    source: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


##### 内容单元板块 #####


@dataclass(slots=True)
class ContentUnit:
    """一个可追溯的 Word 内容对象。"""

    unit_id: str
    unit_type: str
    order: int  # 稳定创建顺序；段内阅读顺序由 inline_tokens 和来源偏移表示。
    text: str = ""
    source: dict[str, Any] = field(default_factory=dict)
    properties: dict[str, Any] = field(default_factory=dict)
    relations: dict[str, Any] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict)
    status: str = "extracted"
    content_hash: str = ""

    def __post_init__(self) -> None:
        if not self.content_hash:
            self.refresh_content_hash()

    def refresh_content_hash(self) -> None:
        """在分阶段填充表格载荷后重新计算语义内容哈希。"""
        canonical = json.dumps(
            {
                "unit_type": self.unit_type,
                "text": self.text,
                "payload": self.payload,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        self.content_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


##### 抽取结果板块 #####


@dataclass(slots=True)
class ExtractionBundle:
    """完整抽取结果及兼容旧流水线所需的投影。"""

    source_path: str
    units: list[ContentUnit]
    paragraphs: list[dict[str, Any]]
    tables: list[dict[str, Any]]
    issues: list[ExtractionIssue] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    # 二进制资源只在进程内传递，不写入 content_units JSON。
    media_assets: dict[str, bytes] = field(default_factory=dict, repr=False)
    schema_version: str = CONTENT_UNITS_SCHEMA_VERSION

    def units_as_dicts(self) -> list[dict[str, Any]]:
        return [unit.to_dict() for unit in self.units]

    def report(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for unit in self.units:
            counts[unit.unit_type] = counts.get(unit.unit_type, 0) + 1
        return {
            "schema_version": self.schema_version,
            "unit_counts": counts,
            "issue_count": len(self.issues),
            "issues": [issue.to_dict() for issue in self.issues],
            "source_path": self.source_path,
            "metadata": self.metadata,
        }
