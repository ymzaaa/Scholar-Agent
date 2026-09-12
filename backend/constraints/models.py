# -*- coding: utf-8 -*-
"""P10 论文级反馈约束的领域模型与输入校验。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


CONSTRAINT_STATUSES = {"proposed", "confirmed", "active", "revoked", "superseded"}
CONSTRAINT_SCOPES = {"document", "chapter", "content_unit"}
VERIFICATION_MODES = {"deterministic", "human_review"}


@dataclass(frozen=True, slots=True)
class PaperConstraint:
    constraint_id: str
    project_id: str
    template_id: str
    raw_feedback: str
    constraint_type: str
    parameters: dict[str, Any]
    description: str
    scope_type: str
    scope_ref: str | None
    source_feedback_id: str
    source_session_id: str
    candidate_generation_id: str
    status: str = "proposed"
    priority: int = 50
    conflict_ids: tuple[str, ...] = field(default_factory=tuple)
    supersedes_id: str | None = None
    verifier: dict[str, Any] = field(default_factory=lambda: {"mode": "human_review"})
    created_at: str = ""
    confirmed_at: str | None = None
    updated_at: str = ""

    def validate(self) -> None:
        required = {
            "constraint_id": self.constraint_id, "project_id": self.project_id,
            "template_id": self.template_id,
            "raw_feedback": self.raw_feedback, "constraint_type": self.constraint_type,
            "description": self.description, "source_feedback_id": self.source_feedback_id,
            "source_session_id": self.source_session_id,
            "candidate_generation_id": self.candidate_generation_id,
        }
        missing = [name for name, value in required.items() if not str(value).strip()]
        if missing:
            raise ValueError(f"论文约束缺少必填字段：{missing}")
        if self.status not in CONSTRAINT_STATUSES:
            raise ValueError(f"论文约束状态无效：{self.status}")
        if self.scope_type not in CONSTRAINT_SCOPES:
            raise ValueError(f"论文约束作用域无效：{self.scope_type}")
        if self.scope_type != "document" and not self.scope_ref:
            raise ValueError("章节或内容单元约束必须包含 scope_ref。")
        if not 0 <= self.priority <= 100:
            raise ValueError("论文约束优先级必须位于 0–100。")
        mode = self.verifier.get("mode")
        if mode not in VERIFICATION_MODES:
            raise ValueError(f"论文约束验证模式无效：{mode}")
        if mode == "deterministic" and not self.verifier.get("checker"):
            raise ValueError("确定性论文约束必须声明 checker。")
        if self.constraint_id in self.conflict_ids:
            raise ValueError("论文约束不能与自身冲突。")

    def to_snapshot(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["conflict_ids"] = list(self.conflict_ids)
        return payload
