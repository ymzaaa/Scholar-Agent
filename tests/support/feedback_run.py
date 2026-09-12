# -*- coding: utf-8 -*-
"""受控 LaTeX 夹具通过正式统一图运行；此辅助不伪造 PDF 或登记可信版本。"""

import json
import re
from copy import deepcopy

from langgraph.checkpoint.memory import InMemorySaver

from agents.quality_repair.models import AgentState
from llm.policy import USER_FEEDBACK_PATCH_TASK
from pipeline.revision_batch import run_confirmed_feedback
from template_registry.registry import resolve_builtin_template


##### 离线模型与明确目标板块 #####


class FixturePlanningModel:
    """只返回测试提供的通用补丁，读取当前工具证据的哈希；没有网络调用。"""

    def __init__(self, edits):
        self.edits = edits
        self.calls = 0

    def invoke_tools(self, **request):
        context = json.loads(request['user'])
        identifier, edits = self.edits[self.calls]
        self.calls += 1
        evidence = next(item for item in context['evidence'] if item['unit_id'] == identifier)
        return {'tool_calls': [{'name': 'apply_protected_patch', 'arguments': {
            'unit_id': identifier, 'expected_sha256': evidence['sha256'], 'edits': deepcopy(edits)}}]}


def run_fixture_feedback(root, *, template_id, sources, edits, constraints=(), gate_result=None):
    """终验默认是显式离线注入；真实编译验收使用独立的正式交付测试。"""
    (root / '.scholar-generation.json').write_text(json.dumps({
        'generation_id': 'candidate', 'status': 'staging'}), encoding='utf-8')
    evidence, goals = [], []
    for relative, source in sources.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding='utf-8', newline='')
        for unit in re.finditer(r'% SCHOLAR_UNIT_BEGIN (\S+) (\S+)\n(.*?)% SCHOLAR_UNIT_END \1', source, re.S):
            identifier, role, body = unit.groups()
            evidence.append({'unit_id': identifier, 'role': role, 'relative_path': relative,
                             'text': body.strip(), 'has_semantic_objects': role != 'body_paragraph'})
    for identifier, _ in edits:
        if any(item['goal_id'] == 'goal-' + identifier for item in goals):
            continue
        goals.append({'goal_id': 'goal-' + identifier, 'kind': 'format', 'status': 'pending',
            'confirmed': True, 'target_unit_ids': [identifier], 'description': '执行本案例已确认的排版修改。'})
    state = AgentState(run_id='fixture-run', project_id='fixture-project', mode='user_feedback',
        template_id=template_id, goals=goals, authorization={'external_processing_allowed': True,
            'allowed_llm_tasks': [USER_FEEDBACK_PATCH_TASK]})
    model = FixturePlanningModel(edits)
    validations = []

    def factory(journal, confirmed_goals):
        def validate(current):
            validations.append(deepcopy(current.goals))
            return deepcopy(gate_result) if gate_result is not None else {
                'gate_statuses': dict.fromkeys(('content-fidelity', 'structure', 'compile', 'format'), 'passed'),
                'issues': []}
        return validate

    result = run_confirmed_feedback(root=root, generation_id='candidate', state=state, evidence=evidence,
        template_context={'template_id': template_id,
            'capabilities': resolve_builtin_template(template_id).manifest.capabilities},
        model=model, validation_factory=factory, checkpointer=InMemorySaver(),
        journal_root=root / 'journals', parent_identity=lambda: (None, ''), constraints=constraints)
    return result, model, validations
