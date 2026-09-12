# -*- coding: utf-8 -*-
"""多个明确目标共用一次完整终验，任一失败均回滚整批。"""

import pytest

from tests.support.feedback_run import run_fixture_feedback
from template_registry.registry import resolve_builtin_template


##### 多目标受控夹具板块 #####


def sources(template_id):
    directory = resolve_builtin_template(template_id).adapter.content_directory
    return {f"{directory}/chapter{index}.tex":
        f"% SCHOLAR_UNIT_BEGIN u{index} figure_caption\n"
        rf"\begin{{figure}}图{index}\end{{figure}}" + "\n"
        f"% SCHOLAR_UNIT_END u{index}\n" for index in (1, 2)}


def edits():
    return [(f"u{index}", [{"old_text": r"\begin{figure}", "new_text": r"\begin{figure}[htbp]"}])
            for index in (1, 2)]


##### 批次终验与保护板块 #####


@pytest.mark.parametrize("template_id", ["ouc-graduate", "ouc-bachelor"])
def test_two_targets_share_one_complete_terminal_validation(tmp_path, template_id):
    inputs = sources(template_id)
    result, model, validations = run_fixture_feedback(tmp_path, template_id=template_id,
                                                       sources=inputs, edits=edits())
    assert result.status == "ready" and result.template_id == template_id
    assert model.calls == 2 and len(validations) == 1
    assert all(goal["status"] == "satisfied" for goal in validations[0])
    assert len(result.observations) == 2
    assert {item["unit_id"] for item in result.protections} == {"u1", "u2"}
    assert {item["action_id"] for item in result.protections} == {
        item["action_id"] for item in result.observations}
    for path in inputs:
        assert "[htbp]" in (tmp_path / path).read_text(encoding="utf-8")


@pytest.mark.parametrize("template_id", ["ouc-graduate", "ouc-bachelor"])
def test_later_content_violation_rolls_back_previously_completed_target(tmp_path, template_id):
    inputs = sources(template_id)
    proposals = edits()
    proposals[1] = ("u2", [{"old_text": "图2", "new_text": "虚构图片说明"}])
    result, model, validations = run_fixture_feedback(tmp_path, template_id=template_id,
                                                       sources=inputs, edits=proposals)
    assert result.status == "failed" and result.stop_reason == "safety_violation"
    assert model.calls == 2 and validations == []
    for path, original in inputs.items():
        assert (tmp_path / path).read_text(encoding="utf-8") == original


def test_accepted_constraint_violation_never_reaches_final_validation(tmp_path):
    inputs = sources("ouc-graduate")
    protection = {"constraint_id": "original-placement", "scope_ref": "u1",
        "verifier": {"mode": "deterministic", "checker": "tex_unit_contains",
            "relative_path": next(iter(inputs)), "needle": r"\begin{figure}图1"}}
    result, model, validations = run_fixture_feedback(tmp_path, template_id="ouc-graduate",
        sources=inputs, edits=edits(), constraints=[protection])
    assert result.status == "failed" and result.stop_reason == "protected_content_changed"
    assert model.calls == 1 and validations == []
    for path, original in inputs.items():
        assert (tmp_path / path).read_text(encoding="utf-8") == original
