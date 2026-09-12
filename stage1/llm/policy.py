# -*- coding: utf-8 -*-
"""大语言模型的单次运行授权与结构化执行结果。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Iterable


##### 任务与状态常量板块 #####


HEADING_TRANSLATION_TASK = "heading_translation"
CAPTION_TRANSLATION_TASK = "required_caption_translation"
INITIAL_GENERATION_REPAIR_TASK = "initial_generation_repair"
USER_FEEDBACK_PATCH_TASK = "user_feedback_patch_generation"
FEEDBACK_NORMALIZATION_TASK = "feedback_normalization"
RECOGNITION_REVIEW_TASK = "recognition_review"
KNOWN_LLM_TASKS = frozenset({
    HEADING_TRANSLATION_TASK, CAPTION_TRANSLATION_TASK, INITIAL_GENERATION_REPAIR_TASK,
    USER_FEEDBACK_PATCH_TASK, FEEDBACK_NORMALIZATION_TASK, RECOGNITION_REVIEW_TASK,
})

STATUS_DISABLED = "disabled"
STATUS_NOT_CONFIGURED = "not_configured"
STATUS_SKIPPED_NOT_NEEDED = "skipped_not_needed"
STATUS_SUCCEEDED = "succeeded"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"
STATUS_REJECTED = "rejected"


##### 授权模型板块 #####


@dataclass(frozen=True, slots=True)
class LLMAuthorization:
    """只对当前 generation 生效的大语言模型授权。

    环境变量中的密钥只代表服务器具备调用能力，绝不自动产生授权。
    """

    allowed_tasks: frozenset[str] = field(default_factory=frozenset)
    external_processing_allowed: bool = False
    source: str = "default_offline"
    approved_at: str | None = None

    @classmethod
    def offline(cls) -> "LLMAuthorization":
        return cls()

    @classmethod
    def for_tasks(
        cls,
        tasks: Iterable[str],
        *,
        external_processing_allowed: bool,
        source: str = "cli",
    ) -> "LLMAuthorization":
        normalized = frozenset(task.strip() for task in tasks if task.strip())
        unknown = normalized - KNOWN_LLM_TASKS
        if unknown:
            raise ValueError(f"未知的大语言模型任务：{sorted(unknown)}")
        approved_at = (
            datetime.now(timezone.utc).isoformat()
            if normalized and external_processing_allowed
            else None
        )
        return cls(normalized, external_processing_allowed, source, approved_at)

    def allows_task(self, task: str) -> bool:
        return task in self.allowed_tasks


##### 执行结果板块 #####


@dataclass(slots=True)
class LLMTaskResult:
    """不包含提示词、论文原文或密钥的审计结果。"""

    task: str
    status: str
    requested_count: int = 0
    completed_count: int = 0
    external_call_attempted: bool = False
    provider: str | None = None
    model: str | None = None
    endpoint_host: str | None = None
    blocking: bool = False
    error_code: str | None = None
    detail: str = ""
    updated_files: list[str] = field(default_factory=list)
    input_hashes: list[str] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    calls: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def disabled(cls, task: str, requested_count: int = 0) -> "LLMTaskResult":
        return cls(
            task=task,
            status=STATUS_DISABLED,
            requested_count=requested_count,
            detail="本轮未授权该任务，保持默认离线。",
        )
