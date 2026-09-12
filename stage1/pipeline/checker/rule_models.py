# -*- coding: utf-8 -*-
"""G5 格式规则定义、运行结果和质量状态聚合。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


##### 规则常量板块 #####


RULE_STATUSES = {"pass", "fail", "not_applicable", "not_implemented", "internal_error"}
RULE_DISPOSITIONS = {"block", "degrade"}
RULE_PHASES = {"source", "compile", "pdf"}


##### 规则定义板块 #####


@dataclass(frozen=True)
class RuleDefinition:
    """一条可审计规则；检测状态和失败处置是两个独立维度。"""

    rule_id: str
    category: str
    item: str
    layer: str
    phase: str
    disposition: str
    detector: str | None = None
    source: str = ""
    config: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.phase not in RULE_PHASES:
            raise ValueError(f"未知规则阶段：{self.phase}")
        if self.disposition not in RULE_DISPOSITIONS:
            raise ValueError(f"未知规则处置：{self.disposition}")


@dataclass(frozen=True)
class RuleResult:
    """单条规则的规范化执行结果。"""

    rule: RuleDefinition
    status: str
    detail: str
    evidence: list[dict[str, Any]] = field(default_factory=list)
    occurrence_count: int = 0

    def __post_init__(self) -> None:
        if self.status not in RULE_STATUSES:
            raise ValueError(f"未知规则状态：{self.status}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "pass": True if self.status == "pass" else False if self.status == "fail" else None,
            "disposition": self.rule.disposition,
            "layer": self.rule.layer,
            "phase": self.rule.phase,
            "category": self.rule.category,
            "item": self.rule.item,
            "detector": self.rule.detector,
            "source": self.rule.source,
            "detail": self.detail,
            "evidence": self.evidence,
            "occurrence_count": self.occurrence_count,
        }


##### 质量聚合板块 #####


@dataclass(frozen=True)
class RuleSummary:
    quality_status: str
    publish_allowed: bool
    all_passed: bool
    blockers: list[dict[str, Any]]
    degradations: list[dict[str, Any]]
    counts: dict[str, int]


def summarize_rule_results(results: dict[str, dict[str, Any]]) -> RuleSummary:
    """阻断、降级和完全通过分别汇总，禁止未实现规则计入通过。"""
    blockers: list[dict[str, Any]] = []
    degradations: list[dict[str, Any]] = []
    counts = {status: 0 for status in sorted(RULE_STATUSES)}
    for rule_id, result in results.items():
        status = result["status"]
        counts[status] += 1
        if status in {"pass", "not_applicable"}:
            continue
        issue = {
            "rule_id": rule_id,
            "status": status,
            "detail": result.get("detail", ""),
            "category": result.get("category", ""),
        }
        if status == "internal_error" or result["disposition"] == "block":
            blockers.append(issue)
        else:
            degradations.append(issue)

    if counts["internal_error"]:
        quality_status = "internal_error"
    elif blockers:
        quality_status = "blocked"
    elif degradations:
        quality_status = "degraded"
    else:
        quality_status = "passed"
    return RuleSummary(
        quality_status=quality_status,
        publish_allowed=not blockers,
        all_passed=quality_status == "passed",
        blockers=blockers,
        degradations=degradations,
        counts=counts,
    )
