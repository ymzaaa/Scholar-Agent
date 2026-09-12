# -*- coding: utf-8 -*-
"""验证已确认论文要求；保护登记与替代确认在后端事务测试覆盖。"""

import json

from pathlib import Path

import pytest

from pipeline.constraint_verifier import verify_paper_constraints


def test_constraint_verifier_blocks_regression_and_marks_manual_review(tmp_path: Path) -> None:
    target = tmp_path / "contents" / "chapter.tex"
    target.parent.mkdir()
    target.write_text(
        "% SCHOLAR_UNIT_BEGIN u1 body_paragraph\n\\fontsize{12pt}{18pt}正文\n"
        "% SCHOLAR_UNIT_END u1\n",
        encoding="utf-8",
    )
    deterministic = {
        "constraint_id": "c1", "scope_ref": "u1",
        "verifier": {
            "mode": "deterministic", "checker": "tex_unit_contains",
            "relative_path": "contents/chapter.tex", "needle": "\\fontsize{12pt}",
        },
    }
    manual = {"constraint_id": "c2", "verifier": {"mode": "human_review"}}
    results = verify_paper_constraints(tmp_path, [deterministic, manual])
    assert [item["status"] for item in results] == ["passed", "manual_review"]

    deterministic["verifier"]["needle"] = "\\fontsize{14pt}"
    with pytest.raises(RuntimeError, match="约束终验失败"):
        verify_paper_constraints(tmp_path, [deterministic])


@pytest.mark.parametrize('fragment,needle,allowed', [
    ('正文 % \\noindent\n', r'\noindent', False),
    ('% \\noindent\n正文\n', r'\noindent', False),
    ('正文\n', 'body_paragraph', False),
    ('\\noindent 正文\n', r'\noindent', True),
    ('完成率 50\\%\n', r'50\%', True),
])
def test_constraint_requires_live_content_not_comments(tmp_path, fragment, needle, allowed):
    """注释与来源标记不能证明已接受格式仍在，转义百分号保持普通内容。"""
    (tmp_path / 'chapter.tex').write_text(
        '% SCHOLAR_UNIT_BEGIN body body_paragraph\n' + fragment + '% SCHOLAR_UNIT_END body\n',
        encoding='utf-8')
    constraint = {'constraint_id': 'accepted', 'scope_ref': 'body', 'verifier': {
        'mode': 'deterministic', 'checker': 'tex_unit_contains',
        'relative_path': 'chapter.tex', 'needle': needle}}
    if allowed:
        assert verify_paper_constraints(tmp_path, [constraint])[0]['passed'] is True
    else:
        with pytest.raises(RuntimeError, match='约束终验失败'):
            verify_paper_constraints(tmp_path, [constraint])


@pytest.mark.parametrize('fault', [None, 'changed', 'missing', 'duplicate', 'path', 'unit', 'unreadable'])
def test_confirmed_action_protection_uses_existing_trace(tmp_path, fault):
    """跨批保护只保存动作引用，原片段来自外层已校验的父版本渲染追踪。"""
    original = '\\noindent 正文\n'
    actual = '正文\n' if fault == 'changed' else original
    (tmp_path / 'chapter.tex').write_text(
        '% SCHOLAR_UNIT_BEGIN body body_paragraph\n' + actual + '% SCHOLAR_UNIT_END body\n',
        encoding='utf-8')
    entry = {'action_id': 'action', 'unit_id': 'other' if fault == 'unit' else 'body',
        'relative_path': 'other.tex' if fault == 'path' else 'chapter.tex',
        'kind': 'format', 'old_fragment': '正文\n', 'new_fragment': original}
    changes = [] if fault == 'missing' else [entry, entry] if fault == 'duplicate' else [entry]
    reports = tmp_path / 'reports'
    reports.mkdir()
    (reports / 'render_trace.json').write_text(
        '{' if fault == 'unreadable' else json.dumps({'approved_changes': changes}), encoding='utf-8')
    constraint = {'constraint_id': 'prior-goal', 'scope_ref': 'body', 'verifier': {
        'mode': 'deterministic', 'checker': 'confirmed_action',
        'relative_path': 'chapter.tex', 'action_id': 'action'}}
    if fault is None:
        assert verify_paper_constraints(tmp_path, [constraint])[0]['passed'] is True
        assert 'fragment' not in json.dumps(constraint)
    else:
        with pytest.raises((ValueError, RuntimeError)):
            verify_paper_constraints(tmp_path, [constraint])
