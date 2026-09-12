# -*- coding: utf-8 -*-
"""P8 格式规范 Agent 的最小契约测试。"""

from __future__ import annotations

from types import SimpleNamespace
import json
import pytest

from adapters.ouc import OUCTemplateAdapter
from agents.quality_repair.action_journal import FileActionJournal
from llm.policy import (
    CAPTION_TRANSLATION_TASK, HEADING_TRANSLATION_TASK, LLMAuthorization,
)
from pipeline.quality_report import build_user_quality_report
from pipeline.quality_issues import format_issues
from pipeline.cover_metadata import resolve_cover_metadata
from pipeline.text_processing import escape_cell, escape_text
from pipeline.translate import _collect_missing_language_inputs, _translate_all, prepare_required_language


class _Profile:
    def requires_english_caption(self, kind: str) -> bool:
        return kind in {"figure", "table"}


class _TranslationClient:
    configured = True
    call_records = []

    def complete_text(self, _system, user, **_kwargs):
        identifiers = [
            int(line.split(" |", 1)[0].split("=", 1)[1])
            for line in user.splitlines() if line.startswith("ID=")
        ]
        return "\n".join(f"ID={index}: English {index}" for index in identifiers)

    def usage_summary(self):
        return {"call_count": 1}


@pytest.mark.parametrize('response', [
    'ID=0: English\nID=99: Extra',
    'Here is the translation:\nID=0: English',
    'ID=0: English\nID=0: Duplicate',
    'ID=99: Wrong target',
    None,
])
def test_translation_rejects_invalid_complete_response_without_retry(response):
    class Client:
        calls = 0

        def complete_text(self, *args, **kwargs):
            self.calls += 1
            return response

    client = Client()
    translated, error = _translate_all(client, LLMAuthorization.offline(), [('chapter', '标题')])
    assert error is not None and translated == {}
    assert client.calls == 1


def test_required_translation_is_one_guarded_agent_action(tmp_path):
    (tmp_path / '.scholar-generation.json').write_text(json.dumps({'status': 'staging', 'generation_id': 'candidate'}))
    contents = tmp_path / "contents"
    contents.mkdir()
    path = contents / "section_1.tex"
    source = (
        "% SCHOLAR_TEMPLATE_HEADING_EN u-000001 chapter\n"
        "\\enchapter{中文标题}\n"
        "\\figurecaption{fig:test}{中文图题}{}\n"
        "\\tablecaption{中文表题}{}\n"
    )
    path.write_text(source, encoding="utf-8")
    records = [{
        "target_file": "contents/section_1.tex", "unit_id": "u-000001",
        "level": "chapter", "text_zh": "中文标题",
    }]
    authorization = LLMAuthorization.for_tasks(
        [HEADING_TRANSLATION_TASK, CAPTION_TRANSLATION_TASK],
        external_processing_allowed=True, source="test",
    )
    prepared = prepare_required_language(
        tmp_path, generation_id='candidate', template_adapter=OUCTemplateAdapter(),
        heading_records=records,
        categories=['heading', 'caption'],
        authorization=authorization, client=_TranslationClient(),
    )
    assert len(prepared['actions']) == 1
    assert path.read_text(encoding='utf-8') == source
    adapter = OUCTemplateAdapter()
    journal = FileActionJournal(tmp_path, tmp_path / 'audit', 'language-run', generation_id='candidate')
    action = prepared['actions'][0]
    journal.apply(action['action_id'], action['prepared'], proposal=action['proposal'])
    rendered = path.read_text(encoding="utf-8")
    assert "\\enchapter{English 0}" in rendered
    assert "中文标题" not in rendered.split("\\enchapter", 1)[1].splitlines()[0]
    assert "中文图题" in rendered and "中文表题" in rendered
    _, remaining = _collect_missing_language_inputs(
        tmp_path, adapter, records, category="heading",
    )
    assert remaining == []
    journal.rollback_all()
    assert path.read_text(encoding='utf-8') == source


def test_unicode_compatibility_is_consistent_for_text_and_table():
    paragraph = escape_text("配置（√×）与结果（✓✗）")
    table = escape_cell("√ × ✓ ✗")
    for command in (r"\surd", r"\times", r"\checkmark"):
        assert command in paragraph
        assert command in table


def test_user_report_separates_findings_from_coverage_limitations():
    report = {
        "published": False, "quality_status": "blocked",
        "agent_run": None,
        "llm_tasks": [],
        "format_check": {"rules": {
            "COMMON-LOG-MISSING-GLYPH": {
                "status": "fail", "disposition": "block",
                "category": "字体与字符", "item": "不得缺字",
                "detail": "发现 2 个缺失字符", "evidence": [{}, {}],
            },
            "OUC-UNIMPLEMENTED": {
                "status": "not_implemented", "disposition": "degrade",
                "category": "版心", "item": "精确版心测量",
            },
        }},
    }
    report["issues"] = format_issues(report["format_check"]["rules"])
    result = build_user_quality_report(report)
    assert result["findings"][0]["occurrence_count"] == 2
    assert result["findings"][0]["severity"] == "block"
    assert result["coverage_limitations"][0]["rule_id"] == "manual-layout-review"


def test_user_report_explains_template_baseline_warnings_separately():
    report = {
        "published": True, "quality_status": "passed", "llm_tasks": [],
        "agent_run": None,
        "format_check": {"rules": {
            "COMMON-LOG-OVERFULL": {
                "status": "pass", "disposition": "degrade",
                "category": "版面溢出", "item": "Overfull 盒子",
                "detail": "与模板基线一致。",
                "evidence": [{
                    "origin": "template_baseline", "baseline_id": "cover",
                    "file": "main.log", "line": 10,
                }],
            },
        }},
    }
    result = build_user_quality_report(report)
    assert result["findings"] == []
    assert result["template_warnings"][0]["occurrence_count"] == 1
    assert result["delivery"]["title"] == "PDF 已生成，发布门禁通过"


def test_cover_metadata_does_not_cross_assign_adjacent_labels():
    paragraphs = [
        {"text": "分类号：       学校代码：10423", "style": "Normal"},
        {"text": "UDC:           学    号: 21241113020", "style": "Normal"},
        {"text": "基于细胞微环境的论文题目", "style": "Normal"},
        {"text": "Cell Microenvironment Thesis Title", "style": "Normal"},
        {"text": "日期： 年 月", "style": "Normal"},
    ]
    tables = [{"data": [
        ["作者", "：", "测试作者"], ["指导教师", "：", "测试导师"],
        ["专业名称", "：", ""],
    ]}]
    result = resolve_cover_metadata(paragraphs, tables)
    assert result["classification"] == "【待填写】"
    assert result["udc"] == "【待填写】"
    assert result["student_id"] == "21241113020"
    assert result["title"] == "基于细胞微环境的论文题目"
    assert result["title_en"] == "Cell Microenvironment Thesis Title"
    assert result["major"] == "【待填写】"
    assert result["completion_date"] == "【待填写】"


@pytest.mark.parametrize('status,reason,expected', [
    ('ready', '', '已修复并复查'),
    ('failed', 'invalid_model_response', '未改善，已恢复原结果'),
    ('failed', 'rollback_failed', '回滚未完成，请联系维护人员'),
])
def test_user_report_uses_actual_agent_actions_without_internal_diagnostics(status, reason, expected):
    result = build_user_quality_report({'published': status == 'ready', 'agent_run': {
        'status': status, 'stop_reason': reason, 'detail': 'private-model-tool-error',
        'observations': [{'tool': 'read_source', 'changed_files': []},
                         {'tool': 'apply_protected_patch', 'changed_files': ['contents/section_01.tex']}],
    }})
    assert result['agent']['detail'] == ''
    assert result['agent']['actions'] == [{'tool': '自动修复', 'status': expected,
        'changed_files': ['contents/section_01.tex']}]
    assert 'private-model-tool-error' not in str(result)


def test_user_report_without_started_agent_has_no_actions():
    result = build_user_quality_report({'published': True, 'agent_run': None})
    assert result['agent'] == {'status': '未启动自动修复', 'detail': '', 'actions': []}
