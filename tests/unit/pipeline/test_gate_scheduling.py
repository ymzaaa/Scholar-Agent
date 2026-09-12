# -*- coding: utf-8 -*-
"""门禁状态、内部异常与修复准入的故障注入。"""

from types import SimpleNamespace
import pytest

from pipeline.validation_runner import run_validation_gates
from pipeline.checker.format_checker import FormatChecker, DEFAULT_COMMON_RULES
from pipeline.checker.rule_detectors import CheckContext, execute_detector, DETECTORS
from pipeline.checker.rule_models import RuleDefinition
from pipeline.checker.rule_models import RuleResult
from adapters.ouc import OUCTemplateAdapter
from adapters.ouc_bachelor import OUCBachelorTemplateAdapter
from pipeline.quality_report import build_user_quality_report
from pipeline.quality_issues import quality_issue
from pipeline.quality_issues import failure_report
from pipeline.generation_service import _translation_snapshot, _run_initial_repair
from pipeline.validation_runner import ValidationOutcome
from llm.policy import LLMAuthorization, LLMTaskResult, HEADING_TRANSLATION_TASK


##### 异常和顺序板块 #####


def test_unexpected_content_exception_is_internal_error_and_stops_later_gates(tmp_path, monkeypatch):
    def fail(*args):
        raise RuntimeError('故障注入')
    monkeypatch.setattr('pipeline.validation_runner.build_content_fidelity_report', fail)
    outcome = run_validation_gates(None, tmp_path, None, None, SimpleNamespace())
    assert outcome.gate_statuses == {
        'content-fidelity': 'internal_error', 'structure': 'not_run',
        'compile': 'not_run', 'format': 'not_run',
    }
    assert outcome.issues[0]['code'] == 'content-fidelity-internal-error'
    assert not outcome.snapshot().publish_allowed


##### 修复准入板块 #####


@pytest.mark.parametrize('message,missing,timed_out,repairable', [
    ('Extra }', False, False, True),
    ("Too many }'s.", False, False, True),
    ('Missing } inserted', False, False, True),
    ('Missing $ inserted', False, False, True),
    (r'\begin{center} on input line 7 ended by \end{document}', False, False, True),
    ('Unknown compiler failure', False, False, False),
    ('environment ended by document', False, False, False),
    ('Extra }', True, False, False),
    ('Extra }', False, True, False),
    ("File `missing.sty' not found. Extra }", False, False, False),
])
def test_compile_classification_uses_deterministic_gate_evidence(
    tmp_path, monkeypatch, message, missing, timed_out, repairable,
):
    """准入由真实编译结果决定，未知环境和环境故障不能交给模型猜测。"""
    monkeypatch.setattr('pipeline.validation_runner.build_content_fidelity_report',
                        lambda *args: {'all_passed': True})
    monkeypatch.setattr('pipeline.validation_runner.write_fidelity_artifacts', lambda *args: {})
    monkeypatch.setattr('pipeline.validation_runner.structural_fidelity_gate', lambda *args, **kwargs: {})
    runner = SimpleNamespace(
        template_adapter=SimpleNamespace(content_directory='contents', entrypoint='main.tex'),
        compile=lambda *args: SimpleNamespace(success=False, raw_error=message,
            process_results=[SimpleNamespace(stdout_tail='', stderr_tail='',
                missing_tool=missing, timed_out=timed_out)]),
    )
    outcome = run_validation_gates(runner, tmp_path, SimpleNamespace(word_structure={}),
        SimpleNamespace(review={}), SimpleNamespace(render_trace={}, chapter_files=[]))
    assert outcome.gate_statuses == {'content-fidelity': 'passed', 'structure': 'passed',
                                     'compile': 'failed', 'format': 'not_run'}
    assert len(outcome.issues) == 1
    assert outcome.issues[0]['repairable'] is repairable
    assert not outcome.snapshot().publish_allowed


@pytest.mark.parametrize('code,message', [
    (None, ''), ('compile-unknown-error', 'Unknown compiler failure'),
    ('compile-tool-missing', 'Extra }'),
])
def test_absent_or_unrepairable_issues_do_not_create_agent_or_checkpoint(tmp_path, monkeypatch, code, message):
    """通过或环境类阻断均由确定性外层返回，不创建执行图或恢复文件。"""
    def forbidden(*args, **kwargs):
        raise AssertionError('不得创建 Agent 状态')
    monkeypatch.setattr('agents.quality_repair.models.AgentState', forbidden)
    monkeypatch.setattr('agents.quality_repair.graph_executor.build_quality_graph', forbidden)
    outcome = ValidationOutcome(gate_statuses={
        'content-fidelity': 'passed', 'structure': 'passed',
        'compile': 'failed' if code else 'passed', 'format': 'not_run' if code else 'passed',
    }, issues=[quality_issue('compile', code, message)] if code else [],
        compile_result=SimpleNamespace(raw_error=message, process_results=[]))
    result, state, tasks = _run_initial_repair(
        runner=None, output_dir=tmp_path, report_dir=tmp_path / 'reports',
        generation_id='candidate', project_id='project', template_id='ouc-bachelor', source_sha256='source',
        rendered=None, outcome=outcome, translation_tasks=[], validate=forbidden,
        authorization=LLMAuthorization(), client=None, on_agent_state=forbidden)
    assert result is outcome and state is None and tasks == []
    assert result.snapshot().publish_allowed is (code is None)
    assert list(tmp_path.iterdir()) == []


def test_language_task_keeps_actual_gate_severity():
    outcome = ValidationOutcome(gate_statuses={
        'content-fidelity': 'passed', 'structure': 'passed',
        'compile': 'passed', 'format': 'degraded',
    }, issues=[quality_issue('format', 'OUC-TOC-ENGLISH-HEADINGS', '缺少英文', severity='degrade')])
    authorization = LLMAuthorization.for_tasks([HEADING_TRANSLATION_TASK],
        external_processing_allowed=True, source='test')
    task = LLMTaskResult(task=HEADING_TRANSLATION_TASK, status='not_authorized', requested_count=1)
    snapshot = _translation_snapshot(outcome, [task], authorization)
    assert snapshot.publish_allowed and snapshot.blockers == 0


##### 动态格式检查板块 #####


def test_runtime_format_rules_exclude_static_template_and_todo_checks(tmp_path):
    checker = FormatChecker(DEFAULT_COMMON_RULES, tmp_path, OUCTemplateAdapter().format_rule_profile())
    ids = {rule.rule_id for rule in checker.rules}
    assert 'COMMON-TODO' not in ids
    assert 'COMMON-REF-UNRESOLVED' not in ids
    assert all(rule.detector and rule.detector != 'require_regex' for rule in checker.rules)


def test_bachelor_dynamic_checks_cover_its_metadata_and_pdf(tmp_path):
    (tmp_path / 'main.tex').write_text(r'\author{【待填写】}', encoding='utf-8')
    checker = FormatChecker(DEFAULT_COMMON_RULES, tmp_path, OUCBachelorTemplateAdapter().format_rule_profile())
    results = checker.run_all()
    assert results['COMMON-COVER-METADATA-PENDING']['status'] == 'fail'
    assert any(rule.detector == 'pdf_page_size' for rule in checker.rules)


def test_detector_failure_has_internal_error_status(tmp_path, monkeypatch):
    def fail(*args):
        raise RuntimeError('检测器故障')
    monkeypatch.setitem(DETECTORS, 'log_pattern', fail)
    rule = RuleDefinition('test', '日志', '日志', 'common', 'compile', 'block', 'log_pattern')
    assert execute_detector(CheckContext(tmp_path), rule).status == 'internal_error'


def test_warning_count_is_complete_after_evidence_limit(tmp_path):
    (tmp_path / 'main.log').write_text('Warning new font\n' * 27, encoding='utf-8')
    rule = RuleDefinition('font', '日志', '字体', 'common', 'compile', 'block',
                          'log_pattern', config={'pattern': 'Warning new font'})
    outcome = execute_detector(CheckContext(tmp_path), rule)
    result = RuleResult(rule, outcome.status, outcome.detail, outcome.evidence,
                        occurrence_count=outcome.occurrence_count).to_dict()
    assert result['occurrence_count'] == 27
    assert len(result['evidence']) <= 20


##### 用户报告板块 #####


def test_report_translates_all_gates_and_uses_numeric_counts():
    report = {'published': False, 'quality_status': 'internal_error', 'issues': [
        quality_issue(stage, stage + '-failure', '文字中故意写发现 999 个', occurrence_count=2)
        for stage in ('content-fidelity', 'structure', 'compile', 'format')
    ]}
    result = build_user_quality_report(report)
    assert len(result['findings']) == 4
    assert all(item['occurrence_count'] == 2 for item in result['findings'])
    assert result['delivery']['title'] == '当前版本未达到交付条件'


def test_early_failure_report_preserves_stage_and_internal_error():
    report = failure_report('content-fidelity', 'render-internal-error', '渲染异常', internal=True)
    assert report['gate_statuses']['content-fidelity'] == 'internal_error'
    assert report['gate_statuses']['compile'] == 'not_run'
    assert report['gate_snapshot']['blocker_count'] == 1
    assert report['user_report']['delivery']['blocking_count'] == 1
