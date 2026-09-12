# -*- coding: utf-8 -*-
"""固定执行内容、结构、编译和动态格式门禁，并保留失败与未运行的区别。"""

from dataclasses import asdict, dataclass, field
from pathlib import Path
import re
from typing import Any

from pipeline.content_fidelity_gate import build_content_fidelity_report, write_fidelity_artifacts
from pipeline.structural_gate import structural_fidelity_gate, FatalStructuralMismatch, FatalChapterReachability
from pipeline.quality_issues import quality_issue, format_issues


##### 门禁结果板块 #####


@dataclass(slots=True)
class GateSnapshot:
    """一次发布门禁的最小结果；raw 不进入 Agent 持久状态。"""

    fidelity_ok: bool
    structure_ok: bool
    compile_ok: bool
    format_publish_allowed: bool
    compile_error: str = ""
    format_blockers: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)
    gate_statuses: dict[str, str] = field(default_factory=dict)
    issues: list[dict[str, Any]] = field(default_factory=list)

    @property
    def blockers(self) -> int:
        if self.gate_statuses:
            return sum(item.get("severity") == "block" for item in self.issues)
        return int(not self.fidelity_ok) + int(not self.structure_ok) + int(
            not self.compile_ok
        ) + len(self.format_blockers)

    @property
    def publish_allowed(self) -> bool:
        return (
            self.fidelity_ok
            and self.structure_ok
            and self.compile_ok
            and self.format_publish_allowed
        )

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value.pop("raw", None)
        return value


@dataclass(slots=True)
class ValidationOutcome:
    fidelity_report: dict[str, Any] = field(default_factory=dict)
    structural_ok: bool = False
    structural_checks: dict[str, Any] = field(default_factory=dict)
    compile_result: Any = None
    check_result: Any = None
    gate_statuses: dict[str, str] = field(default_factory=lambda: {
        'content-fidelity': 'not_run', 'structure': 'not_run',
        'compile': 'not_run', 'format': 'not_run',
    })
    issues: list[dict] = field(default_factory=list)
    report_paths: dict[str, str] = field(default_factory=dict)

    def snapshot(self) -> GateSnapshot:
        """布尔字段仅投影明确执行状态，修复与发布使用同一组状态和问题。"""
        parts = []
        if self.compile_result is not None:
            parts.append(self.compile_result.raw_error)
            for process in self.compile_result.process_results:
                parts.extend([process.stdout_tail, process.stderr_tail])
        return GateSnapshot(
            fidelity_ok=self.gate_statuses['content-fidelity'] in {'passed', 'degraded'},
            structure_ok=self.gate_statuses['structure'] == 'passed',
            compile_ok=self.gate_statuses['compile'] == 'passed',
            format_publish_allowed=self.gate_statuses['format'] in {'passed', 'degraded'},
            compile_error='\n'.join(part for part in parts if part)[-12000:],
            format_blockers=list(self.check_result.fails) if self.check_result is not None else [],
            gate_statuses=dict(self.gate_statuses), issues=[dict(item) for item in self.issues],
        )


##### 门禁执行板块 #####


def run_validation_gates(runner, output_dir, extract_result, recognize_result, render_result, *, report_dir=None):
    """前置阻断后不执行后续门禁；程序错误单独归类，不交给 Agent 猜测修改。"""
    output_dir = Path(output_dir)
    outcome = ValidationOutcome()
    stage = 'content-fidelity'
    try:
        fidelity = build_content_fidelity_report(extract_result, recognize_result, render_result)
        outcome.fidelity_report = fidelity
        render_result.fidelity_report = fidelity
        outcome.report_paths.update(write_fidelity_artifacts(
            output_dir / 'reports', render_result.render_trace, fidelity,
        ))
        if report_dir is not None:
            outcome.report_paths.update(write_fidelity_artifacts(report_dir, render_result.render_trace, fidelity))
        for code in fidelity.get('blocking_or_unimplemented', []):
            metric = fidelity.get('metrics', {}).get(code, {})
            outcome.issues.append(quality_issue(stage, code, metric.get('detail', code)))
        outcome.gate_statuses[stage] = 'passed' if fidelity.get('all_passed') else 'failed'
        for code, metric in fidelity.get('metrics', {}).items():
            if metric.get('status') == 'degraded':
                outcome.issues.append(quality_issue(stage, code, metric.get('detail', code), severity='degrade'))
                if fidelity.get('all_passed'):
                    outcome.gate_statuses[stage] = 'degraded'
        if not fidelity.get('all_passed'):
            if not outcome.issues:
                outcome.issues.append(quality_issue(stage, 'content-fidelity-failed', '内容完整性核对未通过。'))
            return outcome

        stage = 'structure'
        adapter = runner.template_adapter
        try:
            outcome.structural_checks = structural_fidelity_gate(
                extract_result.word_structure, output_dir / adapter.content_directory,
                main_tex_path=output_dir / adapter.entrypoint, chapter_files=render_result.chapter_files,
                template_adapter=adapter, confirmed_structure=recognize_result.review,
            )
        except (FatalStructuralMismatch, FatalChapterReachability) as exc:
            outcome.structural_checks = {'error': str(exc), **getattr(exc, 'checks', {})}
            outcome.gate_statuses[stage] = 'failed'
            outcome.issues.append(quality_issue(stage, 'confirmed-structure-mismatch', str(exc)))
            return outcome
        outcome.structural_ok = True
        outcome.gate_statuses[stage] = 'passed'

        stage = 'compile'
        outcome.compile_result = runner.compile(str(output_dir))
        outcome.gate_statuses[stage] = 'passed' if outcome.compile_result.success else 'failed'
        if not outcome.compile_result.success:
            processes = outcome.compile_result.process_results
            error = outcome.snapshot().compile_error
            code = ('compile-tool-missing' if any(item.missing_tool for item in processes) else
                    'compile-timeout' if any(item.timed_out for item in processes) else
                    'compile-resource-missing' if re.search(r"File .+ not found", error) else 'latex-compile-failed')
            # 只有已知局部语法允许进入后续授权；未知环境名和运行环境故障不可猜测修复。
            local_syntax = bool(re.search(
                r"Extra \}|Too many \}'s\.|Missing \} inserted|Missing \$ inserted|"
                r"begin\{(?:center|figure|table|itemize|enumerate)\}"
                r"(?:\s+on input line \d+)?\s+ended by", error,
            ))
            outcome.issues.append(quality_issue(stage, code, error,
                repairable=code == 'latex-compile-failed' and local_syntax))
            return outcome

        stage = 'format'
        outcome.check_result = runner.check_format(str(output_dir))
        outcome.issues.extend(format_issues(outcome.check_result.results))
        internal = any(item.get('status') == 'internal_error' for item in outcome.check_result.results.values())
        outcome.gate_statuses[stage] = (
            'internal_error' if internal else
            'failed' if not outcome.check_result.publish_allowed else
            'degraded' if outcome.check_result.quality_status == 'degraded' else 'passed'
        )
        return outcome
    except Exception as exc:
        outcome.gate_statuses[stage] = 'internal_error'
        outcome.issues.append(quality_issue(stage, stage + '-internal-error',
            f'{type(exc).__name__}: {exc}', action='这是内部处理错误，请保存报告并联系维护人员。'))
        return outcome
