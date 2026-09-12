# -*- coding: utf-8 -*-
"""单一状态、固定权限、持久化规划轮次及动作去重契约。"""

import json

import pytest

from agents.quality_repair.models import AgentState, RunStatus
from agents.quality_repair.runtime import RepairRuntime, action_signature, permitted_tools, reserve_planning_round, validate_parent


##### 状态往返板块 #####

def make_state(**kwargs):
    return AgentState(run_id="run-1", project_id="project-1", mode="user_feedback",
                      template_id="ouc-graduate", **kwargs)


def test_minimal_recovery_state_roundtrip():
    state = make_state(parent_generation_id="parent-1", parent_source_sha256="a" * 64)
    state.goals = [{"goal_id": "g-1", "confirmed": True, "status": "pending"}]
    state.observations = [{"tool": "read_source", "success": True, "evidence_ids": ["e-1"]}]
    state.planning_round = 2
    encoded = json.loads(json.dumps(state.to_dict()))
    assert AgentState.from_dict(encoded).to_dict() == encoded
    assert set(encoded) == {'run_id', 'project_id', 'mode', 'template_id', 'parent_generation_id',
        'parent_source_sha256', 'status', 'stop_reason', 'detail', 'planning_round', 'goals', 'evidence',
        'pending_action', 'action_signatures', 'observations', 'protections', 'gate_snapshots',
        'suggestions', 'authorization'}


def test_only_six_agent_statuses():
    assert {item.value for item in RunStatus} == {
        "running", "waiting_user", "validating", "ready", "stopped", "failed",
    }


def test_pause_clears_pending_action_before_checkpoint():
    state = make_state()
    state.pending_action = {"tool": "apply_protected_patch", "arguments": {}}
    state.pause("missing_target", "请指定目标")
    assert state.to_dict()["pending_action"] is None
    assert state.status == RunStatus.WAITING_USER
    assert state.stop_reason == "missing_target"


##### 固定权限与停止板块 #####

def test_recognition_mode_is_readonly_and_cannot_compile_or_publish():
    names = permitted_tools("recognition_review")
    assert "read_source" in names
    assert "apply_protected_patch" not in names
    assert "replace_confirmed_text" not in names
    assert not {"compile", "run_gates", "publish"} & names


def test_only_feedback_can_replace_confirmed_body_text():
    assert "replace_confirmed_text" in permitted_tools("user_feedback")
    assert "replace_confirmed_text" not in permitted_tools("initial_generation")
    assert "apply_protected_patch" in permitted_tools("initial_generation")


def test_planning_has_fixed_three_round_stop_persisted_across_reload():
    state = make_state()
    for expected in (1, 2, 3):
        assert reserve_planning_round(state) is True
        assert state.planning_round == expected
        state = AgentState.from_dict(state.to_dict())
    assert reserve_planning_round(state) is False
    assert state.planning_round == 3
    assert state.status == RunStatus.STOPPED
    assert state.stop_reason == "planning_limit"


@pytest.mark.parametrize("count", [-1, 4, True])
def test_invalid_persisted_round_is_not_silently_reset(count):
    value = make_state().to_dict()
    value["planning_round"] = count
    with pytest.raises(ValueError):
        AgentState.from_dict(value)


def test_action_signature_is_stable_but_changes_with_input_target_or_arguments():
    first = action_signature("read_source", ["u-1"], {"radius": 1, "text": "原文"}, "hash-a")
    assert first == action_signature("read_source", ["u-1"], {"text": "原文", "radius": 1}, "hash-a")
    assert first != action_signature("read_source", ["u-1"], {"radius": 1, "text": "原文"}, "hash-b")
    assert first != action_signature("read_source", ["u-2"], {"radius": 1, "text": "原文"}, "hash-a")
    assert first != action_signature("read_source", ["u-1"], {"radius": 2, "text": "原文"}, "hash-a")


def test_parent_change_stops_resumption():
    state = make_state(parent_generation_id="parent-1", parent_source_sha256="hash-a")
    state.pause("missing_target", "请补充")
    assert validate_parent(state, "parent-2", "hash-a") is False
    assert state.status == RunStatus.STOPPED
    assert state.stop_reason == "parent_changed"


def test_interrupted_model_request_cannot_be_reissued_after_reload(tmp_path):
    runtime = RepairRuntime(None, None, None, lambda state: {}, lambda state: {},
                            lambda state, observation: {}, lambda: None, lambda: (None, ""),
                            journal_root=tmp_path)
    state = make_state(planning_round=1)
    runtime.claim_planning_call(state)
    restored = AgentState.from_dict(state.to_dict())
    with pytest.raises(RuntimeError, match="重发"):
        runtime.claim_planning_call(restored)
    assert len(list(tmp_path.rglob("planning-*.json"))) == 1
