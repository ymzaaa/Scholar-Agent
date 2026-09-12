# -*- coding: utf-8 -*-
"""G8-A 对外接口的数据契约。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


##### 项目与任务板块 #####


class CreateProjectResponse(BaseModel):
    project_id: str
    source_sha256: str
    template_id: str


class TemplateSummary(BaseModel):
    template_id: str
    display_name: str
    school: str
    education_level: str
    capabilities: dict[str, str]
    bibtex_supported: bool


class TemplateListResponse(BaseModel):
    templates: list[TemplateSummary]


class TriggerExtractResponse(BaseModel):
    task_id: str
    status: Literal["queued"] = "queued"


class TaskResponse(BaseModel):
    task_id: str
    project_id: str
    task_type: str
    status: Literal["queued", "running", "done", "failed"]
    result: dict[str, Any] | None = None
    user_message: str | None = None


class ErrorResponse(BaseModel):
    error: str
    detail: str


##### 结构确认板块 #####


class HeadingOverride(BaseModel):
    model_config = ConfigDict(extra="forbid")
    unit_id: str
    level: Literal["chapter", "section", "subsection", "body"]


class ObjectBindingOverride(BaseModel):
    model_config = ConfigDict(extra="forbid")
    caption_unit_id: str
    object_unit_ids: list[str]


class CitationOverride(BaseModel):
    """引用确认只接受已有单元编号和二选一决定。"""

    model_config = ConfigDict(extra="forbid")
    unit_id: str
    decision: Literal["body_citation", "non_citation"]


class StructureConfirmationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_revision: int = Field(ge=0)
    heading_overrides: list[HeadingOverride] = Field(default_factory=list)
    object_binding_overrides: list[ObjectBindingOverride] = Field(default_factory=list)
    citation_overrides: list[CitationOverride] = Field(default_factory=list)
    confirmed: bool = True


class StructureResponse(BaseModel):
    project_id: str
    source_sha256: str
    schema_version: str
    revision: int
    confirmed: bool
    headings: list[dict[str, Any]]
    object_bindings: list[dict[str, Any]]
    formula_review: dict[str, Any] = Field(default_factory=dict)
    citation_review: dict[str, Any] = Field(default_factory=dict)
    counts: dict[str, int]
    issues: list[dict[str, Any]] = Field(default_factory=list)


##### Agent 结构审查板块 #####


class RecognitionReviewSuggestion(BaseModel):
    unit_id: str
    suggested_level: Literal["chapter", "section", "subsection", "body"]
    confidence: float | None = Field(default=None, ge=0, le=1)
    rationale: str
    evidence: list[str] = Field(default_factory=list)


class RecognitionReviewResponse(BaseModel):
    run_id: str
    project_id: str
    mode: Literal["recognition_review"] = "recognition_review"
    status: Literal["running", "waiting_user", "validating", "ready", "stopped", "failed"]
    state_revision: int = Field(ge=0)
    base_revision: int = Field(ge=0)
    suggestions: list[RecognitionReviewSuggestion] = Field(default_factory=list)
    model_status: str = "pending"
    detail: str = ""
    task_id: str | None = None
    structure: StructureResponse | None = None
    created_at: str = ""
    updated_at: str = ""


class ResumeRecognitionReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_state_revision: int = Field(ge=0)
    decisions: dict[str, Literal["chapter", "section", "subsection", "body"]]
    object_binding_overrides: list[ObjectBindingOverride] = Field(default_factory=list)
    citation_overrides: list[CitationOverride] = Field(default_factory=list)


##### generation 板块 #####


class GenerateRequest(BaseModel):
    """一次项目生成授权；只允许格式 Agent 已登记的模型任务。"""

    model_config = ConfigDict(extra="forbid")
    structure_revision: int = Field(ge=1)
    external_processing_allowed: bool = False
    allowed_llm_tasks: list[Literal[
        "heading_translation", "required_caption_translation",
        "initial_generation_repair",
    ]] = Field(default_factory=list, max_length=3)


class TriggerGenerationResponse(BaseModel):
    generation_id: str
    task_id: str
    status: Literal["queued"] = "queued"


class GenerationResponse(BaseModel):
    generation_id: str
    project_id: str
    structure_revision: int
    source_sha256: str
    task_id: str | None
    status: Literal[
        "draft", "queued", "running", "success", "degraded", "failed", "reverted"
    ]
    quality_status: str
    detail: str
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    parent_version_id: str | None = None
    version_number: int = Field(ge=1)
    change_origin: str
    created_by: str
    trusted: bool
    review_status: Literal[
        "not_reviewable", "pending", "accepted", "rejected", "superseded"
    ] = "not_reviewable"
    accepted: bool = False
    rollback_target_id: str | None = None
    active_constraints: list[dict[str, Any]] = Field(default_factory=list)
    gate_snapshot: dict[str, Any] = Field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""


##### 可信版本板块 #####


class VersionListResponse(BaseModel):
    project_id: str
    accepted_version_id: str | None = None
    versions: list[GenerationResponse]


##### 版本评审契约板块 #####


class VersionReviewResponse(BaseModel):
    project_id: str
    version_id: str
    previous_accepted_version_id: str | None = None
    accepted_version_id: str | None = None
    review_status: str




##### 修订会话契约板块 #####


class AddRevisionFeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    feedback_text: str = Field(min_length=1, max_length=4000)
    selected_unit_ids: list[str] = Field(default_factory=list, max_length=20)
    page_number: int | None = Field(default=None, ge=1)


class ProcessRevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    external_processing_allowed: bool = False
    allowed_llm_tasks: list[str] = Field(default_factory=list, max_length=4)


class ResumeRevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_run_revision: int = Field(ge=0)
    clarifications: dict[str, list[str]] = Field(default_factory=dict)
    external_processing_allowed: bool = False
    allowed_llm_tasks: list[str] = Field(default_factory=list, max_length=4)
    goal_decisions: list[dict[str, Any]] = Field(default_factory=list, max_length=50)


class RevisionSessionResponse(BaseModel):
    session_id: str
    project_id: str
    base_version_id: str
    current_version_id: str | None = None
    status: Literal[
        "collecting", "queued", "running", "reviewable", "needs_user_input",
        "failed", "accepted", "discarded",
    ]
    state_revision: int = Field(ge=0)
    feedbacks: list[dict[str, Any]] = Field(default_factory=list)
    current_run_id: str | None = None
    current_run_revision: int | None = Field(default=None, ge=0)
    unresolved: list[dict[str, Any]] = Field(default_factory=list)
    current_task_id: str | None = None
    evaluation_summary: dict[str, Any] = Field(default_factory=dict)
    constraint_candidates: list[dict[str, Any]] = Field(default_factory=list)
    goal_drafts: list[dict[str, Any]] = Field(default_factory=list)
    goals: list[dict[str, Any]] = Field(default_factory=list)
    detail: str = ""
    created_at: str = ""
    updated_at: str = ""


class ProcessRevisionResponse(BaseModel):
    session_id: str
    run_id: str
    task_id: str
    status: Literal["queued"] = "queued"


##### 论文级约束契约板块 #####


class ConfirmConstraintRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    supersedes_id: str | None = None


class ConstraintListResponse(BaseModel):
    project_id: str
    constraints: list[dict[str, Any]] = Field(default_factory=list)
