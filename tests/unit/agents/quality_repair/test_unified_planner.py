# -*- coding: utf-8 -*-
"""统一规划入口只接受一个动作，终态不携带隐式动作。"""

import json

import pytest

from agents.quality_repair.planner import parse_plan, plan_one_action


##### 输出契约板块 #####

TOOLS = [{"name": "read_source", "description": "读取局部源码", "input_schema": {
    "type": "object", "properties": {"unit_id": {"type": "string"}},
    "required": ["unit_id"], "additionalProperties": False,
}}]


def test_one_native_function_call_is_one_action():
    result = parse_plan({"tool_calls": [{"name": "read_source", "arguments": {
        "unit_id": "u-1",
    }}]}, TOOLS)
    assert result == {"kind": "action", "tool": "read_source", "arguments": {"unit_id": "u-1"}}


@pytest.mark.parametrize("response", [
    {"tool_calls": []},
    {"tool_calls": [{"name": "read_source", "arguments": {"unit_id": "u-1"}}] * 2},
    {"tool_calls": [{"name": "compile", "arguments": {}}]},
    {"tool_calls": [{"name": "read_source", "arguments": "{}"}]},
    {"tool_calls": [{"name": "read_source", "arguments": {}}]},
    {"tool_calls": [{"name": "read_source", "arguments": {"unit_id": 1}}]},
    {"tool_calls": [{"name": "read_source", "arguments": {"unit_id": "u-1", "shell": "bad"}}]},
    {"tool_calls": [{"name": "read_source", "arguments": {"unit_id": "u-1"}}], "status": "finish"},
    {"status": "finish", "edits": [{"new_text": "不可隐式执行"}]},
    {"status": "publish"},
    {"status": "ask_user", "detail": ""},
    [],
])
def test_invalid_or_multiple_actions_fail_closed(response):
    with pytest.raises(ValueError):
        parse_plan(response, TOOLS)


@pytest.mark.parametrize("status", ["finish", "ask_user", "stop"])
def test_structured_terminal_response(status):
    result = parse_plan({"status": status, "detail": "需要查看结果"}, TOOLS)
    assert result == {"kind": status, "detail": "需要查看结果", "suggestions": []}


def test_finish_preserves_readonly_heading_suggestions():
    suggestion = {"unit_id": "u-1", "suggested_level": "section", "rationale": "编号证据一致"}
    assert parse_plan({"status": "finish", "suggestions": [suggestion]}, TOOLS)["suggestions"] == [suggestion]


##### 调用边界板块 #####

def test_prompt_uses_explicit_context_without_serializing_runtime():
    class FakeModel:
        calls = 0

        def invoke_tools(self, *, system, user, tools):
            self.calls += 1
            assert "一个" in system
            assert "论文" in system
            assert tools == TOOLS
            value = json.loads(user)
            assert value["goals"] == [{"goal_id": "g-1", "description": "检查标题"}]
            assert "client" not in value and "authorization" not in value
            return {"status": "ask_user", "detail": "请确认目标标题"}

    model = FakeModel()
    result = plan_one_action(model, mode="recognition_review", tools=TOOLS,
                             context={"goals": [{"goal_id": "g-1", "description": "检查标题"}]})
    assert result["kind"] == "ask_user"
    assert model.calls == 1


def test_invalid_model_response_does_not_retry():
    class FakeModel:
        calls = 0

        def invoke_tools(self, **kwargs):
            self.calls += 1
            return {"status": "invented"}

    model = FakeModel()
    with pytest.raises(ValueError):
        plan_one_action(model, mode="user_feedback", tools=TOOLS, context={"goals": []})
    assert model.calls == 1


def test_prompt_explains_completed_observations_and_existing_stop_rules():
    observation = {'tool': 'read_source', 'status': 'ok',
                   'data': {'unit_id': 'u-1', 'text': '完整正文', 'sha256': 'a' * 64}}

    class FakeModel:
        def invoke_tools(self, *, system, user, tools):
            assert 'last_observation 是上一动作已经执行完成的结果' in system
            assert '相同工具、目标、参数和输入哈希' in system
            assert '再次出现会立即停止整批' in system and '最多只有三轮规划' in system
            assert json.loads(user)['last_observation'] == observation
            return {'status': 'ask_user', 'detail': '请明确缺失的格式要求'}

    assert plan_one_action(FakeModel(), mode='user_feedback', tools=TOOLS,
        context={'planning_round': 2, 'last_observation': observation})['kind'] == 'ask_user'


@pytest.mark.parametrize("context", [
    {"workspace": "D:/secret"}, {"api_key": "secret"}, {"messages": []},
])
def test_unbounded_runtime_fields_cannot_enter_prompt(context):
    with pytest.raises(ValueError):
        plan_one_action(None, mode="user_feedback", tools=TOOLS, context=context)
