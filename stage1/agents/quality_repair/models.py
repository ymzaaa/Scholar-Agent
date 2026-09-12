# -*- coding: utf-8 -*-
"""统一论文质量修复 Agent 的框架无关、可序列化领域模型。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from copy import deepcopy


##### 枚举模型板块 #####


class RunStatus(StrEnum):
    RUNNING = "running"
    WAITING_USER = "waiting_user"
    VALIDATING = "validating"
    READY = "ready"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass(slots=True)
class AgentState:
    """三种模式的恢复事实；客户端、文件事务及完整对话只属于运行时。"""

    run_id: str
    project_id: str
    mode: str
    template_id: str
    parent_generation_id: str | None = None
    parent_source_sha256: str = ""
    status: RunStatus = RunStatus.RUNNING
    stop_reason: str = ""
    detail: str = ""
    planning_round: int = 0
    goals: list[dict] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)
    pending_action: dict | None = None
    action_signatures: list[str] = field(default_factory=list)
    observations: list[dict] = field(default_factory=list)
    protections: list[dict] = field(default_factory=list)
    gate_snapshots: dict[str, dict] = field(default_factory=dict)
    suggestions: list[dict] = field(default_factory=list)
    authorization: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.mode not in {"recognition_review", "initial_generation", "user_feedback"}:
            raise ValueError("未知 Agent 运行模式。")
        self.status = RunStatus(self.status)
        if type(self.planning_round) is not int or not 0 <= self.planning_round <= 3:
            raise ValueError("已使用规划轮次无效。")

    def to_dict(self) -> dict:
        value = asdict(self)
        value["status"] = self.status.value
        return value

    @classmethod
    def from_dict(cls, value: dict) -> "AgentState":
        """未知字段明确失败，不把旧状态静默当作可恢复的新运行。"""
        return cls(**deepcopy(value))

    def stop(self, reason: str, detail: str, *, failed: bool = False) -> None:
        self.pending_action = None
        self.status = RunStatus.FAILED if failed else RunStatus.STOPPED
        self.stop_reason = reason
        self.detail = detail

    def pause(self, reason: str, detail: str) -> None:
        self.pending_action = None
        self.status = RunStatus.WAITING_USER
        self.stop_reason = reason
        self.detail = detail
