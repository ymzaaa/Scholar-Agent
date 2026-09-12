# -*- coding: utf-8 -*-
"""版本事务测试使用的最小登记产物；不冒充真实 PDF 编译。"""

import hashlib
import json
from pathlib import Path


##### 登记夹具板块 #####


def delivery_values(store, generation_id, *, project_id=None):
    """为已创建 generation 写入具备大小和哈希的最小测试产物。"""
    generation = store.get_generation(generation_id)
    project = store.get(project_id or generation.project_id)
    output = Path(project.workspace_dir) / 'generations' / generation_id
    output.mkdir(parents=True, exist_ok=True)
    report_dir = Path(project.workspace_dir) / 'reports' / generation_id
    report_dir.mkdir(parents=True, exist_ok=True)
    (output / '.scholar-generation.json').write_text(json.dumps({
        'project_id': project.project_id, 'generation_id': generation_id, 'status': 'published',
    }), encoding='utf-8')
    items = []
    for kind, relative, content in [('pdf', 'main.pdf', b'%PDF-unit-fixture'),
                                    ('latex_source', 'latex-source.zip', b'unit-archive'),
                                    ('report', 'reports/pipeline_log.json', b'{}')]:
        path = output / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        items.append({'kind': kind, 'root': 'output', 'name': path.name, 'relative_path': relative,
                      'size': len(content), 'sha256': hashlib.sha256(content).hexdigest()})
    return {'output_dir': str(output), 'artifact_manifest': {'version': '1.0.0', 'artifacts': items},
            'status': 'success', 'quality_status': 'passed', 'report_dir': str(report_dir),
            'gate_snapshot': {'published': True, 'gate_statuses': {
                'content-fidelity': 'passed', 'structure': 'passed', 'compile': 'passed', 'format': 'passed'}}}


def frozen_values(project, generation_id):
    """创建版本私有确认输入，保留源哈希与修订号。"""
    path = Path(project.workspace_dir) / 'reports' / 'version-inputs' / generation_id / 'confirmed_structure.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps({'confirmed': True, 'revision': project.structure_revision,
                          'project_source_sha256': project.source_sha256}).encode()
    path.write_bytes(content)
    return {'confirmed_snapshot_path': str(path), 'confirmed_snapshot_sha256': hashlib.sha256(content).hexdigest()}


def ready_feedback_state(*, run_id, project_id, parent_id, session_id, feedback_id, delivery):
    """存储测试的明确终验夹具；不声称执行过模型或真实编译。"""
    from copy import deepcopy
    from agents.quality_repair.models import AgentState
    agent = AgentState(run_id=run_id, project_id=project_id, parent_generation_id=parent_id,
        template_id='ouc-bachelor', mode='user_feedback', status='ready',
        goals=[{'goal_id': 'goal-' + run_id, 'kind': 'format', 'target_unit_ids': ['body'],
                'description': '保持用户确认的段落格式', 'confirmed': True, 'status': 'satisfied'}],
        gate_snapshots={'final': {'gate_statuses': deepcopy(delivery['gate_snapshot']['gate_statuses'])}})
    return {'session_id': session_id, 'feedback_ids': [feedback_id], 'agent': agent.to_dict()}
