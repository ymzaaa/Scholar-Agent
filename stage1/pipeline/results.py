# -*- coding: utf-8 -*-
"""流水线各阶段的兼容返回类型。"""

from dataclasses import dataclass, field


##### 抽取与识别结果板块 #####


@dataclass
class ExtractResult:
    """同时提供新内容单元与旧 paragraphs/tables 投影。"""

    paragraphs: list
    tables: list
    content_units: list = field(default_factory=list)
    extraction_report: dict = field(default_factory=dict)
    # 键为图片内容单元编号，值为从 DOCX relationship 读取的原始字节。
    media_assets: dict[str, bytes] = field(default_factory=dict, repr=False)

    @property
    def word_structure(self):
        return {
            "paragraphs": self.paragraphs,
            "tables": self.tables,
            "content_units": self.content_units,
            "extraction_report": self.extraction_report,
        }


@dataclass
class RecognizeResult:
    review: dict
    internal: dict


##### 生成与校验结果板块 #####


@dataclass
class RenderResult:
    output_dir: str
    stats: dict
    template_adapter: object = field(kw_only=True, repr=False)
    chapter_files: list[str] = field(default_factory=list)
    render_trace: dict = field(default_factory=dict)
    fidelity_report: dict = field(default_factory=dict)
    # 来自用户确认事务或已校验可信父版本，不能从待验候选账本自行获得授权。
    confirmed_body_replacements: list[dict] = field(default_factory=list, kw_only=True)


@dataclass
class ProcessResult:
    """一个编译子进程的可审计结果。"""

    label: str
    command: list[str]
    return_code: int | None
    duration_seconds: float
    timed_out: bool = False
    missing_tool: bool = False
    output_complete: bool = True
    stdout_tail: str = ""
    stderr_tail: str = ""


@dataclass
class CompileResult:
    success: bool
    pdf_path: str
    raw_error: str = ""
    errs: int = -1
    started_at_ns: int = 0
    finished_at_ns: int = 0
    process_results: list[ProcessResult] = field(default_factory=list)
    gates: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


@dataclass
class CheckResult:
    all_passed: bool
    fails: list
    results: dict = field(default_factory=dict)
    quality_status: str = "passed"
    publish_allowed: bool = True
    degradations: list = field(default_factory=list)
    counts: dict = field(default_factory=dict)
    template_profile: dict = field(default_factory=dict)
