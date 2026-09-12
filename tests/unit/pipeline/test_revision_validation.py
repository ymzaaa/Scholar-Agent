# -*- coding: utf-8 -*-
"""修订终验只承认已验证动作，不能用同步账本掩盖正文变化。"""

from copy import deepcopy

import pytest

from pipeline.content_fidelity_gate import build_content_fidelity_report, _parse_marked_files
from tests.unit.pipeline.test_content_fidelity_gate import _virtual_case


##### 已确认动作夹具板块 #####

def changed_case(tmp_path):
    extracted, recognized, rendered, path = _virtual_case(tmp_path)
    before, _, _ = _parse_marked_files(tmp_path, rendered.chapter_files)
    old = before["u-000005"]["fragment"]
    new = r"\noindent " + old
    text = path.read_text(encoding="utf-8")
    marker = "% SCHOLAR_UNIT_BEGIN u-000005 body_paragraph\n"
    text = text.replace(marker + old, marker + new)
    path.write_text(text, encoding="utf-8")
    change = {"action_id": "approved-action", "unit_id": "u-000005", "relative_path": rendered.chapter_files[0],
              "old_fragment": old, "new_fragment": new, "kind": "format"}
    rendered.render_trace["approved_changes"] = [change]
    return extracted, recognized, rendered, path, change


##### 来源与实际片段核对板块 #####

def test_confirmed_format_change_passes_without_mutating_original_source(tmp_path):
    extracted, recognized, rendered, _, _ = changed_case(tmp_path)
    source_before, trace_before = deepcopy(extracted.content_units), deepcopy(rendered.render_trace)
    report = build_content_fidelity_report(extracted, recognized, rendered)
    assert report["all_passed"], report["blocking_or_unimplemented"]
    assert extracted.content_units == source_before and rendered.render_trace == trace_before


@pytest.mark.parametrize("fault", ["missing_approval", "wrong_source", "changed_content", "duplicate_action", "unrecorded_addition"])
def test_revision_approval_cannot_mask_unapproved_content_changes(tmp_path, fault):
    extracted, recognized, rendered, path, change = changed_case(tmp_path)
    if fault == "missing_approval":
        rendered.render_trace.pop("approved_changes")
    elif fault == "wrong_source":
        change["unit_id"] = "u-000006"
    elif fault == "changed_content":
        new = change["new_fragment"].replace("重复内容。", "捏造结果。")
        path.write_text(path.read_text(encoding="utf-8").replace(change["new_fragment"], new), encoding="utf-8")
        change["new_fragment"] = new
    elif fault == "duplicate_action":
        rendered.render_trace["approved_changes"].append(deepcopy(change))
    else:
        path.write_text(path.read_text(encoding="utf-8") + "\n无来源追加文字\n", encoding="utf-8")
    assert not build_content_fidelity_report(extracted, recognized, rendered)["all_passed"]


def replacement_case(tmp_path):
    extracted, recognized, rendered, path = _virtual_case(tmp_path)
    before, _, _ = _parse_marked_files(tmp_path, rendered.chapter_files)
    old = before["u-000005"]["fragment"]
    new = old.replace("重复内容。", "用户确认的新文字。")
    marker = "% SCHOLAR_UNIT_BEGIN u-000005 body_paragraph\n"
    path.write_text(path.read_text(encoding="utf-8").replace(marker + old, marker + new), encoding="utf-8")
    approval = {"goal_id": "body-goal", "kind": "body_replace", "confirmed": True,
        "target_unit_ids": ["u-000005"], "replacement": {"old_text": "重复内容。", "new_text": "用户确认的新文字。"}}
    change = {"action_id": "body-action", "goal_id": "body-goal", "unit_id": "u-000005",
              "relative_path": rendered.chapter_files[0], "old_fragment": old, "new_fragment": new, "kind": "body_replace"}
    rendered.render_trace["approved_changes"] = [change]
    rendered.confirmed_body_replacements = [approval]
    return extracted, recognized, rendered, path, change, approval


def test_full_confirmed_body_replacement_is_proved_independently_of_candidate_ledger(tmp_path):
    extracted, recognized, rendered, path, change, approval = replacement_case(tmp_path)
    original = deepcopy(extracted.content_units)
    report = build_content_fidelity_report(extracted, recognized, rendered)
    assert report["all_passed"], report["blocking_or_unimplemented"]
    assert extracted.content_units == original
    assert "用户确认的新文字。" in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("fault", ["no_confirmation", "unconfirmed", "wrong_goal", "wrong_unit", "extra_text", "partial_old"])
def test_candidate_body_ledger_cannot_forge_or_expand_user_confirmation(tmp_path, fault):
    extracted, recognized, rendered, path, change, approval = replacement_case(tmp_path)
    if fault == "no_confirmation":
        rendered.confirmed_body_replacements = []
    elif fault == "unconfirmed":
        approval["confirmed"] = False
    elif fault == "wrong_goal":
        change["goal_id"] = "unknown"
    elif fault == "wrong_unit":
        approval["target_unit_ids"] = ["u-000004"]
    elif fault == "partial_old":
        approval["replacement"]["old_text"] = "重复"
    else:
        modified = change["new_fragment"] + "未经用户确认的文字"
        path.write_text(path.read_text(encoding="utf-8").replace(change["new_fragment"], modified), encoding="utf-8")
        change["new_fragment"] = modified
    assert not build_content_fidelity_report(extracted, recognized, rendered)["all_passed"]


def test_format_after_body_replacement_keeps_both_proofs_in_order(tmp_path):
    extracted, recognized, rendered, path, change, approval = replacement_case(tmp_path)
    old = change["new_fragment"]
    new = r"\noindent " + old
    path.write_text(path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")
    rendered.render_trace["approved_changes"].append({"action_id": "format-after-body", "kind": "format",
        "unit_id": change["unit_id"], "relative_path": change["relative_path"], "old_fragment": old, "new_fragment": new})
    report = build_content_fidelity_report(extracted, recognized, rendered)
    assert report["all_passed"], report["blocking_or_unimplemented"]


def journal_case(tmp_path):
    import hashlib
    import json
    from agents.quality_repair.action_journal import FileActionJournal
    from agents.quality_repair.patching import WriteRegion, prepare_confirmed_text
    extracted, recognized, rendered, path = _virtual_case(tmp_path)
    before, _, _ = _parse_marked_files(tmp_path, rendered.chapter_files)
    (tmp_path / ".scholar-generation.json").write_text(json.dumps({"status": "staging", "generation_id": "candidate"}), encoding="utf-8")
    goal = {"goal_id": "body-goal", "kind": "body_replace", "confirmed": True,
        "target_unit_ids": ["u-000005"], "replacement": {"old_text": "重复内容。", "new_text": "用户明确替换的文字。"}}
    region = WriteRegion(rendered.chapter_files[0], "u-000005", "body_paragraph")
    prepared = prepare_confirmed_text(tmp_path, generation_id="candidate", region=region,
                                     expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest(), goal=goal)
    journal = FileActionJournal(tmp_path, tmp_path / "private-journal", "run", generation_id="candidate")
    journal.apply("body-action", prepared, proposal={"action_id": "body-action", "tool": "replace_confirmed_text",
        "arguments": {"unit_id": "u-000005", "goal_id": "body-goal"}})
    actual, _, _ = _parse_marked_files(tmp_path, rendered.chapter_files)
    return extracted, recognized, rendered, journal, goal, before, actual


def test_actual_body_action_journal_enters_content_gate_with_independent_confirmation(tmp_path):
    from pipeline.revision_validation import _journal_changes
    extracted, recognized, rendered, journal, goal, before, actual = journal_case(tmp_path)
    rendered.render_trace["approved_changes"] = _journal_changes(journal.entries(), goals=[goal], before=before, actual=actual)
    rendered.confirmed_body_replacements = [goal]
    report = build_content_fidelity_report(extracted, recognized, rendered)
    assert report["all_passed"], report["blocking_or_unimplemented"]


@pytest.mark.parametrize("fault", ["unacknowledged", "missing_action", "unknown_goal", "forged_text", "duplicate_replacement"])
def test_body_action_registration_rejects_incomplete_or_forged_execution(tmp_path, fault):
    from pipeline.revision_validation import _journal_changes
    extracted, recognized, rendered, journal, goal, before, actual = journal_case(tmp_path)
    entries = journal.entries()
    if fault == "unacknowledged":
        entries[0]["status"] = "prepared"
    elif fault == "missing_action":
        entries = []
    elif fault == "unknown_goal":
        entries[0]["proposal"]["arguments"]["goal_id"] = "unknown"
    elif fault == "forged_text":
        entries[0]["confirmed_replacement"]["new_text"] = "没有确认的文本"
    else:
        entries.append(deepcopy(entries[0]))
    with pytest.raises(ValueError):
        _journal_changes(entries, goals=[goal], before=before, actual=actual)
