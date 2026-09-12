# -*- coding: utf-8 -*-
"""已确认反馈在同一私有工作副本执行，完整终验后交给外层登记候选。"""

from __future__ import annotations

from pathlib import Path

from llm.policy import USER_FEEDBACK_PATCH_TASK
from pipeline.constraint_verifier import verify_paper_constraints
from pipeline.generation_service import _artifact_manifest, _package_latex_source
from template_registry.registry import resolve_builtin_template
from pipeline.workspace import GenerationWorkspace


##### 已确认目标的统一图板块 #####

def run_confirmed_feedback(*, root: Path, generation_id: str, state, evidence: list[dict],
                           template_context: dict, model, validation_factory, checkpointer,
                           journal_root: Path, parent_identity, on_state=None, resume=None, constraints=()):
    """执行已确认目标；只返回 Agent 状态，完整交付与数据库登记由外层负责。"""
    from copy import deepcopy
    from agents.quality_repair.action_journal import FileActionJournal
    from agents.quality_repair.graph_executor import build_quality_graph
    from agents.quality_repair.models import AgentState
    from agents.quality_repair.patching import WriteRegion
    from agents.quality_repair.runtime import RepairRuntime
    from agents.quality_repair.tools import QualityTools

    current = AgentState.from_dict(state.to_dict())
    inherited = deepcopy(list(constraints))
    # 继承约束由外层从可信父版本提供；模型不能移除或改写其验证条件。
    verify_paper_constraints(root, inherited)
    by_id = {item['unit_id']: item for item in evidence}
    identifiers = [goal['goal_id'] for goal in current.goals]
    if (current.mode != 'user_feedback' or not current.goals
        or len(identifiers) != len(set(identifiers)) or len(by_id) != len(evidence)
        or any(goal.get('confirmed') is not True or goal.get('kind') not in {'format', 'body_replace'}
               or not goal.get('target_unit_ids') or set(goal['target_unit_ids']) - by_id.keys()
               for goal in current.goals)):
        raise ValueError('反馈执行只能接收已有、明确确认的目标与唯一来源。')
    replacements = [goal for goal in current.goals if goal['kind'] == 'body_replace']
    if len(replacements) > 5 or len({tuple(goal['target_unit_ids']) for goal in replacements}) != len(replacements):
        raise ValueError('每批最多五项不同普通正文的完整替换。')
    for goal in replacements:
        targets = goal['target_unit_ids']
        if (len(targets) != 1 or by_id[targets[0]].get('has_semantic_objects') is not False
            or by_id[targets[0]].get('role') != 'body_paragraph'
            or goal.get('replacement', {}).get('old_text') != by_id[targets[0]].get('text')):
            raise ValueError('正文替换与可信父版本的完整普通正文不一致。')
    regions = [WriteRegion(item['relative_path'], item['unit_id'], item['role']) for item in evidence]
    journal = FileActionJournal(root, journal_root, current.run_id, generation_id=generation_id)
    tools = QualityTools(root=root, generation_id=generation_id, regions=regions, evidence=evidence,
                         template_context=template_context, journal=journal)
    validate = validation_factory(journal, deepcopy(current.goals))

    def authorized(agent):
        return (model is not None and agent.authorization.get('external_processing_allowed') is True
                and USER_FEEDBACK_PATCH_TASK in agent.authorization.get('allowed_llm_tasks', []))

    def deterministic(agent):
        # 正文替换先完成，格式模型不接收生成新正文的权限或替换参数。
        if any(goal['kind'] == 'format' and goal.get('status') != 'satisfied' for goal in agent.goals) and not authorized(agent):
            return None
        for goal in agent.goals:
            if goal['kind'] == 'body_replace' and goal.get('status') != 'satisfied':
                identifier = goal['target_unit_ids'][0]
                return {'kind': 'action', 'tool': 'replace_confirmed_text', 'arguments': {
                    'unit_id': identifier, 'goal_id': goal['goal_id'],
                    'expected_sha256': tools._source(identifier)['sha256']}}
        return None

    def context(agent):
        pending = [goal for goal in agent.goals if goal.get('status') != 'satisfied']
        selected = {identifier for goal in pending for identifier in goal['target_unit_ids']}
        return {'goals': [{key: deepcopy(goal[key]) for key in
                ('goal_id', 'kind', 'target_unit_ids', 'description', 'status', 'completed_unit_ids') if key in goal} for goal in pending],
            'evidence': [tools._source(identifier) for identifier in by_id if identifier in selected],
            'template_context': deepcopy(template_context), 'planning_round': agent.planning_round,
            'last_observation': deepcopy(agent.observations[-1]) if agent.observations else {},
            'protections': [{'unit_id': item['unit_id'], 'description': '保留此前已经完成的修改。'}
                            for item in agent.protections] + [{'unit_id': item.get('scope_ref'),
                'description': item.get('description', '保留用户已经接受的要求。')}
                for item in inherited if item.get('scope_ref') in selected or item.get('scope_type') == 'document']}

    def check_protections(agent):
        entries = {item['action_id']: item for item in journal.entries()}
        for protection in agent.protections:
            entry = entries.get(protection['action_id'])
            if (not entry or entry['status'] != 'applied' or entry['unit_id'] != protection['unit_id']
                or tools._source(protection['unit_id'])['text'] != entry['new_fragment']):
                raise ValueError('当前修改破坏了已完成目标的临时保护。')

    def observe(agent, observation):
        check_protections(agent)
        try:
            verify_paper_constraints(root, inherited)
        except RuntimeError:
            return {'safety_violation': True, 'detail': '本次动作破坏了继承的已确认约束。'}
        entry = next(item for item in journal.entries() if item['action_id'] == observation['action_id'])
        identifier, proposal = entry['unit_id'], entry['proposal']
        body = proposal['tool'] == 'replace_confirmed_text'
        candidates = [goal for goal in agent.goals if goal.get('status') != 'satisfied'
            and identifier in goal['target_unit_ids'] and
            (goal['goal_id'] == proposal['arguments'].get('goal_id') if body else goal['kind'] == 'format')]
        completed = []
        for goal in candidates:
            goal['completed_unit_ids'] = list(dict.fromkeys([*goal.get('completed_unit_ids', []), identifier]))
            if set(goal['target_unit_ids']) <= set(goal['completed_unit_ids']):
                completed.append(goal['goal_id'])
        if not any(goal.get('status') != 'satisfied' and goal['goal_id'] not in completed
                   and identifier in goal['target_unit_ids']
                   and identifier not in goal.get('completed_unit_ids', []) for goal in agent.goals):
            agent.protections.append({'unit_id': identifier, 'action_id': entry['action_id'],
                                     'goal_ids': [goal['goal_id'] for goal in candidates]})
        return {'goal_satisfied': bool(completed), 'goal_progress': bool(candidates), 'completed_goal_ids': completed,
            'verification': 'confirmed_text' if body else 'manual_visual_review',
            'detail': '已执行用户确认的正文替换。' if body else '格式补丁已通过内容保护，视觉效果仍由用户核对。'}

    def final_validate(agent):
        if parent_identity() != (agent.parent_generation_id, agent.parent_source_sha256):
            raise ValueError('终验前可信父版本已经变化。')
        check_protections(agent)
        verify_paper_constraints(root, inherited)
        return validate(agent)

    runtime = RepairRuntime(workspace_root=root, model=model, tools=tools, context=context,
        final_validate=final_validate, observe=observe, deterministic_action=deterministic,
        rollback_all=journal.rollback_all, rollback_action=lambda action: journal.rollback(action['action_id']),
        parent_identity=parent_identity, journal_root=journal_root,
        authorized=authorized)
    graph = build_quality_graph(runtime=runtime, checkpointer=checkpointer)
    config = {'configurable': {'thread_id': current.run_id}}
    if resume is not None:
        saved = graph.get_state(config)
        stored = saved.values.get('agent', {})
        immutable = {key: value for key, value in current.to_dict().items() if key != 'authorization'}
        previous = {key: value for key, value in stored.items() if key != 'authorization'}
        if current.status != 'waiting_user' or saved.next != ('wait',) or immutable != previous:
            raise ValueError('恢复状态与当前等待中的 checkpoint 不一致，不能重置轮次或目标。')
        # 具体任务的新授权由外层请求校验；状态、动作签名与停止线只继承同一 checkpoint。
        graph.update_state(config, {'agent': current.to_dict()}, as_node='prepare')
    initial = {'agent': current.to_dict()} if resume is None else resume
    try:
        for value in graph.stream(initial, config=config, stream_mode='values'):
            current = AgentState.from_dict(value['agent'])
            if on_state is not None:
                on_state(current.to_dict())
    except Exception as exc:
        try:
            journal.rollback_all()
        except Exception as cleanup:
            exc.add_note(f'反馈执行异常后的回滚失败：{type(cleanup).__name__}')
        raise
    return current


##### 统一反馈交付板块 #####


def run_confirmed_feedback_candidate(*, workspace: GenerationWorkspace, state, evidence: list[dict],
                                     snapshot: dict, structure_revision: int, model, checkpointer,
                                     parent_identity, on_state=None, resume=None, constraints=()):
    """完整终验后准备报告和源码包；此函数不登记数据库候选，也不移动接受指针。"""
    from copy import deepcopy
    from pipeline.revision_validation import build_revision_validator
    from pipeline.validation_runner import ValidationOutcome
    from pipeline.generation_service import _compile_report, _format_report, _gate_snapshot, _write_json
    from pipeline.quality_report import build_user_quality_report
    from pipeline.quality_issues import quality_issue

    workspace.manager._verify_generation_staging(workspace)
    if state.project_id != workspace.project.project_id or state.mode != 'user_feedback':
        raise ValueError('反馈运行与私有工作副本不一致。')
    root = workspace.staging_dir.resolve()
    report_dir = workspace.project.reports_dir / workspace.generation_id
    report_dir.mkdir(exist_ok=resume is not None)
    outcome = ValidationOutcome()
    report = {'schema_version': '1.0.0', 'mode': 'user_feedback', 'published': False,
              'quality_status': 'blocked', 'issues': []}
    pending_report = report_dir / '.pending-pipeline-log.json'
    inherited = deepcopy(list(constraints))

    def validation_factory(journal, goals):
        validator = build_revision_validator(root=root, project_dir=workspace.project.project_dir,
            snapshot=snapshot, source_sha256=state.parent_source_sha256,
            structure_revision=structure_revision, template_id=state.template_id,
            journal=journal, confirmed_goals=goals)
        def validate(current):
            nonlocal outcome
            outcome = validator(root)
            results = verify_paper_constraints(root, inherited)
            manual = [item for item in results if item['status'] == 'manual_review']
            if manual:
                outcome.issues.append(quality_issue('constraint', 'inherited-constraint-manual-review',
                    '部分已确认格式要求仍需结合 PDF 人工核对。', severity='degrade',
                    occurrence_count=len(manual), title='已确认要求需要人工复核',
                    action='接受候选前，请在 PDF 中核对此前确认的格式要求。'))
                if outcome.gate_statuses.get('format') == 'passed':
                    outcome.gate_statuses['format'] = 'degraded'
            return {'gate_statuses': dict(outcome.gate_statuses), 'issues': deepcopy(outcome.issues),
                    'constraint_results': results}
        return validate

    try:
        resolved = resolve_builtin_template(state.template_id)
        current = run_confirmed_feedback(root=root, generation_id=workspace.generation_id, state=state,
            evidence=evidence, template_context={'template_id': state.template_id,
                'capabilities': deepcopy(resolved.manifest.capabilities)},
            model=model, validation_factory=validation_factory, checkpointer=checkpointer,
            journal_root=workspace.project.reports_dir / 'agent-runs',
            parent_identity=parent_identity, on_state=on_state, resume=resume, constraints=inherited)
        allowed = current.status == 'ready' and outcome.snapshot().publish_allowed
        quality = ('degraded' if 'degraded' in outcome.gate_statuses.values()
                   or any(item.get('severity') == 'degrade' for item in outcome.issues) else 'passed') if allowed else 'blocked'
        if current.stop_reason in {'internal_error', 'rollback_failed'} or 'internal_error' in outcome.gate_statuses.values():
            quality = 'internal_error'
        if not allowed and not any(item.get('severity') == 'block' for item in outcome.issues):
            outcome.issues.append(quality_issue('feedback', 'feedback-not-deliverable',
                '反馈执行未完成或完整终验未通过，本批改动未交付。'))
        report.update(quality_status=quality, publish_allowed=allowed, agent_run=current.to_dict(),
            content_fidelity=outcome.fidelity_report, structural_gate=outcome.structural_checks,
            compile=_compile_report(outcome.compile_result), format_check=_format_report(outcome.check_result),
            gate_statuses=dict(outcome.gate_statuses), issues=deepcopy(outcome.issues),
            gate_snapshot=_gate_snapshot(outcome, published=False, quality_status=quality))
        report['user_report'] = build_user_quality_report(report)
        _write_json(report_dir / 'pipeline_log.json', report)
        if not allowed:
            # 等待用户时保留同一私有副本及动作日志，完整终验前不能交付。
            if current.status != 'waiting_user':
                workspace.abort()
            return {'output_dir': None, 'report_dir': str(report_dir), 'quality_status': quality,
                'artifact_manifest': _artifact_manifest(None, report_dir),
                'gate_snapshot': report['gate_snapshot'], 'agent_state': current.to_dict()}
        _package_latex_source(root)
        committed = {**report, 'published': True, 'gate_snapshot': {**report['gate_snapshot'], 'published': True}}
        committed['user_report'] = build_user_quality_report(committed)
        _write_json(root / 'reports/pipeline_log.json', committed)
        _write_json(pending_report, committed)
        manifest = _artifact_manifest(root, report_dir)
        # 私有待提交报告不能成为下载项；正式报告已随输出目录准备完成。
        manifest['artifacts'] = [item for item in manifest['artifacts'] if item['name'] != pending_report.name]
        final = workspace.publish()
        pending_report.replace(report_dir / 'pipeline_log.json')
        return {'output_dir': str(final), 'report_dir': str(report_dir), 'quality_status': quality,
            'artifact_manifest': manifest, 'gate_snapshot': committed['gate_snapshot'],
            'agent_state': current.to_dict()}
    except Exception as exc:
        try:
            pending_report.unlink(missing_ok=True)
            workspace.abort()
            report.update(published=workspace._published, quality_status='internal_error',
                          failure_stage='feedback-delivery', detail='反馈执行或交付未完成。')
            report['issues'] = [*report.get('issues', []), quality_issue('delivery', 'feedback-delivery-internal-error',
                '反馈执行或交付发生内部错误，未登记可信候选。')]
            report['gate_snapshot'] = {**report.get('gate_snapshot', {}), 'published': workspace._published,
                'quality_status': 'internal_error', 'failure_stage': 'feedback-delivery',
                'issues': deepcopy(report['issues']),
                'blocker_count': sum(item.get('severity') == 'block' for item in report['issues'])}
            report['user_report'] = build_user_quality_report(report)
            _write_json(report_dir / 'pipeline_log.json', report)
        except Exception as cleanup:
            exc.add_note(f'反馈交付异常后的清理失败：{type(cleanup).__name__}')
        raise
