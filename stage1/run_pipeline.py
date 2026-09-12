# -*- coding: utf-8 -*-
"""run_pipeline.py — CLI entry point for the Word→LaTeX pipeline.

Uses PipelineRunner from pipeline_api for staged execution:
  extract → recognize → render → translate → structural_gate → compile → check_format

All business logic lives in pipeline_api.py (zero logic changes from original).
This file is now a thin CLI wrapper.
"""

import argparse, json, os, sys
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PIPELINE_DIR = Path(__file__).resolve().parent / 'pipeline'
sys.path.insert(0, str(PIPELINE_DIR.parent))

# ---- Import from pipeline_api (all business logic) ----
from pipeline_api import PipelineRunner
from pipeline.workspace import WorkspaceManager, default_workspace_root
from llm.policy import (
    CAPTION_TRANSLATION_TASK, HEADING_TRANSLATION_TASK,
    KNOWN_LLM_TASKS,
    LLMAuthorization,
)
from pipeline.bibliography import load_explicit_mapping
from pipeline.validation_runner import run_validation_gates
from pipeline.generation_service import _run_initial_repair
from template_registry.registry import resolve_builtin_template


##### 报告序列化板块 #####


def _compile_report(result):
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


def _write_report(report_dir, report):
    os.makedirs(report_dir, exist_ok=True)
    path = os.path.join(report_dir, "pipeline_log.json")
    with open(path, "w", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    print(f"[Done] {path}")


def _quality_exit_code(publish_allowed, quality_status):
    """CLI用0表示完全通过、2表示已发布但降级、1表示阻断。"""
    if not publish_allowed:
        return 1
    return 0 if quality_status == 'passed' else 2


def main():
    parser = argparse.ArgumentParser(description='Generic Word→LaTeX pipeline')
    parser.add_argument('--docx', required=True)
    parser.add_argument('--bib', default=None, help='可选BibTeX；不提供时尝试使用Word文末参考文献列表')
    parser.add_argument('--citation-map', default=None, help='可选JSON：正文编号到BibTeX键的显式映射')
    parser.add_argument('--reference-source', choices=['auto', 'bibtex', 'word'], default='auto')
    parser.add_argument(
        '--template-id', choices=['ouc-graduate', 'ouc-bachelor'],
        default='ouc-graduate',
    )
    parser.add_argument(
        '--workspace-root',
        default=str(default_workspace_root()),
        help='系统任务工作区根目录；不接受最终输出目录',
    )
    parser.add_argument(
        '--llm-task',
        action='append',
        choices=sorted(KNOWN_LLM_TASKS),
        default=[],
        help='显式授权本轮使用的大语言模型任务；默认不授权任何任务',
    )
    parser.add_argument(
        '--allow-external-llm',
        action='store_true',
        help='确认本轮允许把所选任务的最小必要文本发送到配置的外部接口',
    )
    parser.add_argument(
        '--required-caption-mode', choices=['auto', 'offline'], default='auto',
        help='auto 仅执行本轮明确授权的语言任务；offline 不调用翻译模型',
    )
    args = parser.parse_args()
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    llm_authorization = LLMAuthorization.for_tasks(
        [task for task in args.llm_task if args.required_caption_mode != 'offline'
         or task not in {HEADING_TRANSLATION_TASK, CAPTION_TRANSLATION_TASK}],
        external_processing_allowed=args.allow_external_llm,
        source='cli',
    )

    resolved_template = resolve_builtin_template(args.template_id)
    runner = PipelineRunner(resolved_template=resolved_template)
    template_source = str(
        PROJECT_ROOT.joinpath(*resolved_template.manifest.source_path.split('/'))
    )
    manager = WorkspaceManager(args.workspace_root)
    project = manager.create_project(
        source_paths={
            'docx': args.docx,
            'bib': args.bib,
            'citation_map': args.citation_map,
            'template': template_source,
        }
    )
    generation = manager.begin_generation(project.project_id)
    report_dir = project.reports_dir / timestamp
    print(f'[Project] project_id={project.project_id}')
    print(f'[Generation] generation_id={generation.generation_id}')

    try:
        with generation:
            output_dir = str(generation.staging_dir)
            print(f'[模板准备] {args.template_id} → staging')

            # Stage 1: 输入只读抽取。
            extract_result = runner.extract(args.docx)
            print(
                f'[内容抽取] {len(extract_result.paragraphs)} paragraphs, '
                f'{len(extract_result.tables)} tables'
            )
            recognize_result = runner.recognize(extract_result)
            from content_extraction.formula_validation import ensure_formula_preflight

            ensure_formula_preflight(
                recognize_result.review.get('formula_review', {})
            )
            from pipeline.confirmed_structure import resolve_render_structure

            recognize_result = resolve_render_structure(extract_result, recognize_result)
            # 引用配置随确认后的识别结果传递，保持分阶段接口可序列化。
            recognize_result.internal['citation_mapping'] = load_explicit_mapping(
                args.citation_map
            )
            recognize_result.internal['reference_source'] = args.reference_source

            print('[前置内容] Generating front matter...')
            print('[章节渲染] Generating chapters...')
            render_result = runner.render(
                extract_result,
                recognize_result,
                template_source,
                output_dir,
                args.bib,
            )
            stats = render_result.stats

            print('[必需语言检查] Deterministic metadata and optional LLM tasks...')
            from pipeline.translate import (
                translate_headings_for_toc, translate_required_captions,
            )
            template_adapter = runner.template_adapter
            caption_authorization = llm_authorization
            profile = (
                template_adapter.format_contract()
                if resolved_template.manifest.capabilities.get(
                    'required_english_translation'
                ) == 'supported' else None
            )
            heading_translation = translate_headings_for_toc(
                output_dir,
                profile=profile,
                template_adapter=template_adapter,
                heading_records=render_result.render_trace.get('headings', []),
                authorization=caption_authorization,
                allow_degraded_without_translation=(
                    args.required_caption_mode == 'offline'
                ),
            )
            caption_translation = translate_required_captions(
                output_dir,
                profile=profile,
                template_adapter=template_adapter,
                heading_records=render_result.render_trace.get('headings', []),
                authorization=caption_authorization,
                allow_degraded_without_translation=(
                    args.required_caption_mode == 'offline'
                ),
            )
            print(
                f'  [LLM] {heading_translation.task}: '
                f'{heading_translation.status} '
                f'({heading_translation.completed_count}/'
                f'{heading_translation.requested_count})'
            )
            print(
                f'  [LLM] {caption_translation.task}: '
                f'{caption_translation.status} '
                f'({caption_translation.completed_count}/'
                f'{caption_translation.requested_count})'
            )

            print('[质量门禁] Running content, structure, compile and format gates...')
            outcome = run_validation_gates(
                runner,
                output_dir,
                extract_result,
                recognize_result,
                render_result,
                report_dir=report_dir,
            )
            from pipeline.quality_issues import quality_issue
            language_codes = {
                HEADING_TRANSLATION_TASK: 'FORMAT-REQUIRED-HEADINGS',
                CAPTION_TRANSLATION_TASK: 'FORMAT-REQUIRED-CAPTIONS',
            }
            for task in (heading_translation, caption_translation):
                if task.blocking and task.task in language_codes and outcome.gate_statuses['format'] in {'passed', 'degraded', 'failed'}:
                    outcome.issues.append(quality_issue('format', language_codes[task.task],
                        task.detail, occurrence_count=task.requested_count - task.completed_count))
                    outcome.gate_statuses['format'] = 'failed'
            def rerun_gates():
                return run_validation_gates(
                    runner, output_dir, extract_result, recognize_result, render_result,
                    report_dir=report_dir,
                )

            from config import get_settings
            from llm.client import LLMClient
            # CLI 的本次授权同样不包含隐藏重发，保留共享配置原值。
            outcome, agent_state, repair_tasks = _run_initial_repair(
                runner=runner, output_dir=Path(output_dir), report_dir=report_dir,
                generation_id=generation.generation_id, project_id=project.project_id,
                template_id=args.template_id,
                source_sha256=getattr(extract_result, 'extraction_report', {}).get('metadata', {}).get('source_sha256', ''),
                rendered=render_result, outcome=outcome,
                translation_tasks=[heading_translation, caption_translation], validate=rerun_gates,
                authorization=llm_authorization, client=LLMClient(replace(get_settings(), llm_max_retries=0)),
            )
            task_reports = {item.task: item.to_dict() for item in (heading_translation, caption_translation)}
            task_reports.update({item['task']: item for item in repair_tasks})

            fidelity_report = outcome.fidelity_report
            checks = outcome.structural_checks
            compile_result = outcome.compile_result
            check_result = outcome.check_result
            publish_allowed = bool(
                outcome.snapshot().publish_allowed
                and not heading_translation.blocking
                and not caption_translation.blocking
            )
            quality_status = (
                getattr(check_result, 'quality_status', 'passed')
                if publish_allowed else 'blocked'
            )
            if 'internal_error' in outcome.gate_statuses.values():
                quality_status = 'internal_error'
            elif publish_allowed and any(item['severity'] == 'degrade' for item in outcome.issues):
                quality_status = 'degraded'
            final_dir = None
            if publish_allowed:
                final_dir = generation.publish()
                compile_result.pdf_path = str(final_dir / 'main.pdf')

            report = {
                'timestamp': timestamp,
                'project_id': project.project_id,
                'generation_id': generation.generation_id,
                'published': publish_allowed,
                'quality_status': quality_status,
                'output_dir': str(final_dir) if final_dir else None,
                'source': args.docx,
                'stats': stats,
                'llm_tasks': list(task_reports.values()),
                'agent_run': agent_state,
                'content_fidelity': fidelity_report,
                'structural_gate': checks,
                'gate_statuses': dict(outcome.gate_statuses),
                'issues': list(outcome.issues),
                'compile': _compile_report(compile_result),
                'format_check': ({
                    'quality_status': getattr(
                        check_result, 'quality_status',
                        'passed' if check_result.all_passed else 'blocked',
                    ),
                    'publish_allowed': getattr(
                        check_result, 'publish_allowed', check_result.all_passed
                    ),
                    'all_passed': check_result.all_passed,
                    'counts': getattr(check_result, 'counts', {}),
                    'blockers': getattr(check_result, 'fails', []),
                    'degradations': getattr(check_result, 'degradations', []),
                    'template': getattr(check_result, 'template_profile', {}),
                    'rules': check_result.results,
                } if check_result is not None else {'status': 'not_run'}),
            }
            from pipeline.quality_report import build_user_quality_report
            report['user_report'] = build_user_quality_report(report)
            _write_report(report_dir, report)
            if publish_allowed:
                print(f'[Published:{quality_status}] {final_dir}')
            return _quality_exit_code(publish_allowed, quality_status)
    except Exception as exc:
        formula_review = getattr(exc, "review", None)
        from pipeline.quality_issues import failure_report
        failed = failure_report('content-fidelity',
            'formula-preflight-failed' if formula_review is not None else 'generation-internal-error',
            str(exc), internal=formula_review is None)
        failed.update(timestamp=timestamp, project_id=project.project_id,
                      generation_id=generation.generation_id, error=str(exc))
        if formula_review is not None:
            failed.update(formula_preflight=formula_review, llm_tasks=[])
        _write_report(report_dir, failed)
        print(f'[Failed] {exc}')
        return 1


if __name__ == '__main__':
    sys.exit(main())
