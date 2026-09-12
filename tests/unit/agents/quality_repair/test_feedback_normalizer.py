# -*- coding: utf-8 -*-
"""反馈理解只生成待确认目标，不能生成补丁或发明格式要求。"""

from copy import deepcopy

import pytest

from agents.quality_repair.feedback_normalizer import NORMALIZATION_PROMPT, normalize_feedback, confirm_goal_drafts


##### 输入夹具板块 #####

FEEDBACKS = [{"feedback_id": "f-1", "text": "将这段的段后距离设为6pt", "selected_unit_ids": ["u-1"]}]
EVIDENCE = [{"unit_id": "u-1", "role": "body_paragraph", "text": "原始普通正文。"}]
DRAFT = {"feedback_ids": ["f-1"], "kind": "format", "target_unit_ids": ["u-1"],
         "description": "段后距离设为6pt", "missing_information": []}


class FakeNormalizer:
    def __init__(self, output):
        self.output = output
        self.calls = 0

    def invoke_tools(self, **kwargs):
        self.calls += 1
        assert kwargs["tools"] == []
        return deepcopy(self.output)


##### 目标理解板块 #####

def test_prompt_keeps_user_granularity_and_separates_internal_identifiers():
    assert '不把文字要求换算成数字、单位、比例或实现参数' in NORMALIZATION_PROMPT
    assert '反馈 ID 和内容单元 ID 只填入对应 ID 字段' in NORMALIZATION_PROMPT
    assert '需要用户补充的格式参数放入 missing_information，不自行补全' in NORMALIZATION_PROMPT


def test_one_call_for_entire_batch_and_no_input_mutation():
    feedbacks = deepcopy(FEEDBACKS)
    evidence = deepcopy(EVIDENCE)
    model = FakeNormalizer({"goal_drafts": [DRAFT]})
    drafts = normalize_feedback(model, feedbacks=feedbacks, evidence=evidence)
    assert model.calls == 1
    assert drafts[0]["goal_id"]
    assert drafts[0]["confirmed"] is False
    assert drafts[0]["request"] == FEEDBACKS[0]["text"]
    assert feedbacks == FEEDBACKS and evidence == EVIDENCE


@pytest.mark.parametrize("change", [
    {"tool": "apply_patch"}, {"edits": []}, {"new_text": "虚构论文"},
    {"target_unit_ids": ["unknown"]}, {"feedback_ids": ["unknown"]},
    {"description": "段后距离设为18pt"}, {"kind": "execute_shell"},
])
def test_invalid_normalization_never_retries(change):
    model = FakeNormalizer({"goal_drafts": [{**DRAFT, **change}]})
    with pytest.raises(ValueError):
        normalize_feedback(model, feedbacks=FEEDBACKS, evidence=EVIDENCE)
    assert model.calls == 1


def test_every_feedback_must_be_accounted_for():
    model = FakeNormalizer({"goal_drafts": [DRAFT]})
    with pytest.raises(ValueError, match="遗漏"):
        normalize_feedback(model, feedbacks=FEEDBACKS + [{"feedback_id": "f-2", "text": "这里不对"}],
                           evidence=EVIDENCE)


def test_missing_target_stays_a_draft_and_cannot_be_confirmed():
    model = FakeNormalizer({"goal_drafts": [{**DRAFT, "target_unit_ids": [],
                                             "missing_information": ["请指定段落"]}]})
    drafts = normalize_feedback(model, feedbacks=FEEDBACKS, evidence=EVIDENCE)
    with pytest.raises(ValueError):
        confirm_goal_drafts(drafts, decisions=[{"goal_id": drafts[0]["goal_id"]}], evidence=EVIDENCE)


##### 用户确认板块 #####

def test_confirmation_creates_goals_without_mutating_drafts():
    model = FakeNormalizer({"goal_drafts": [DRAFT]})
    drafts = normalize_feedback(model, feedbacks=FEEDBACKS, evidence=EVIDENCE)
    original = deepcopy(drafts)
    goals = confirm_goal_drafts(drafts, decisions=[{"goal_id": drafts[0]["goal_id"]}], evidence=EVIDENCE)
    assert goals[0]["confirmed"] is True
    assert goals[0]["status"] == "pending"
    assert drafts == original


def test_confirmed_body_replacement_requires_full_original_and_new_text():
    drafts = [{**DRAFT, "goal_id": "g-1", "kind": "body_replace", "confirmed": False}]
    decisions = [{"goal_id": "g-1", "replacement": {"old_text": EVIDENCE[0]["text"],
                                                    "new_text": "用户确认的新正文。"}}]
    goals = confirm_goal_drafts(drafts, decisions=decisions, evidence=EVIDENCE)
    assert goals[0]["replacement"] == decisions[0]["replacement"]


@pytest.mark.parametrize("role", ["display_formula", "table_caption", "heading_1", "citation"])
def test_body_replacement_cannot_target_semantic_objects(role):
    drafts = [{**DRAFT, "goal_id": "g-1", "kind": "body_replace"}]
    with pytest.raises(ValueError):
        confirm_goal_drafts(drafts, decisions=[{"goal_id": "g-1", "replacement": {
            "old_text": "原始普通正文。", "new_text": "新正文。",
        }}], evidence=[{**EVIDENCE[0], "role": role}])


def test_more_than_five_body_replacements_are_rejected():
    drafts = [{**DRAFT, "goal_id": f"g-{i}", "kind": "body_replace"} for i in range(6)]
    decisions = [{"goal_id": item["goal_id"], "replacement": {
        "old_text": "原始普通正文。", "new_text": "新正文。",
    }} for item in drafts]
    with pytest.raises(ValueError):
        confirm_goal_drafts(drafts, decisions=decisions, evidence=EVIDENCE)


@pytest.mark.parametrize("decisions", [[], [{"goal_id": "unknown"}], [{"goal_id": "g-1"}] * 2])
def test_confirmation_requires_each_existing_goal_once(decisions):
    with pytest.raises(ValueError):
        confirm_goal_drafts([{**DRAFT, "goal_id": "g-1"}], decisions=decisions, evidence=EVIDENCE)


def test_user_can_resolve_clarification_as_an_explicit_format_goal():
    drafts = [{**DRAFT, "goal_id": "g-1", "kind": "clarification", "target_unit_ids": [],
               "missing_information": ["请指定目标和要求"]}]
    goals = confirm_goal_drafts(drafts, decisions=[{"goal_id": "g-1", "kind": "format",
        "target_unit_ids": ["u-1"], "description": "将段后距离设为6pt"}], evidence=EVIDENCE)
    assert goals[0]["kind"] == "format" and goals[0]["confirmed"]
    assert drafts[0]["kind"] == "clarification"


def test_user_cannot_clear_missing_parameters_by_resubmitting_only_target():
    drafts = [{**DRAFT, "goal_id": "g-1", "missing_information": ["请说明需要的间距"]}]
    with pytest.raises(ValueError, match="信息"):
        confirm_goal_drafts(drafts, decisions=[{"goal_id": "g-1", "target_unit_ids": ["u-1"]}], evidence=EVIDENCE)


def test_user_can_resolve_clarification_as_full_body_replacement():
    drafts = [{**DRAFT, "goal_id": "g-1", "kind": "clarification", "missing_information": ["请提供新文本"]}]
    goals = confirm_goal_drafts(drafts, decisions=[{"goal_id": "g-1", "kind": "body_replace",
        "replacement": {"old_text": EVIDENCE[0]["text"], "new_text": "用户明确提供的新正文。"}}], evidence=EVIDENCE)
    assert goals[0]["kind"] == "body_replace" and goals[0]["replacement"]["new_text"] == "用户明确提供的新正文。"
