# -*- coding: utf-8 -*-
"""把机器门禁结果整理为面向用户的可操作质量报告。"""

from __future__ import annotations

from typing import Any


##### 解释规则板块 #####


_IMPACTS = {
    "COMMON-LOG-MISSING-GLYPH": "PDF 中对应字符可能缺失，影响内容完整性。",
    "COMMON-LOG-OVERFULL": "部分文字或表格可能超出版心，需要查看对应页面。",
    "COMMON-LOG-UNDERFULL": "局部排版可能较疏松，但通常不影响文字内容。",
    "FORMAT-REQUIRED-HEADINGS": "模板要求英文目录，当前标题尚未生成有效英文。",
    "FORMAT-REQUIRED-CAPTIONS": "模板要求双语题注，当前部分英文题注缺失。",
    "COMMON-COVER-METADATA-PENDING": "封面仍有未提供的字段；PDF 可预览，但提交前必须补全。",
}




##### 报告构造板块 #####


def _finding(raw: dict[str, Any]) -> dict[str, Any]:
    """仅翻译统一问题，不重新检测内容或推导严重度。"""
    code = raw['code']
    return {
        'finding_id': code, 'check_type': raw['check_type'],
        'severity': raw['severity'], 'repairable': raw['repairable'],
        'category': {'content-fidelity': '内容完整性', 'structure': '确认结构',
                     'compile': 'PDF 编译', 'format': '格式与交付'}.get(raw['check_type'], '生成'),
        'title': raw.get('title') or code, 'message': raw['description'],
        'impact': _IMPACTS.get(code, '该问题影响本次生成或交付，请查看对应证据。'),
        'recommended_action': raw['recommended_action'],
        'occurrence_count': raw['occurrence_count'],
        'evidence': raw['evidence'][:12],
    }


def build_user_quality_report(report: dict[str, Any]) -> dict[str, Any]:
    """翻译统一问题和发布事实；模板基线与本次问题保持分开。"""
    findings = [_finding(issue) for issue in report.get('issues', [])]
    template_warnings = []
    format_check = report.get('format_check', {}) or {}
    for code, result in format_check.get('rules', {}).items():
        evidence = [item for item in result.get('evidence', []) if item.get('origin') == 'template_baseline']
        if evidence:
            template_warnings.append({
                'rule_id': code, 'category': '模板编译基线', 'title': result.get('item', code),
                'message': '这些警告与冻结模板的已知基线一致。',
                'occurrence_count': len(evidence), 'evidence': evidence[:12],
            })
    agent = report.get('agent_run') or {}
    labels = {
        'running': '正在自动修复', 'waiting_user': '需要补充信息', 'validating': '正在复查修复结果',
        'ready': '已修复并复查', 'stopped': '未改善，已恢复原结果',
        'failed': '自动修复未完成，已保留原结果',
    }
    rollback_failed = agent.get('stop_reason') == 'rollback_failed'
    actions = [{
        'tool': '自动修复', 'status': ('回滚未完成，请联系维护人员' if rollback_failed else
            '未改善，已恢复原结果' if item.get('rolled_back') or agent.get('status') != 'ready' else '已修复并复查'),
        'changed_files': item.get('changed_files', []),
    } for item in agent.get('observations', []) if item.get('changed_files')]
    blocking_count = sum(item['severity'] == 'block' for item in findings)
    review_count = sum(item['severity'] == 'degrade' for item in findings)
    published = bool(report.get('published'))
    title = ('当前版本未达到交付条件' if not published else
             'PDF 已生成，仍有待处理事项' if findings else 'PDF 已生成，发布门禁通过')
    return {
        'schema_version': '1.0.0',
        'delivery': {
            'status': report.get('quality_status', 'unknown'), 'published': published, 'title': title,
            'summary': f'发现 {blocking_count} 个阻断问题，{review_count} 个需复核问题。',
            'blocking_count': blocking_count, 'review_count': review_count,
        },
        'findings': findings, 'template_warnings': template_warnings,
        'agent': {'status': '回滚未完成，请联系维护人员' if rollback_failed else
                  labels.get(agent.get('status'), '未启动自动修复'), 'detail': '', 'actions': actions},
        'coverage_limitations': [{
            'rule_id': 'manual-layout-review', 'category': '人工复核',
            'title': '精细版式需要结合 PDF 人工复核',
            'message': '自动检查覆盖已实现的动态产物检查，不代表全部版式要求均已验证。',
        }] if format_check.get('rules') else [],
    }
