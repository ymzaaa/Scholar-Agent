# -*- coding: utf-8 -*-
"""统一首次修复的诊断准入、动作审计和整批回滚。"""

import json

import pytest

from agents.quality_repair.models import AgentState
from pipeline.validation_runner import ValidationOutcome
from pipeline.quality_issues import quality_issue
from tests.unit.pipeline.test_initial_quality_repair import case, Client, execute


##### 不可修复诊断板块 #####

@pytest.mark.parametrize('stage,code', [
    ('content-fidelity', 'content-fidelity-failed'),
    ('structure', 'structure-failed'),
    ('compile', 'compile-unknown-error'),
    ('compile', 'compile-tool-missing'),
])
def test_blocking_diagnostics_never_start_repair(case, stage, code):
    arguments, path, events, validations, compiles = case
    statuses = dict.fromkeys(('content-fidelity', 'structure', 'compile', 'format'), 'not_run')
    for name in statuses:
        statuses[name] = 'failed' if name == stage else 'passed'
        if name == stage:
            break
    arguments['outcome'] = ValidationOutcome(gate_statuses=statuses,
        issues=[quality_issue(stage, code, '不可由模型修复的问题')])
    original, client = path.read_bytes(), Client()
    outcome, state, tasks = execute(arguments, client)
    assert not outcome.snapshot().publish_allowed and state is None and tasks == []
    assert client.calls == 0 and not events and not validations and not compiles
    assert path.read_bytes() == original and not arguments['report_dir'].exists()


##### 确定性动作与序列化板块 #####

def test_excess_closing_group_uses_located_syntax_evidence(case):
    arguments, _path, _events, _validations, _compiles = case
    arguments['outcome'].compile_result.raw_error = "contents/section_1.tex:2: Too many }'s."
    client = Client()
    outcome, state, _tasks = execute(arguments, client)
    assert state is not None and state['status'] == 'ready'
    assert client.calls == 1 and outcome.snapshot().publish_allowed


def test_deterministic_repair_has_serializable_state_and_exact_action_hashes(case):
    arguments, path, _events, _validations, _compiles = case
    path.write_text('% SCHOLAR_UNIT_BEGIN u-000001 body_paragraph\n'
        '\\begin{center}\nOriginal text.\n% SCHOLAR_UNIT_END u-000001\n', encoding='utf-8')
    arguments['outcome'].compile_result.raw_error = (
        r'contents/section_1.tex:3: \begin{center} on input line 2 ended by \end{document}.')
    client = Client()
    outcome, state, _tasks = execute(arguments, client, authorized=False)
    assert outcome.snapshot().publish_allowed and state['status'] == 'ready'
    assert state['planning_round'] == 0 and client.calls == 0
    restored = AgentState.from_dict(json.loads(json.dumps(state)))
    assert restored.to_dict() == state
    entries = [json.loads(item.read_text(encoding='utf-8'))
               for item in arguments['report_dir'].rglob('*.json') if item.name != 'state.json']
    actions = [entry for entry in entries if 'before_sha256' in entry]
    assert len(actions) == 1
    assert actions[0]['action_id'] == state['run_id'] + '-' + state['action_signatures'][0]
    assert len(state['action_signatures'][0]) == 64
    assert actions[0]['before_sha256'] != actions[0]['after_sha256']


##### 终验失败回滚板块 #####

def test_unsatisfied_final_validation_cannot_deliver_partial_repair(case):
    arguments, path, _events, validations, _compiles = case
    original, client = path.read_bytes(), Client()

    def blocked():
        validations.append(path.read_bytes())
        return arguments['outcome']

    arguments['validate'] = blocked
    outcome, state, _tasks = execute(arguments, client)
    assert not outcome.snapshot().publish_allowed
    assert state['status'] in {'stopped', 'failed'}
    assert 1 <= client.calls <= 3 and state['planning_round'] <= 3
    assert validations and path.read_bytes() == original
