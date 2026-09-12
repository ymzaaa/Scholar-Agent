# -*- coding: utf-8 -*-
"""首次修复复用统一图，无问题不创建 Agent，允许动作仍须完整终验。"""

import json
from types import SimpleNamespace

import pytest

from llm.policy import (LLMAuthorization, INITIAL_GENERATION_REPAIR_TASK,
                        HEADING_TRANSLATION_TASK, LLMTaskResult)
from adapters.ouc import OUCTemplateAdapter
from models.render_trace import RenderRecord
from pipeline import generation_service
from pipeline.quality_issues import quality_issue
from pipeline.validation_runner import ValidationOutcome


##### 已定位语法问题板块 #####

@pytest.fixture()
def case(tmp_path):
    root = tmp_path / 'staging'
    (root / 'contents').mkdir(parents=True)
    (root / '.scholar-generation.json').write_text(json.dumps({'generation_id': 'candidate', 'status': 'staging'}))
    path = root / 'contents/section_1.tex'
    path.write_text('% SCHOLAR_UNIT_BEGIN u-000001 body_paragraph\n'
                    '\\textbf{Original text.\n% SCHOLAR_UNIT_END u-000001\n', encoding='utf-8')
    record = RenderRecord(record_id='r-000000', marker_unit_id='u-000001', source_unit_ids=['u-000001'],
        source_order=0, role='body_paragraph', status='rendered', target_file='contents/section_1.tex', output_order=0)
    rendered = SimpleNamespace(render_trace={'records': [record.to_dict()]},
                               chapter_files=['contents/section_1.tex'])
    outcome = ValidationOutcome(gate_statuses={'content-fidelity': 'passed', 'structure': 'passed',
        'compile': 'failed', 'format': 'not_run'}, compile_result=SimpleNamespace(
            raw_error='contents/section_1.tex:2: Missing } inserted.', process_results=[]),
        issues=[quality_issue('compile', 'latex-compile-failed', 'Missing } inserted.', repairable=True)])
    events, validations, compiles = [], [], []

    def compile_project(*args):
        compiles.append(True)
        return SimpleNamespace(success=True)

    def validate():
        validations.append(path.read_bytes())
        return ValidationOutcome(gate_statuses=dict.fromkeys(('content-fidelity', 'structure', 'compile', 'format'), 'passed'))

    arguments = dict(runner=SimpleNamespace(compile=compile_project, template_adapter=None),
        output_dir=root, report_dir=tmp_path / 'reports', generation_id='candidate', project_id='project',
        template_id='ouc-bachelor', source_sha256='source', rendered=rendered, outcome=outcome,
        translation_tasks=[], validate=validate, on_agent_state=lambda state: events.append(state))
    return arguments, path, events, validations, compiles


class Client:
    configured = True

    def __init__(self, *, violate=False):
        self.calls = 0
        self.violate = violate

    def complete_with_tools(self, system, user, tools, **kwargs):
        self.calls += 1
        evidence = json.loads(user)['evidence'][0]
        return {'tool_calls': [{'name': 'apply_protected_patch', 'arguments': {
            'unit_id': evidence['unit_id'], 'expected_sha256': evidence['sha256'],
            'edits': [{'old_text': r'\textbf{Original text.',
                       'new_text': r'\textbf{Original text.}' + (' Added fact.' if self.violate else '')}]}}]}


def execute(arguments, client, *, authorized=True):
    authorization = LLMAuthorization.for_tasks([INITIAL_GENERATION_REPAIR_TASK],
        external_processing_allowed=authorized, source='test')
    return generation_service._run_initial_repair(**arguments, client=client, authorization=authorization)


##### 启动、终验和回滚板块 #####

def test_no_issue_does_not_create_state_journal_or_checkpoint(case):
    arguments, _path, events, validations, _compiles = case
    arguments['outcome'] = ValidationOutcome(gate_statuses=dict.fromkeys(
        ('content-fidelity', 'structure', 'compile', 'format'), 'passed'))
    client = Client()
    outcome, state, tasks = execute(arguments, client)
    assert outcome is arguments['outcome'] and state is None and tasks == []
    assert client.calls == 0 and events == [] and validations == []
    assert not arguments['report_dir'].exists()


def test_initial_repair_records_start_then_uses_one_action_and_full_validation(case):
    arguments, path, events, validations, compiles = case
    client = Client()
    outcome, state, _tasks = execute(arguments, client)
    assert events[0]['status'] == 'running' and events[0]['planning_round'] == 0
    assert state['status'] == 'ready' and state['planning_round'] == 1
    assert client.calls == 1 and len(compiles) == 1 and len(validations) == 1
    assert outcome.snapshot().publish_allowed
    assert r'\textbf{Original text.}' in path.read_text(encoding='utf-8')
    assert arguments['rendered'].render_trace['approved_changes'][0]['kind'] == 'format'


def test_unapproved_initial_repair_does_not_start_agent(case):
    arguments, _path, events, validations, compiles = case
    client = Client()
    outcome, state, _tasks = execute(arguments, client, authorized=False)
    assert state is None and not outcome.snapshot().publish_allowed
    assert client.calls == 0 and not events and not validations and not compiles


def test_known_unclosed_center_is_repaired_without_external_authorization(case):
    arguments, path, events, validations, compiles = case
    path.write_text('% SCHOLAR_UNIT_BEGIN u-000001 body_paragraph\n'
        '\\begin{center}\nOriginal text.\n% SCHOLAR_UNIT_END u-000001\n', encoding='utf-8')
    arguments['outcome'].compile_result.raw_error = (
        r'contents/section_1.tex:3: \begin{center} on input line 2 ended by \end{document}.')
    client = Client()
    client.configured = False
    outcome, state, _tasks = execute(arguments, client, authorized=False)
    assert state['status'] == 'ready' and state['planning_round'] == 0 and client.calls == 0
    assert events and len(compiles) == 1 and len(validations) == 1
    assert outcome.snapshot().publish_allowed and r'\end{center}' in path.read_text(encoding='utf-8')
    assert state['authorization']['external_processing_allowed'] is False


def test_ambiguous_nested_environment_does_not_receive_deterministic_patch(case):
    arguments, path, events, _validations, _compiles = case
    path.write_text('% SCHOLAR_UNIT_BEGIN u-000001 body_paragraph\n'
        '\\begin{center}\n\\begin{figure}\nOriginal text.\n% SCHOLAR_UNIT_END u-000001\n', encoding='utf-8')
    arguments['outcome'].compile_result.raw_error = (
        r'contents/section_1.tex:4: \begin{center} on input line 2 ended by \end{document}.')
    original = path.read_bytes()
    outcome, state, _tasks = execute(arguments, Client(), authorized=False)
    assert state is None and not events and not outcome.snapshot().publish_allowed
    assert path.read_bytes() == original


def test_content_change_is_rejected_and_original_bytes_survive(case):
    arguments, path, events, validations, _compiles = case
    original = path.read_bytes()
    outcome, state, _tasks = execute(arguments, Client(violate=True))
    assert state['status'] == 'failed' and state['stop_reason'] == 'safety_violation'
    assert path.read_bytes() == original and not validations and not outcome.snapshot().publish_allowed


def test_final_validation_exception_rolls_back_the_action(case):
    arguments, path, _events, _validations, _compiles = case
    original = path.read_bytes()

    def fail():
        raise RuntimeError('injected validator failure')

    arguments['validate'] = fail
    outcome, state, _tasks = execute(arguments, Client())
    assert state['status'] == 'failed' and state['stop_reason'] == 'internal_error'
    assert path.read_bytes() == original and not outcome.snapshot().publish_allowed
    from pipeline.quality_report import build_user_quality_report
    public = build_user_quality_report({'issues': outcome.issues, 'agent_run': state, 'published': False})
    assert 'RuntimeError' not in json.dumps(public) and 'private' not in json.dumps(public)


def test_new_compiler_environment_failure_stops_without_another_model_call(case):
    arguments, path, _events, _validations, _compiles = case
    original = path.read_bytes()
    arguments['runner'].compile = lambda *args: SimpleNamespace(success=False,
        raw_error='contents/section_1.tex:2: Missing } inserted.', process_results=[SimpleNamespace(
            missing_tool=True, timed_out=False, stdout_tail='', stderr_tail='')])
    client = Client()
    outcome, state, _tasks = execute(arguments, client)
    assert state['status'] == 'stopped' and state['stop_reason'] == 'unrepairable_compile'
    assert client.calls == 1 and path.read_bytes() == original and not outcome.snapshot().publish_allowed


@pytest.mark.parametrize('response', ['ID=0: English heading', '', 'ID=0: English \\input{private}'])
def test_required_language_uses_auxiliary_translation_and_same_final_graph(case, response):
    arguments, path, events, validations, compiles = case
    path.write_text(path.read_text(encoding='utf-8')
        + '% SCHOLAR_TEMPLATE_HEADING_EN heading chapter\n\\enchapter{中文标题}\n', encoding='utf-8')
    arguments['runner'].template_adapter = OUCTemplateAdapter()
    arguments['rendered'].render_trace['headings'] = [{'unit_id': 'heading', 'text_zh': '中文标题',
        'level': 'chapter', 'target_file': 'contents/section_1.tex'}]
    arguments['outcome'] = ValidationOutcome(gate_statuses={
        'content-fidelity': 'passed', 'structure': 'passed', 'compile': 'passed', 'format': 'degraded'})
    arguments['translation_tasks'] = [LLMTaskResult.disabled(HEADING_TRANSLATION_TASK, 1)]

    class Translation:
        configured = True
        calls = 0

        def complete_text(self, *args, **kwargs):
            self.calls += 1
            return response

        def complete_with_tools(self, *args, **kwargs):
            raise AssertionError('完成语言任务后不得额外询问模型')

    client = Translation()
    original = path.read_bytes()
    outcome, state, tasks = generation_service._run_initial_repair(**arguments, client=client,
        authorization=LLMAuthorization.for_tasks([HEADING_TRANSLATION_TASK],
            external_processing_allowed=True, source='test'))
    if response != 'ID=0: English heading':
        assert state['status'] == 'failed' and not outcome.snapshot().publish_allowed
        assert client.calls == 1 and not validations and path.read_bytes() == original
        assert tasks[0]['task'] == HEADING_TRANSLATION_TASK
        assert tasks[0]['status'] == 'failed' and tasks[0]['external_call_attempted'] is True
        assert tasks[0]['completed_count'] == 0 and tasks[0]['input_hashes']
        return
    assert state['status'] == 'ready' and state['planning_round'] == 0
    assert client.calls == 1 and len(validations) == 1 and not compiles
    assert events[0]['status'] == 'running' and outcome.snapshot().publish_allowed
    assert tasks[0]['completed_count'] == 1 and 'English heading' in path.read_text(encoding='utf-8')


def test_failed_language_attempt_is_not_implicitly_retried(case):
    arguments, _path, events, validations, _compiles = case
    arguments['outcome'] = ValidationOutcome(gate_statuses={
        'content-fidelity': 'passed', 'structure': 'passed', 'compile': 'passed', 'format': 'degraded'})
    arguments['translation_tasks'] = [LLMTaskResult(task=HEADING_TRANSLATION_TASK,
        status='failed', requested_count=1, external_call_attempted=True)]
    client = Client()
    outcome, state, tasks = generation_service._run_initial_repair(**arguments, client=client,
        authorization=LLMAuthorization.for_tasks([HEADING_TRANSLATION_TASK],
            external_processing_allowed=True, source='test'))
    assert state is None and tasks == [] and not events and not validations and client.calls == 0
    assert outcome is arguments['outcome']
