# -*- coding: utf-8 -*-
"""冻结反馈案例经统一目标执行图验证；离线终验注入不冒充 PDF 编译。"""

import hashlib
import json
from pathlib import Path

import pytest

from agents.quality_repair.feedback_normalizer import normalize_feedback, confirm_goal_drafts
from tests.support.feedback_run import run_fixture_feedback
from tests.unit.agents.quality_repair.test_feedback_normalizer import FakeNormalizer


##### 冻结输入与目标板块 #####

ROOT = Path(__file__).resolve().parents[4]
FIXTURES = ROOT / "tests/fixtures/feedback"
BASELINES = ROOT / "tests/baselines/feedback"


def _load(identifier):
    source = next(FIXTURES.glob(f"{identifier}_*.json"))
    baseline = json.loads(next(BASELINES.glob(f"{identifier}_*.expected.json")).read_text(encoding="utf-8"))
    assert hashlib.sha256(source.read_bytes()).hexdigest() == baseline["input_sha256"]
    return json.loads(source.read_text(encoding="utf-8")), baseline["expected"]


def _run(case, template_id, root, *, edits=None, gates=None):
    target = case["target"]
    return run_fixture_feedback(root, template_id=template_id,
        sources={target["target_file"]: case["initial_latex"]},
        edits=[(target["unit_id"], edits if edits is not None else case["model_output"]["edits"])],
        constraints=case.get("active_constraints", []), gate_result=gates)


def test_feedback_fixture_hashes_are_frozen():
    for index in range(1, 9):
        case, expected = _load(f"F{index:02d}")
        assert case["case_id"] == f"F{index:02d}" and expected


##### 通用补丁与保护板块 #####


@pytest.mark.parametrize("template_id", ["ouc-graduate", "ouc-bachelor"])
@pytest.mark.parametrize("case_id", ["F01", "F02", "F04", "F06"])
def test_legal_feedback_cases_preserve_content_across_templates(case_id, template_id, tmp_path):
    case, expected = _load(case_id)
    result, model, validations = _run(case, template_id, tmp_path)
    source = (tmp_path / case["target"]["target_file"]).read_text(encoding="utf-8")
    assert result.status == "ready" and model.calls == 1 and len(validations) == 1
    assert result.observations[0]["tool"] == "apply_protected_patch"
    for fragment in expected.get("required_fragments", [expected.get("required_fragment")]):
        if fragment:
            assert fragment in source
    protected = expected.get("protected_text", [])
    for fragment in ([protected] if isinstance(protected, str) else protected) + expected.get("protected_fragments", []):
        assert fragment in source
    if expected.get("forbidden_fragment"):
        assert expected["forbidden_fragment"] not in source
    if case_id == "F04":
        assert r"\fontsize{12pt}" in source


@pytest.mark.parametrize("template_id", ["ouc-graduate", "ouc-bachelor"])
def test_table_feedback_uses_generic_patch_and_declared_template_capability(template_id, tmp_path):
    case, _ = _load("F03")
    old = "\\begin{table}\n\\caption{长表}\n\\begin{tabular}{cc}A&B\\\\\\end{tabular}\n\\end{table}"
    new = r"\begingroup\begin{longtable}{cc}\caption{长表}\\A&B\\\end{longtable}\endgroup"
    result, model, validations = _run(case, template_id, tmp_path, edits=[{"old_text": old, "new_text": new}])
    source = (tmp_path / case["target"]["target_file"]).read_text(encoding="utf-8")
    assert model.calls == 1
    if template_id == "ouc-graduate":
        assert result.status == "ready" and len(validations) == 1
        assert r"\begin{longtable}" in source and r"A&B\\" in source
    else:
        assert result.status == "failed" and result.stop_reason == "safety_violation"
        assert validations == [] and source == case["initial_latex"]


@pytest.mark.parametrize("template_id", ["ouc-graduate", "ouc-bachelor"])
def test_conflicting_feedback_without_explicit_replacement_rolls_back(template_id, tmp_path):
    case, expected = _load("F05")
    result, model, validations = _run(case, template_id, tmp_path)
    assert result.status == "failed" and result.stop_reason == "protected_content_changed"
    assert validations == [] and model.calls == 1
    assert expected["conflict_ids"] == ["font-lock"]
    assert (tmp_path / case["target"]["target_file"]).read_text(encoding="utf-8") == case["initial_latex"]


##### 用户澄清与完整终验板块 #####


def test_ambiguous_feedback_stays_a_draft_without_actions_or_candidate():
    case, _ = _load("F07")
    model = FakeNormalizer({"goal_drafts": [{"feedback_ids": ["f07-1"], "kind": "clarification",
        "target_unit_ids": [], "description": "需要用户指定问题内容", "missing_information": ["请选择具体内容并说明修改要求。"]}]})
    evidence = [{**item, "role": "body_paragraph", "text": "受控邻域。"} for item in case["target_candidates"]]
    drafts = normalize_feedback(model, feedbacks=case["feedbacks"], evidence=evidence)
    assert model.calls == 1 and not drafts[0]["confirmed"]
    with pytest.raises(ValueError):
        confirm_goal_drafts(drafts, decisions=[{"goal_id": drafts[0]["goal_id"]}], evidence=evidence)
    assert "actions" not in drafts[0]


def test_metric_failure_rolls_back_even_when_compile_succeeds(tmp_path):
    case, expected = _load("F08")
    result, model, validations = _run(case, "ouc-graduate", tmp_path,
        edits=[{"old_text": r"\begin{table}", "new_text": r"\begin{table}[htbp]"}],
        gates={"gate_statuses": {"content-fidelity": "passed", "structure": "passed",
            "compile": "passed", "format": "failed"},
            "issues": [{"severity": "block", "repairable": False, "code": "table-visibility-failed"}]})
    assert expected["compile_success"] is True
    assert result.status == "stopped" and result.stop_reason == "final_validation_failed"
    assert len(validations) == 1 and model.calls == 1
    assert (tmp_path / case["target"]["target_file"]).read_text(encoding="utf-8") == case["initial_latex"]
