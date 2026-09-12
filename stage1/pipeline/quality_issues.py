# -*- coding: utf-8 -*-
"""确定性门禁共用的轻量问题结构。"""


##### 问题模型板块 #####


def quality_issue(check_type, code, detail, *, severity='block', evidence=None,
                  occurrence_count=1, repairable=False, action='', title=''):
    """问题次数由检测器提供，不从面向用户的句子中反推。"""
    return {
        'check_type': check_type, 'code': code, 'severity': severity,
        'repairable': repairable, 'description': detail,
        'evidence': list(evidence or []), 'occurrence_count': int(occurrence_count),
        'recommended_action': action or '查看问题证据，修正来源或联系维护人员后重新生成。',
        'title': title or {'content-fidelity': '内容完整性需要处理', 'structure': '确认结构未完整落地',
                           'compile': 'PDF 编译未通过', 'format': '格式检查需要处理'}.get(check_type, '生成问题'),
    }


##### 格式结果转换板块 #####


def format_issues(results):
    """格式执行器的结构化结果转换为统一问题，基线证据单独保存。"""
    issues = []
    for code, result in results.items():
        if result['status'] in {'pass', 'not_applicable'}:
            continue
        evidence = [item for item in result.get('evidence', []) if item.get('origin') != 'template_baseline']
        internal = result['status'] == 'internal_error'
        issues.append(quality_issue(
            'format', code, result.get('detail', ''),
            severity='block' if internal or result.get('disposition') == 'block' else 'degrade',
            evidence=evidence, occurrence_count=result.get('occurrence_count', max(1, len(evidence))),
            title=result.get('item', code),
            repairable=not internal and code in {'FORMAT-REQUIRED-HEADINGS', 'FORMAT-REQUIRED-CAPTIONS'},
        ))
    return issues


##### 早期失败报告板块 #####


def failure_report(stage, code, detail, *, internal=False):
    """预检和渲染失败也使用相同的问题、门禁状态和用户报告契约。"""
    from pipeline.quality_report import build_user_quality_report

    statuses = dict.fromkeys(('content-fidelity', 'structure', 'compile', 'format'), 'not_run')
    statuses[stage] = 'internal_error' if internal else 'failed'
    issue = quality_issue(stage, code, detail,
        action='请保存报告并联系维护人员。' if internal else '请修正 Word 中对应内容后重新提交。')
    report = {
        'published': False, 'quality_status': 'internal_error' if internal else 'blocked',
        'gate_statuses': statuses, 'issues': [issue],
        'compile': {'success': False, 'status': 'not_run'},
        'format_check': {'status': 'not_run'},
    }
    report['gate_snapshot'] = {
        'schema_version': '1.0.0', 'published': False, 'quality_status': report['quality_status'],
        'fidelity_ok': False, 'structure_ok': False, 'compile_ok': False,
        'format_publish_allowed': False, 'blocker_count': 1,
        'gate_statuses': statuses, 'issues': [issue], 'failure_stage': stage, 'report_paths': {},
    }
    report['user_report'] = build_user_quality_report(report)
    return report
