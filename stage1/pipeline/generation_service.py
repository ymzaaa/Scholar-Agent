# -*- coding: utf-8 -*-
"""确认结构驱动的 Stage1 generation 服务，供 HTTP 与未来 CLI 共同复用。"""

from __future__ import annotations

import json
import hashlib
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import get_settings
from llm.client import LLMClient
from llm.policy import (
    CAPTION_TRANSLATION_TASK, HEADING_TRANSLATION_TASK, LLMAuthorization,
    LLMTaskResult, STATUS_SKIPPED_NOT_NEEDED,
)
from pipeline.bibliography import load_explicit_mapping
from pipeline.confirmed_structure import apply_confirmed_structure
from pipeline.quality_report import build_user_quality_report
from pipeline.quality_issues import failure_report, quality_issue
from pipeline.source_package import package_latex_source
from pipeline.translate import (
    translate_headings_for_toc, translate_required_captions,
)
from pipeline.validation_runner import run_validation_gates
from pipeline.workspace import WorkspaceManager, _write_json as atomic_write_json


##### 报告序列化板块 #####


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _compile_report(result: Any) -> dict[str, Any]:
    if result is None:
        return {"success": False, "status": "not_run"}
    return {
        "success": result.success,
        "pdf_path": result.pdf_path,
        "errs": result.errs,
        "raw_error": result.raw_error,
        "started_at_ns": result.started_at_ns,
        "finished_at_ns": result.finished_at_ns,
        "processes": [asdict(item) for item in result.process_results],
        "gates": result.gates,
        "warnings": result.warnings,
    }


def _format_report(result: Any) -> dict[str, Any]:
    if result is None:
        return {"status": "not_run"}
    return {
        "quality_status": result.quality_status,
        "publish_allowed": result.publish_allowed,
        "all_passed": result.all_passed,
        "counts": result.counts,
        "blockers": result.fails,
        "degradations": result.degradations,
        "template": result.template_profile,
        "rules": result.results,
    }


def _gate_snapshot(
    outcome: Any, *, published: bool, quality_status: str, snapshot=None,
    agent_authorization: dict[str, Any] | None = None,
) -> dict[str, Any]:
    snapshot = snapshot or outcome.snapshot()
    result = {
        "schema_version": "1.0.0",
        "published": published,
        "quality_status": quality_status,
        "fidelity_ok": snapshot.fidelity_ok,
        "structure_ok": snapshot.structure_ok,
        "compile_ok": snapshot.compile_ok,
        "format_publish_allowed": snapshot.format_publish_allowed,
        "blocker_count": snapshot.blockers,
        "gate_statuses": dict(snapshot.gate_statuses),
        "issues": list(snapshot.issues),
        "failure_stage": next((stage for stage, state in snapshot.gate_statuses.items() if state in {"failed", "internal_error"}), None),
        "report_paths": dict(outcome.report_paths),
    }
    if agent_authorization is not None:
        result["agent_authorization"] = agent_authorization
    return result


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, payload)


def _translation_snapshot(
    outcome: Any, tasks: list[LLMTaskResult], authorization: LLMAuthorization,
):
    """只标注已知语言问题的修复资格，沿用实际门禁严重度。"""
    snapshot = outcome.snapshot()
    task_rules = {
        HEADING_TRANSLATION_TASK: ("FORMAT-REQUIRED-HEADINGS", {"OUC-TOC-ENGLISH-HEADINGS"}),
        CAPTION_TRANSLATION_TASK: ("FORMAT-REQUIRED-CAPTIONS", {"OUC-FIGURE-ENGLISH-CAPTION", "OUC-TABLE-ENGLISH-CAPTION"}),
    }
    for task in tasks:
        if (task.task not in task_rules or task.requested_count <= task.completed_count
            or not authorization.external_processing_allowed or not authorization.allows_task(task.task)):
            continue
        canonical, rule_ids = task_rules[task.task]
        for issue in snapshot.issues:
            if issue["code"] in rule_ids:
                issue["repairable"] = True
        for blocker in snapshot.format_blockers:
            if blocker.get("rule_id") in rule_ids:
                blocker["rule_id"] = canonical
    return snapshot


##### 首次统一修复板块 #####


def _initial_repair_request(*, output_dir, generation_id, rendered, snapshot, tasks, agent_state):
    """只为尚未调用模型的已定位阻断问题提供具体说明；不改变可信首版的交付规则。"""
    import re
    from agents.quality_repair.runtime import locate_initial_compile_target, MAX_PLANNING_ROUNDS
    from llm.policy import INITIAL_GENERATION_REPAIR_TASK

    if (snapshot.publish_allowed or not snapshot.fidelity_ok or not snapshot.structure_ok
        or agent_state is not None or any(item.external_call_attempted for item in tasks)
        or snapshot.gate_statuses.get('compile') != 'failed'
        or not any(item.get('check_type') == 'compile' and item.get('repairable') for item in snapshot.issues)):
        return None
    context = locate_initial_compile_target(output_dir, generation_id=generation_id,
        diagnostics=snapshot.compile_error, records=rendered.render_trace.get('records', []))
    if context is None:
        return None
    selected = [{'task': INITIAL_GENERATION_REPAIR_TASK, 'title': '修复已定位的编译语法问题',
                 'data_scope': '一个已定位内容单元的局部 LaTeX、局部编译诊断及模板能力摘要。',
                 'item_count': 1}]
    calls = MAX_PLANNING_ROUNDS
    names = {HEADING_TRANSLATION_TASK: '补齐模板必需英文目录',
             CAPTION_TRANSLATION_TASK: '补齐模板必需英文题注'}
    for item in tasks:
        missing = item.requested_count - item.completed_count
        if item.task in names and missing > 0 and item.status not in {'failed', 'partial', 'rejected'}:
            selected.append({'task': item.task, 'title': names[item.task], 'item_count': missing,
                             'data_scope': f'{missing} 条缺少英文的已确认标题或题注，每批最多 15 条。'})
            calls += (missing + 14) // 15
    settings = get_settings()
    name = settings.llm_model
    if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_./-]{0,119}', name):
        name = '已配置模型' if settings.llm_configured else '尚未配置'
    return {'generation_id': generation_id, 'tasks': selected, 'max_calls': calls,
            'model': {'provider': {'openai': 'OpenAI 兼容服务', 'anthropic': 'Anthropic'}.get(
                settings.llm_provider, '模型服务'), 'name': name, 'configured': bool(settings.llm_configured)}}


def _run_initial_repair(
    *, runner, output_dir, report_dir, generation_id, project_id, template_id, source_sha256,
    rendered, outcome, translation_tasks, validate, authorization, client, on_agent_state=None,
):
    """有明确可处理目标才建立运行；语言准备、编译和终验由外层执行。"""
    from copy import deepcopy
    from langgraph.checkpoint.sqlite import SqliteSaver
    from agents.quality_repair.models import AgentState
    from agents.quality_repair.runtime import (
        RepairRuntime, AuthorizedPlanningModel, locate_initial_compile_target, initial_syntax_action,
    )
    from agents.quality_repair.graph_executor import build_quality_graph
    from agents.quality_repair.tools import QualityTools
    from agents.quality_repair.patching import WriteRegion
    from agents.quality_repair.action_journal import FileActionJournal
    from llm.policy import INITIAL_GENERATION_REPAIR_TASK
    from pipeline.translate import prepare_required_language
    from pipeline.content_fidelity_gate import _parse_marked_files
    from pipeline.revision_validation import _journal_changes

    snapshot = outcome.snapshot()
    configured = getattr(client, 'configured', False) and authorization.external_processing_allowed
    if not snapshot.fidelity_ok or not snapshot.structure_ok:
        return outcome, None, []
    categories = [category for category, task_name in (
        ('heading', HEADING_TRANSLATION_TASK), ('caption', CAPTION_TRANSLATION_TASK))
        if configured and authorization.allows_task(task_name) and any(task.task == task_name
            and not task.external_call_attempted and task.status not in {'failed', 'rejected', 'partial'}
            and task.requested_count > task.completed_count for task in translation_tasks)]
    context = None
    if not snapshot.compile_ok:
        if not any(issue.get('check_type') == 'compile' and issue.get('repairable') for issue in snapshot.issues):
            return outcome, None, []
        context = locate_initial_compile_target(output_dir, generation_id=generation_id,
            diagnostics=snapshot.compile_error, records=rendered.render_trace.get('records', []))
        if context is None:
            return outcome, None, []
        deterministic = initial_syntax_action(output_dir, generation_id=generation_id, context=context)
        if deterministic is None and (not configured or not authorization.allows_task(INITIAL_GENERATION_REPAIR_TASK)):
            return outcome, None, []
    if not categories and context is None:
        return outcome, None, []

    goals = [{'goal_id': 'language-' + category, 'kind': 'template_language', 'confirmed': True,
              'target_unit_ids': [], 'status': 'pending', 'description': '补齐模板声明的必需英文。'}
             for category in categories]
    if context is not None:
        goals.append({'goal_id': 'compile-' + context['unit_id'], 'kind': 'format', 'confirmed': True,
                      'target_unit_ids': [context['unit_id']], 'status': 'pending',
                      'description': '保持原文及语义对象，修正已定位局部语法：' + context['diagnostics']})
    state = AgentState(run_id=generation_id, project_id=project_id, mode='initial_generation',
        template_id=template_id, parent_source_sha256=source_sha256, goals=goals,
        authorization={'external_processing_allowed': authorization.external_processing_allowed,
                       'allowed_llm_tasks': sorted(authorization.allowed_tasks)})
    original_trace = deepcopy(rendered.render_trace)
    current_outcome, task_reports, language_ids = outcome, [], set()
    journal = None

    def persist(value):
        if on_agent_state is not None:
            on_agent_state(deepcopy(value))

    try:
        persist(state.to_dict())
        journal = FileActionJournal(output_dir, report_dir / 'agent-actions', state.run_id,
                                    generation_id=generation_id)
        if categories:
            prepared = prepare_required_language(output_dir, generation_id=generation_id,
                template_adapter=runner.template_adapter, heading_records=rendered.render_trace.get('headings', []),
                categories=categories, authorization=authorization, client=client,
                on_task_report=lambda report: task_reports.append(deepcopy(report)))
            task_reports = prepared['tasks']
            for item in prepared['actions']:
                observation = journal.apply(item['action_id'], item['prepared'], proposal=item['proposal'])
                language_ids.add(item['action_id'])
                state.observations.append({'kind': 'template_language', **observation})
            for goal in state.goals:
                if goal['kind'] == 'template_language':
                    goal['status'] = 'satisfied'
            persist(state.to_dict())
        before, _, _ = _parse_marked_files(output_dir, rendered.chapter_files)
        regions = [WriteRegion(context['relative_path'], context['unit_id'], context['role'])] if context else []
        tools = QualityTools(root=output_dir, generation_id=generation_id, regions=regions,
            evidence=[context] if context else [], journal=journal,
            template_context={'template_id': template_id, 'required_language': categories},
            quality_report={'issues': [{'check_type': 'compile', 'description': context['diagnostics']}]} if context else {},
            compile_diagnostics=context['diagnostics'] if context else '')

        def planning_context(current):
            evidence = []
            if context:
                actual = tools._source(context['unit_id'])
                offset = min(context['offset'], len(actual['text']))
                evidence = [{**actual, 'text': actual['text'][offset:offset + 4000], 'offset': offset}]
            return {'goals': current.goals, 'evidence': evidence, 'planning_round': current.planning_round,
                    'diagnostics': tools.compile_diagnostics, 'template_context': tools.template_context,
                    'last_observation': current.observations[-1] if current.observations else {}}

        def observe(current, observation):
            compiled = runner.compile(str(output_dir))
            if compiled.success:
                return {'goal_satisfied': True, 'completed_goal_ids': [goal['goal_id'] for goal in current.goals
                        if goal['kind'] == 'format']}
            from pipeline.validation_runner import ValidationOutcome
            import re
            diagnostic = ValidationOutcome(compile_result=compiled).snapshot().compile_error
            if (any(process.missing_tool or process.timed_out for process in compiled.process_results)
                or re.search(r'File .+ not found', diagnostic)):
                return {'goal_satisfied': False, 'stop_reason': 'unrepairable_compile',
                        'detail': '编译工具、资源或运行环境异常，不能交给模型修改源码。'}
            located = locate_initial_compile_target(output_dir, generation_id=generation_id,
                diagnostics=diagnostic, records=original_trace.get('records', []))
            if located is None or located['unit_id'] != context['unit_id']:
                return {'goal_satisfied': False, 'stop_reason': 'unrepairable_compile',
                        'detail': '后续编译问题不能在当前已确认目标中安全处理。'}
            tools.compile_diagnostics = located['diagnostics']
            return {'goal_satisfied': False}

        def final_validate(current):
            nonlocal current_outcome
            actual, _, _ = _parse_marked_files(output_dir, rendered.chapter_files)
            entries = [entry for entry in journal.entries() if entry['action_id'] not in language_ids]
            changes = _journal_changes(entries, goals=current.goals, before=before, actual=actual)
            rendered.render_trace = {**deepcopy(original_trace),
                'approved_changes': deepcopy(original_trace.get('approved_changes', [])) + changes}
            current_outcome = validate()
            return {'gate_statuses': dict(current_outcome.gate_statuses), 'issues': deepcopy(current_outcome.issues)}

        runtime = RepairRuntime(workspace_root=output_dir,
            model=AuthorizedPlanningModel(client, authorization, INITIAL_GENERATION_REPAIR_TASK), tools=tools,
            context=planning_context, observe=observe, final_validate=final_validate,
            rollback_all=journal.rollback_all, rollback_action=lambda action: journal.rollback(action['action_id']),
            parent_identity=lambda: (None, source_sha256),
            authorized=lambda current: configured and authorization.allows_task(INITIAL_GENERATION_REPAIR_TASK),
            deterministic_action=lambda current: initial_syntax_action(output_dir,
                generation_id=generation_id, context=context) if not current.observations else None,
            journal_root=report_dir / 'agent-actions')
        with SqliteSaver.from_conn_string(str(report_dir / 'agent-checkpoints.sqlite')) as saver:
            graph = build_quality_graph(runtime=runtime, checkpointer=saver)
            for value in graph.stream({'agent': state.to_dict()},
                config={'configurable': {'thread_id': state.run_id}}, stream_mode='values'):
                state = AgentState.from_dict(value['agent'])
                persist(state.to_dict())
    except Exception as exc:
        state.stop('internal_error', f'自动修复执行异常：{type(exc).__name__}', failed=True)
        if journal is not None:
            try:
                journal.rollback_all()
            except Exception as cleanup:
                state.stop('rollback_failed', f'修复回滚异常：{type(cleanup).__name__}', failed=True)
    if state.status != 'ready':
        rendered.render_trace = original_trace
        current_outcome = deepcopy(outcome)
        stage = 'format' if snapshot.compile_ok else 'compile'
        current_outcome.gate_statuses[stage] = 'internal_error' if state.stop_reason == 'internal_error' else 'failed'
        current_outcome.issues.append(quality_issue(stage, 'automatic-repair-failed',
            '自动修复未通过安全校验或完整终验，本次改动未交付。'))
        task_reports = [{**report, 'status': 'failed', 'completed_count': 0, 'blocking': True,
            'updated_files': [], 'detail': '模板语言任务未完成完整终验，未交付本次改动。'}
            for report in task_reports]
    persist(state.to_dict())
    return current_outcome, state.to_dict(), task_reports


##### LaTeX 产物板块 #####


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _package_latex_source(staging: Path) -> Path:
    """只打包可重建 PDF 的源码与图片，不纳入编译缓存和生成 PDF。"""
    archive, _ = package_latex_source(staging)
    return archive


def _artifact_manifest(output_dir: Path | None, report_dir: Path) -> dict[str, Any]:
    artifacts: list[dict[str, Any]] = []
    if output_dir:
        for kind, relative in (("pdf", "main.pdf"), ("latex_source", "latex-source.zip")):
            path = output_dir / relative
            if path.is_file():
                artifacts.append({
                    "kind": kind, "name": path.name, "root": "output",
                    "relative_path": relative, "size": path.stat().st_size,
                    "sha256": _sha256_file(path),
                })
        final_reports = output_dir / "reports"
        if final_reports.is_dir():
            for path in sorted(final_reports.glob("*.json")):
                artifacts.append({
                    "kind": "report", "name": path.name, "root": "output",
                    "relative_path": f"reports/{path.name}", "size": path.stat().st_size,
                    "sha256": _sha256_file(path),
                })
    for path in sorted(report_dir.glob("*.json")):
        if any(item["name"] == path.name and item["kind"] == "report" for item in artifacts):
            continue
        artifacts.append({
            "kind": "report", "name": path.name, "root": "report",
            "relative_path": path.name, "size": path.stat().st_size,
            "sha256": _sha256_file(path),
        })
    return {"version": "1.0.0", "artifacts": artifacts}


##### generation 执行板块 #####


def run_confirmed_generation(
    *,
    runner: Any,
    workspace_root: str,
    project_id: str,
    generation_id: str,
    bib_path: str | None,
    citation_map_path: str | None,
    reference_source: str,
    template_dir: str,
    template_id: str,
    source_sha256: str,
    structure_revision: int,
    confirmed_snapshot_path: str,
    version_context: dict[str, Any] | None = None,
    external_processing_allowed: bool = False,
    allowed_llm_tasks: list[str] | None = None,
    on_agent_state=None,
) -> dict[str, Any]:
    """运行 确认结构渲染与发布门禁；模板必需语言内容和允许问题由统一 Agent 修复。"""
    manager = WorkspaceManager(workspace_root)
    project = manager.get_project(project_id)
    from content_extraction.persistence import load_extraction

    extract_result = load_extraction(
        project.project_dir, source_sha256=source_sha256,
    )
    generation = manager.begin_generation(
        project_id, generation_id, version_context=version_context,
    )
    report_dir = project.reports_dir / generation_id
    owns_report = False
    pending_report = report_dir / ".pipeline-ready"
    failure_stage = "generation"
    report: dict[str, Any] = {
        "timestamp": _utc_now(), "project_id": project_id,
        "generation_id": generation_id, "structure_revision": structure_revision,
        "source_sha256": source_sha256, "published": False,
        "template": {
            "template_id": template_id,
            "adapter": runner.template_adapter.name,
        },
        "version": version_context or {},
    }

    try:
        with generation:
            report_dir.mkdir(parents=True, exist_ok=False)
            owns_report = True
            snapshot = json.loads(Path(confirmed_snapshot_path).read_text(encoding="utf-8"))
            output_dir = generation.staging_dir
            recognized = apply_confirmed_structure(
                extract_result, None, snapshot,
                source_sha256=source_sha256,
                structure_revision=structure_revision,
            )
            formula_review = recognized.review.get("formula_review", {})
            if formula_review.get("status") != "passed":
                report.update({"formula_preflight": formula_review, "llm_tasks": []})
                report.update(failure_report('content-fidelity', 'formula-preflight-failed',
                    '公式预检未通过，已在渲染和编译前阻断。'))
                _write_json(report_dir / "pipeline_log.json", report)
                return {
                    "status": "failed", "quality_status": "blocked",
                    "output_dir": None, "report_dir": str(report_dir),
                    "artifact_manifest": _artifact_manifest(None, report_dir),
                    "detail": "检测到异常公式，已在渲染和编译前阻断。",
                    "gate_snapshot": report["gate_snapshot"],
                }
            recognized.internal["citation_mapping"] = load_explicit_mapping(citation_map_path)
            recognized.internal["reference_source"] = {
                "auto": "auto", "word_list": "word", "bib": "bibtex"
            }[reference_source]
            rendered = runner.render(
                extract_result, recognized, template_dir, str(output_dir), bib_path
            )
            language_capable = (
                runner.template_manifest is not None
                and runner.template_manifest.capabilities.get(
                    "required_english_translation"
                ) == "supported"
            )
            profile = (
                runner.template_adapter.format_contract()
                if language_capable else None
            )
            authorization = LLMAuthorization.for_tasks(
                allowed_llm_tasks or [],
                external_processing_allowed=external_processing_allowed,
                source="project_generation_consent",
            )
            # 本次具体授权不包含网络重发；不改写共享设置或历史兼容客户端。
            client = LLMClient(replace(get_settings(), llm_max_retries=0))
            if profile is not None:
                heading_translation = translate_headings_for_toc(
                    str(output_dir), profile=profile,
                    template_adapter=runner.template_adapter,
                    heading_records=rendered.render_trace.get("headings", []),
                    authorization=LLMAuthorization.offline(),
                    allow_degraded_without_translation=False,
                )
                translation = translate_required_captions(
                    str(output_dir), profile=profile,
                    template_adapter=runner.template_adapter,
                    heading_records=rendered.render_trace.get("headings", []),
                    authorization=LLMAuthorization.offline(),
                    allow_degraded_without_translation=False,
                )
            else:
                heading_translation = LLMTaskResult(
                    task=HEADING_TRANSLATION_TASK,
                    status=STATUS_SKIPPED_NOT_NEEDED,
                    detail="当前模板未声明英文目录能力。",
                )
                translation = LLMTaskResult(
                    task=CAPTION_TRANSLATION_TASK,
                    status=STATUS_SKIPPED_NOT_NEEDED,
                    detail="当前模板未声明英文题注能力。",
                )
            outcome = run_validation_gates(
                runner, output_dir, extract_result, recognized, rendered,
                report_dir=report_dir,
            )
            translation_tasks = [heading_translation, translation]

            def rerun_gates():
                return run_validation_gates(
                    runner, output_dir, extract_result, recognized, rendered,
                    report_dir=report_dir,
                )

            outcome, agent_state, repair_task_reports = _run_initial_repair(
                runner=runner, output_dir=output_dir, report_dir=report_dir,
                generation_id=generation_id, project_id=project_id, template_id=template_id,
                source_sha256=source_sha256, rendered=rendered, outcome=outcome,
                translation_tasks=translation_tasks, validate=rerun_gates,
                authorization=authorization, client=client, on_agent_state=on_agent_state,
            )
            final_snapshot = _translation_snapshot(outcome, translation_tasks, authorization)
            publish_allowed = bool(final_snapshot.publish_allowed)
            check_result = outcome.check_result
            quality_status = (
                getattr(check_result, "quality_status", "passed")
                if publish_allowed else "blocked"
            )
            if 'internal_error' in final_snapshot.gate_statuses.values():
                quality_status = 'internal_error'
            elif publish_allowed and any(item['severity'] == 'degrade' for item in final_snapshot.issues):
                quality_status = 'degraded'
            bibliography = rendered.render_trace.get("bibliography", {})
            bibliography_degraded = bool(
                bibliography.get("transaction_status") == "rolled_back"
                and bibliography.get("required_numbers")
            )
            if publish_allowed and bibliography_degraded:
                quality_status = "degraded"
            task_reports = {
                item.task: item.to_dict() for item in translation_tasks
            }
            task_reports.update({str(item["task"]): item for item in repair_task_reports})
            report.update({
                "published": False,
                "publish_allowed": publish_allowed,
                "quality_status": quality_status,
                "stats": rendered.stats,
                "llm_tasks": list(task_reports.values()),
                "agent_run": agent_state,
                "content_fidelity": outcome.fidelity_report,
                "structural_gate": outcome.structural_checks,
                "compile": _compile_report(outcome.compile_result),
                "format_check": _format_report(check_result),
                "gate_statuses": dict(final_snapshot.gate_statuses),
                "issues": list(final_snapshot.issues),
            })
            report['repair_request'] = _initial_repair_request(
                output_dir=output_dir, generation_id=generation_id, rendered=rendered,
                snapshot=final_snapshot, tasks=translation_tasks, agent_state=agent_state)
            report["user_report"] = build_user_quality_report(report)
            report["gate_snapshot"] = _gate_snapshot(
                outcome, published=False, quality_status=quality_status,
                snapshot=final_snapshot,
                agent_authorization={
                    "external_processing_allowed": authorization.external_processing_allowed,
                    "allowed_llm_tasks": sorted(authorization.allowed_tasks),
                    "source": authorization.source,
                },
            )
            failure_stage = "delivery"
            _write_json(report_dir / "pipeline_log.json", report)
            _write_json(output_dir / "reports" / "pipeline_log.json", report)
            if not publish_allowed:
                blocked_status = "failed"
                return {
                    "status": blocked_status, "quality_status": quality_status,
                    "output_dir": None, "report_dir": str(report_dir),
                    "artifact_manifest": _artifact_manifest(None, report_dir),
                    "detail": "生成门禁未通过，未发布产物。",
                    "gate_snapshot": report["gate_snapshot"],
                }
            _package_latex_source(output_dir)
            # 正式目录内保存提交后的报告表示；它在提交前只存在于私有 staging。
            committed_report = {**report, "published": True,
                                "gate_snapshot": {**report["gate_snapshot"], "published": True}}
            committed_report["user_report"] = build_user_quality_report(committed_report)
            _write_json(output_dir / "reports" / "pipeline_log.json", committed_report)
            _write_json(pending_report, committed_report)
            manifest = _artifact_manifest(output_dir, report_dir)
            final_dir = generation.publish()
            report = committed_report
            pending_report.replace(report_dir / "pipeline_log.json")
            return {
                "status": "success" if quality_status == "passed" else "degraded",
                "quality_status": quality_status,
                "output_dir": str(final_dir), "report_dir": str(report_dir),
                "artifact_manifest": manifest,
                "detail": "已通过发布门禁。" if quality_status == "passed" else "已发布，存在明确降级项。",
                "gate_snapshot": report["gate_snapshot"],
            }
    except Exception as exc:
        report.update({"published": generation._published, "quality_status": "internal_error",
                       "failure_stage": failure_stage, "error": str(exc)})
        issue = quality_issue(failure_stage, failure_stage + "-internal-error",
                              f"{type(exc).__name__}: {exc}", action="请保存诊断报告并联系维护人员。")
        report["issues"] = [*report.get("issues", []), issue]
        report["gate_snapshot"] = {**report.get("gate_snapshot", {}),
            "published": generation._published, "quality_status": "internal_error",
            "failure_stage": failure_stage, "issues": report["issues"]}
        report["user_report"] = build_user_quality_report(report)
        if owns_report:
            try:
                pending_report.unlink(missing_ok=True)
                _write_json(report_dir / "pipeline_log.json", report)
            except Exception as report_error:
                exc.add_note(f"生成诊断报告写入失败：{report_error}")
                try:
                    if report_dir.resolve().parent == project.reports_dir.resolve() and not any(report_dir.iterdir()):
                        report_dir.rmdir()
                except Exception as cleanup:
                    exc.add_note(f"空报告目录清理失败：{cleanup}")
        raise
