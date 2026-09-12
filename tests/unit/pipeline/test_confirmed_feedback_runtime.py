# -*- coding: utf-8 -*-
"""反馈目标使用统一执行图，正文替换、格式修改和终验在一个私有工作副本中完成。"""

from copy import deepcopy
import hashlib
import json

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from agents.quality_repair.models import AgentState
from pipeline.revision_batch import run_confirmed_feedback
from template_registry.registry import resolve_builtin_template


##### 私有候选与假客户端板块 #####

@pytest.fixture()
def case(tmp_path):
    root = tmp_path / 'staging'
    root.mkdir()
    (root / '.scholar-generation.json').write_text(json.dumps({'status': 'staging', 'generation_id': 'candidate'}))
    path = root / 'chapter.tex'
    path.write_bytes(b'% SCHOLAR_UNIT_BEGIN body body_paragraph\r\nOriginal text.\r\n% SCHOLAR_UNIT_END body\r\n')
    evidence = [{'unit_id': 'body', 'role': 'body_paragraph', 'relative_path': 'chapter.tex',
                 'text': 'Original text.', 'has_semantic_objects': False}]
    state = AgentState(run_id='run', project_id='project', mode='user_feedback', template_id='ouc-bachelor',
        parent_generation_id='parent', parent_source_sha256='source',
        authorization={'external_processing_allowed': True, 'allowed_llm_tasks': ['user_feedback_patch_generation']})
    state.goals = [{'goal_id': 'format', 'kind': 'format', 'confirmed': True, 'target_unit_ids': ['body'],
                    'description': 'Set paragraph spacing to 6pt.', 'status': 'pending'}]
    return root, path, evidence, state


class Model:
    def __init__(self, path, *, source='Original text.', extra='', read_only=False):
        self.path, self.source, self.extra, self.read_only = path, source, extra, read_only
        self.calls = 0

    def invoke_tools(self, *, system, user, tools):
        self.calls += 1
        assert 'compile' not in {item['name'] for item in tools}
        assert str(self.path) not in user
        if self.read_only:
            return {'tool_calls': [{'name': 'read_source', 'arguments': {'unit_id': 'body', 'offset': self.calls - 1}}]}
        assert self.source in self.path.read_text(encoding='utf-8')
        return {'tool_calls': [{'name': 'apply_protected_patch', 'arguments': {
            'unit_id': 'body', 'expected_sha256': hashlib.sha256(self.path.read_bytes()).hexdigest(),
            'edits': [{'old_text': self.source, 'new_text': r'\setlength{\parskip}{6pt} ' + self.source + self.extra}]}}]}


def execute(case, *, model=None, final=None, parent_identity=None, constraints=()):
    root, path, evidence, state = case
    validations = []
    def factory(journal, goals):
        assert journal.entries() == []
        assert goals == state.goals
        def validate(current):
            validations.append(deepcopy(current.to_dict()))
            assert all(goal['status'] == 'satisfied' for goal in current.goals)
            return final or {'gate_statuses': {'content-fidelity': 'passed', 'structure': 'passed',
                                               'compile': 'passed', 'format': 'passed'}, 'issues': []}
        return validate
    result = run_confirmed_feedback(root=root, generation_id='candidate', state=state,
        evidence=evidence, template_context={'template_id': state.template_id,
            'capabilities': resolve_builtin_template(state.template_id).manifest.capabilities},
        model=model, validation_factory=factory, checkpointer=InMemorySaver(),
        journal_root=root.parent / 'journal', parent_identity=parent_identity or (lambda: ('parent', 'source')),
        constraints=constraints)
    return result, validations


##### 确定性正文和单动作规划板块 #####

def test_confirmed_format_action_goes_directly_to_full_validation(case):
    model = Model(case[1])
    result, validations = execute(case, model=model)
    assert result.status == 'ready' and result.planning_round == 1
    assert model.calls == 1 and len(validations) == 1
    assert validations[0]['status'] == 'validating'
    assert result.observations[0]['evaluation']['verification'] == 'manual_visual_review'
    assert result.protections[0]['action_id'] == result.observations[0]['action_id']
    assert 'fragment' not in result.protections[0]
    assert not (case[0] / 'latex-source.zip').exists()


def test_confirmed_body_replacement_runs_before_model_format_action(case):
    state = case[3]
    state.goals.append({'goal_id': 'body-change', 'kind': 'body_replace', 'confirmed': True,
        'target_unit_ids': ['body'], 'replacement': {'old_text': 'Original text.', 'new_text': 'Confirmed new text.'},
        'status': 'pending'})
    original = deepcopy(state.to_dict())
    model = Model(case[1], source='Confirmed new text.')
    result, validations = execute(case, model=model)
    assert result.status == 'ready' and model.calls == 1 and len(validations) == 1
    assert [item['tool'] for item in result.observations] == ['replace_confirmed_text', 'apply_protected_patch']
    assert r'\setlength{\parskip}{6pt} Confirmed new text.' in case[1].read_text(encoding='utf-8')
    assert state.to_dict() == original


def test_body_only_needs_no_external_authorization_or_planning_call(case):
    case[3].goals = [{'goal_id': 'body-change', 'kind': 'body_replace', 'confirmed': True,
        'target_unit_ids': ['body'], 'replacement': {'old_text': 'Original text.', 'new_text': 'Confirmed new text.'},
        'status': 'pending'}]
    case[3].authorization = {}
    result, validations = execute(case)
    assert result.status == 'ready' and result.planning_round == 0 and len(validations) == 1


##### 整批回滚与暂停板块 #####


def test_pause_after_body_replacement_resumes_without_repeating_the_action(case):
    """暂停保留私有动作与目标进度，恢复不能再次替换已经确认的原文。"""
    root, path, evidence, state = case
    state.goals.append({'goal_id': 'body-change', 'kind': 'body_replace', 'confirmed': True,
        'target_unit_ids': ['body'], 'replacement': {'old_text': 'Original text.', 'new_text': 'Confirmed new text.'},
        'status': 'pending'})
    model = Model(path, source='Confirmed new text.')
    invoke = model.invoke_tools
    def ask_first(**kwargs):
        model.invoke_tools = invoke
        return {'status': 'ask_user', 'detail': '请确认是否继续处理已确认格式目标。'}
    model.invoke_tools = ask_first
    validations = []
    def factory(journal, goals):
        def validate(current):
            validations.append(current.to_dict())
            return {'gate_statuses': dict.fromkeys(('content-fidelity', 'structure', 'compile', 'format'), 'passed')}
        return validate
    arguments = dict(root=root, generation_id='candidate', evidence=evidence,
        template_context={'template_id': state.template_id}, model=model,
        checkpointer=InMemorySaver(), journal_root=root.parent / 'journal',
        parent_identity=lambda: ('parent', 'source'), validation_factory=factory)
    paused = run_confirmed_feedback(state=state, **arguments)
    assert paused.status == 'waiting_user' and paused.planning_round == 1
    assert 'Confirmed new text.' in path.read_text(encoding='utf-8')
    assert not validations and not (root / 'latex-source.zip').exists()
    result = run_confirmed_feedback(state=paused, resume=Command(resume={'continue': True}), **arguments)
    assert result.status == 'ready' and result.planning_round == 2
    assert [item['tool'] for item in result.observations] == ['replace_confirmed_text', 'apply_protected_patch']
    assert len(result.action_signatures) == 2 and len(validations) == 1


@pytest.mark.parametrize('remove_protection', [False, True])
def test_inherited_constraint_is_checked_after_generic_patch(case, remove_protection):
    path = case[1]
    path.write_bytes(path.read_bytes().replace(b'Original text.', b'\\noindent Original text.'))
    original = path.read_bytes()
    constraints = [{'constraint_id': 'accepted-indent', 'scope_ref': 'body',
        'description': '保留已接受的不缩进设置', 'verifier': {'mode': 'deterministic',
            'checker': 'tex_unit_contains', 'relative_path': 'chapter.tex', 'needle': r'\noindent'}}]
    before = deepcopy(constraints)
    class Patch(Model):
        def invoke_tools(self, **kwargs):
            response = super().invoke_tools(**kwargs)
            if remove_protection:
                response['tool_calls'][0]['arguments']['edits'] = [
                    {'old_text': r'\noindent Original text.', 'new_text': r'\setlength{\parskip}{6pt} Original text.'}]
            return response
    result, validations = execute(case, model=Patch(path), constraints=constraints)
    assert constraints == before
    if remove_protection:
        assert result.status == 'failed' and result.stop_reason == 'protected_content_changed'
        assert path.read_bytes() == original and not validations
    else:
        assert result.status == 'ready' and len(validations) == 1
        assert r'\noindent' in path.read_text(encoding='utf-8')

@pytest.mark.parametrize('fault', [None, 'round', 'parent'])
def test_resume_reuses_checkpoint_and_only_accepts_new_authorization(case, fault):
    root, path, evidence, state = case
    state.authorization = {}
    saver, model = InMemorySaver(), Model(path)
    parent = ['parent', 'source']
    arguments = dict(root=root, generation_id='candidate', evidence=evidence,
        template_context={'template_id': state.template_id}, model=model, checkpointer=saver,
        journal_root=root.parent / 'journal', parent_identity=lambda: tuple(parent),
        validation_factory=lambda journal, goals: lambda agent: {
            'gate_statuses': dict.fromkeys(('content-fidelity', 'structure', 'compile', 'format'), 'passed'),
            'issues': []})
    original = path.read_bytes()
    paused = run_confirmed_feedback(state=state, **arguments)
    assert paused.status == 'waiting_user' and paused.planning_round == 0
    assert path.read_bytes() == original and model.calls == 0
    paused.authorization = {'external_processing_allowed': True,
                            'allowed_llm_tasks': ['user_feedback_patch_generation']}
    if fault == 'round':
        paused.planning_round = 1
        with pytest.raises(ValueError, match='恢复状态'):
            run_confirmed_feedback(state=paused, resume=Command(resume={'continue': True}), **arguments)
        assert path.read_bytes() == original and model.calls == 0
        return
    if fault == 'parent':
        parent[0] = 'different-parent'
    result = run_confirmed_feedback(state=paused, resume=Command(resume={'continue': True}), **arguments)
    if fault:
        assert result.status == 'stopped' and path.read_bytes() == original and model.calls == 0
    else:
        assert result.status == 'ready' and result.planning_round == 1 and model.calls == 1
        assert len(result.action_signatures) == 1


@pytest.mark.parametrize('fault', ['content', 'gate', 'parent', 'three_rounds'])
def test_failed_feedback_keeps_original_bytes_and_never_delivers(case, fault):
    original = case[1].read_bytes()
    model = Model(case[1], extra='Unconfirmed facts.' if fault == 'content' else '', read_only=fault == 'three_rounds')
    final = {'gate_statuses': {'content-fidelity': 'passed', 'structure': 'passed',
                              'compile': 'failed', 'format': 'not_run'}, 'issues': []} if fault == 'gate' else None
    result, _ = execute(case, model=model, final=final,
                         parent_identity=(lambda: ('other', 'source')) if fault == 'parent' else None)
    assert result.status in {'stopped', 'failed'}
    assert case[1].read_bytes() == original and not (case[0] / 'latex-source.zip').exists()
    if fault == 'three_rounds':
        assert model.calls == result.planning_round == 3


def test_missing_specific_authorization_pauses_without_writing(case):
    case[3].authorization = {'external_processing_allowed': True, 'allowed_llm_tasks': ['feedback_normalization']}
    original = case[1].read_bytes()
    model = Model(case[1])
    result, validations = execute(case, model=model)
    assert result.status == 'waiting_user' and result.stop_reason == 'authorization_required'
    assert model.calls == 0 and not validations and case[1].read_bytes() == original


def test_multi_unit_goal_keeps_each_proved_action_until_all_targets_are_processed(case):
    path = case[1]
    path.write_bytes(path.read_bytes() + b'% SCHOLAR_UNIT_BEGIN second body_paragraph\r\nSecond text.\r\n% SCHOLAR_UNIT_END second\r\n')
    case[2].append({'unit_id': 'second', 'role': 'body_paragraph', 'relative_path': 'chapter.tex',
                    'text': 'Second text.', 'has_semantic_objects': False})
    case[3].goals[0]['target_unit_ids'].append('second')
    class TwoTargets(Model):
        def invoke_tools(self, **kwargs):
            if self.calls == 0:
                return super().invoke_tools(**kwargs)
            assert r'\setlength{\parskip}{6pt} Original text.' in path.read_text(encoding='utf-8')
            self.source = 'Second text.'
            response = super().invoke_tools(**kwargs)
            response['tool_calls'][0]['arguments']['unit_id'] = 'second'
            return response
    model = TwoTargets(path)
    result, validations = execute(case, model=model)
    assert result.status == 'ready' and model.calls == 2 and len(validations) == 1
    assert path.read_text(encoding='utf-8').count(r'\setlength{\parskip}{6pt}') == 2
    assert result.goals[0]['completed_unit_ids'] == ['body', 'second']


def test_repairable_final_failure_does_not_repeat_confirmed_body_replacement(case):
    state = case[3]
    state.goals.append({'goal_id': 'body-change', 'kind': 'body_replace', 'confirmed': True,
        'target_unit_ids': ['body'], 'replacement': {'old_text': 'Original text.', 'new_text': 'Confirmed new text.'},
        'status': 'pending'})
    checks = []
    class Repair(Model):
        def invoke_tools(self, **kwargs):
            response = super().invoke_tools(**kwargs)
            if self.calls == 2:
                response['tool_calls'][0]['arguments']['edits'] = [
                    {'old_text': 'Confirmed new text.', 'new_text': r'\noindent Confirmed new text.'}]
            return response
    model = Repair(case[1], source='Confirmed new text.')
    def factory(journal, goals):
        def validate(current):
            checks.append(current.to_dict())
            return {'gate_statuses': {'content-fidelity': 'passed', 'structure': 'passed',
                'compile': 'failed' if len(checks) == 1 else 'passed',
                'format': 'not_run' if len(checks) == 1 else 'passed'},
                'issues': [{'severity': 'block', 'repairable': True}] if len(checks) == 1 else []}
        return validate
    result = run_confirmed_feedback(root=case[0], generation_id='candidate', state=state,
        evidence=case[2], template_context={}, model=model, validation_factory=factory,
        checkpointer=InMemorySaver(), journal_root=case[0].parent / 'journal',
        parent_identity=lambda: ('parent', 'source'))
    assert result.status == 'ready' and model.calls == 2 and len(checks) == 2
    assert [item['tool'] for item in result.observations].count('replace_confirmed_text') == 1


@pytest.mark.parametrize('role,old,new', [
    ('figure', r'\begin{figure}\includegraphics[width=0.8\linewidth]{image.png}\caption{Caption}\end{figure}',
     r'\begin{figure}\includegraphics[width=0.6\linewidth]{image.png}\caption{Caption}\end{figure}'),
    ('table', r'\begin{table}\caption{Caption}\begin{tabular}{lc}A&B\\C&D\\\end{tabular}\end{table}',
     r'\begin{longtable}{lc}\caption{Caption}\\A&B\\C&D\\\end{longtable}'),
])
def test_same_generic_patch_handles_figure_and_cross_page_table(case, role, old, new):
    if role == 'table':
        case[3].template_id = 'ouc-graduate'
    case[1].write_text(f'% SCHOLAR_UNIT_BEGIN body {role}\n{old}\n% SCHOLAR_UNIT_END body\n', encoding='utf-8')
    case[2][0].update(role=role, text='Caption', has_semantic_objects=True)
    class ObjectPatch(Model):
        def invoke_tools(self, **kwargs):
            self.calls += 1
            return {'tool_calls': [{'name': 'apply_protected_patch', 'arguments': {
                'unit_id': 'body', 'expected_sha256': hashlib.sha256(self.path.read_bytes()).hexdigest(),
                'edits': [{'old_text': old, 'new_text': new}]}}]}
    model = ObjectPatch(case[1])
    result, validations = execute(case, model=model)
    assert result.status == 'ready' and model.calls == 1 and len(validations) == 1
    assert new in case[1].read_text(encoding='utf-8')
    assert result.observations[0]['tool'] == 'apply_protected_patch'


def test_missing_format_authorization_pauses_before_any_body_write(case):
    case[3].authorization = {}
    case[3].goals.append({'goal_id': 'body-change', 'kind': 'body_replace', 'confirmed': True,
        'target_unit_ids': ['body'], 'replacement': {'old_text': 'Original text.', 'new_text': 'Confirmed new text.'},
        'status': 'pending'})
    original = case[1].read_bytes()
    result, validations = execute(case, model=Model(case[1]))
    assert result.status == 'waiting_user' and not result.action_signatures and not result.observations
    assert not validations and case[1].read_bytes() == original
