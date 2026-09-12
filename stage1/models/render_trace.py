# -*- coding: utf-8 -*-
"""Word 内容单元到 LaTeX 片段的渲染追踪模型。"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field


##### 版本与哈希板块 #####


RENDER_TRACE_VERSION = "1.0.0"


def fragment_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


##### 记录模型板块 #####


@dataclass(slots=True)
class RenderRecord:
    record_id: str
    marker_unit_id: str
    source_unit_ids: list[str]
    source_order: int
    role: str
    status: str
    target_file: str
    output_order: int
    transformations: list[str] = field(default_factory=list)
    details: dict = field(default_factory=dict)
    output_hash: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class RenderTrace:
    source_schema_version: str
    records: list[RenderRecord] = field(default_factory=list)
    trace_version: str = RENDER_TRACE_VERSION

    def add(self, **kwargs) -> RenderRecord:
        record = RenderRecord(
            record_id=f"r-{len(self.records):06d}",
            output_order=len(self.records),
            **kwargs,
        )
        self.records.append(record)
        return record

    def to_dict(self) -> dict:
        return {
            "trace_version": self.trace_version,
            "source_schema_version": self.source_schema_version,
            "records": [record.to_dict() for record in self.records],
        }

