# -*- coding: utf-8 -*-
"""内置模板必须同时具备 LaTeX 文件、官方 Word 规范和可信画像。"""

import json
from pathlib import Path

import pytest

from adapters.ouc import OUCTemplateAdapter
from pipeline.template_profile import TemplateProfileError, load_template_format_profile
from pipeline.checker.rule_catalog import build_active_rules


##### 正常画像板块 #####


def test_ouc_profile_binds_built_in_latex_and_official_word() -> None:
    profile = OUCTemplateAdapter().format_contract()
    assert profile.official_spec_path.name == "研究生论文书写规范.docx"
    assert profile.requires_english_caption("figure") is True
    assert profile.requires_english_caption("table") is True
    assert profile.locked_rules["body.line_spacing"] == "20pt"
    assert profile.locked_rules["formula.display_above_skip"] == "0pt"
    assert profile.locked_rules["formula.display_below_skip"] == "6bp"
    assert profile.locked_rules["toc.page_number_right_margin"] == (
        "3em, nonbreaking leader"
    )
    assert profile.requires_english_toc() is True
    assert profile.capability("breakable_table")["strategy"] == "paginate"


def test_bachelor_common_only_profile_does_not_invent_school_rules() -> None:
    from adapters.ouc_bachelor import OUCBachelorTemplateAdapter

    root = Path(__file__).resolve().parents[3]
    metadata, rules = build_active_rules(
        root / "stage1/rules/common-format-rules.json",
        OUCBachelorTemplateAdapter().format_rule_profile(),
    )
    assert metadata["template_id"] == "ouc-bachelor"
    assert metadata["rule_count"] == 1
    assert metadata["active_rule_count"] == metadata["common_rule_count"] + 1
    template_rules = [rule for rule in rules if rule.layer == 'template']
    assert [(rule.rule_id, rule.detector) for rule in template_rules] == [('OUC-BACHELOR-PDF-A4', 'pdf_page_size')]


##### 失败传播板块 #####


def test_profile_rejects_changed_official_spec_hash(tmp_path: Path) -> None:
    template = tmp_path / "template"
    profile_dir = tmp_path / "rules" / "templates"
    template.mkdir(parents=True)
    profile_dir.mkdir(parents=True)
    (template / "main.tex").write_text("test", encoding="utf-8")
    (template / "spec.docx").write_bytes(b"changed")
    payload = {
        "format_contract": {
            "template_root": "../../template",
            "required_latex_files": ["main.tex"],
            "official_spec": {"path": "spec.docx", "sha256": "0" * 64},
            "locked_rules": {}, "conditional_rules": {}, "capabilities": {},
        },
    }
    path = profile_dir / "profile.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(TemplateProfileError, match="哈希"):
        load_template_format_profile(path)
