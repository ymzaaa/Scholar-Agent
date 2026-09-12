# -*- coding: utf-8 -*-
"""三模式共用执行图的离线规划、暂停、重复动作和完整终验。"""

from copy import deepcopy
import pytest

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from agents.quality_repair.graph_executor import build_quality_graph
from agents.quality_repair.models import AgentState
from agents.quality_repair.runtime import RepairRuntime


##### 离线运行依赖板块 #####

class ScriptedModel:
    def __init__(self, responses):
        self.responses = responses
        self.calls = 0

    def invoke_tools(self, **kwargs):
        response = self.responses[self.calls]
        self.calls += 1
        return deepcopy(response)


class FakeTools:
    def __init__(self):
        self.executed = []

    def definitions(self, mode):
        names = ["read_source"] + ([] if mode == "recognition_review" else ["apply_protected_patch"])
        return [{"name": name, "input_schema": {"type": "object", "properties": {
            "unit_id": {"type": "string"}, "value": {"type": "string"},
        }, "required": ["unit_id"], "additionalProperties": False}} for name in names]

    def input_hash(self, action, state):
        return "stable-input"

    def execute(self, action, state):
        self.executed.append(deepcopy(action))
        return {"status": "ok", "changed_files": ["chapter.tex"] if action["tool"] == "apply_protected_patch" else [],
                "detail": "操作成功"}


def action(tool="apply_protected_patch", value="6pt"):
    return {"tool_calls": [{"name": tool, "arguments": {"unit_id": "u-1", "value": value}}]}


def setup_run(responses, *, mode="user_feedback", satisfied=True, gates=None):
    model = ScriptedModel(responses)
    tools = FakeTools()
    events = []
    state = AgentState(run_id="test-run", project_id="test-project", mode=mode, template_id="ouc-bachelor",
                       goals=[{"goal_id": "g-1", "target_unit_ids": ["u-1"], "confirmed": True, "status": "pending"}])

    def observe(state, observation):
        events.append("observe")
        return {"goal_satisfied": satisfied, "repairable": True}

    def final_validate(state):
        events.append("full_validation")
        return {"gate_statuses": gates or {name: "passed" for name in
                ("content-fidelity", "structure", "compile", "format")}, "issues": []}

    runtime = RepairRuntime(None, model, tools, lambda state: {"goals": state.goals},
                            final_validate, observe, lambda: events.append("rollback_all"),
                            lambda: (None, ""), authorized=lambda state: True,
                            rollback_action=lambda action: events.append("rollback_action"))
    graph = build_quality_graph(runtime=runtime, checkpointer=InMemorySaver())
    return graph, state, model, tools, events


def invoke(graph, state):
    return graph.invoke({"agent": state.to_dict()}, config={"configurable": {"thread_id": state.run_id}})


##### 行动与终验板块 #####

def test_action_identity_is_checkpointed_before_execution_and_reused_on_resume(monkeypatch):
    graph, state, model, tools, events = setup_run([action()])
    execute = tools.execute
    interrupted = []
    def first_call(proposal, current):
        interrupted.append(deepcopy(proposal))
        raise KeyboardInterrupt("interrupted before acknowledgment")
    monkeypatch.setattr(tools, "execute", first_call)
    config = {"configurable": {"thread_id": state.run_id}}
    with pytest.raises(KeyboardInterrupt):
        graph.invoke({"agent": state.to_dict()}, config=config)
    saved = graph.get_state(config).values["agent"]
    assert saved["pending_action"]["action_id"] == state.run_id + '-' + saved["action_signatures"][0]
    monkeypatch.setattr(tools, "execute", execute)
    result = graph.invoke(None, config=config)["agent"]
    assert result["status"] == "ready" and model.calls == 1
    assert tools.executed[0]["action_id"] == interrupted[0]["action_id"]
    assert len(result["action_signatures"]) == 1


def test_same_action_in_different_runs_has_distinct_trace_identity():
    results = []
    for identifier in ('first-run', 'later-run'):
        graph, state, model, tools, events = setup_run([action()])
        state.run_id = identifier
        results.append(invoke(graph, state)['agent'])
    assert all(item['status'] == 'ready' for item in results)
    assert results[0]['action_signatures'] == results[1]['action_signatures']
    assert results[0]['observations'][0]['action_id'] != results[1]['observations'][0]['action_id']

def test_contradictory_final_snapshot_cannot_publish_despite_passed_statuses():
    graph, state, model, tools, events = setup_run([action()])
    # 直接绑定带阻断问题的终验，防止只看总状态掩盖异常。
    runtime = RepairRuntime(None, model, tools, lambda state: {"goals": state.goals},
        lambda state: {"gate_statuses": dict.fromkeys(("content-fidelity", "structure", "compile", "format"), "passed"),
                       "issues": [{"severity": "block", "repairable": False, "code": "internal-error"}]},
        lambda *args: {"goal_satisfied": True}, lambda: events.append("rollback_all"),
        lambda: (None, ""), authorized=lambda state: True)
    graph = build_quality_graph(runtime=runtime, checkpointer=InMemorySaver())
    result = invoke(graph, state)["agent"]
    assert result["status"] == "failed" and result["stop_reason"] == "internal_error"
    assert events == ["rollback_all"]


def test_pause_preserves_private_progress_and_stop_rolls_back_batch():
    graph, state, model, tools, events = setup_run([action(), {"status": "ask_user", "detail": "请补充后续目标"}])
    state.goals.append({"goal_id": "g-2", "target_unit_ids": ["u-2"], "confirmed": True, "status": "pending"})
    state.goals[0]['completed_unit_ids'] = ['u-1']
    state.protections = [{'unit_id': 'u-1', 'goal_ids': ['g-1'], 'fragment': 'changed'}]
    result = invoke(graph, state)["agent"]
    assert result["status"] == "waiting_user" and result["pending_action"] is None
    assert events == ["observe"]
    assert [goal["status"] for goal in result["goals"]] == ['satisfied', 'pending']
    assert result['goals'][0]['completed_unit_ids'] == ['u-1']
    assert result['protections'] == state.protections
    stopped = graph.invoke(Command(resume={'continue': False}),
        config={'configurable': {'thread_id': state.run_id}})['agent']
    assert stopped['status'] == 'stopped' and stopped['stop_reason'] == 'user_stopped'
    assert events == ['observe', 'rollback_all']

def test_successful_action_runs_full_gates_without_extra_model_finish():
    graph, state, model, tools, events = setup_run([action()])
    result = invoke(graph, state)["agent"]
    assert result["status"] == "ready"
    assert model.calls == 1 and len(tools.executed) == 1
    assert events == ["observe", "full_validation"]


def test_validation_phase_is_checkpointed_before_gates_and_resumes_without_replanning():
    state = AgentState(run_id='validation-run', project_id='project', mode='initial_generation',
        template_id='ouc-bachelor', goals=[{'goal_id': 'done', 'status': 'satisfied'}])
    model = ScriptedModel([])
    attempts = []

    def validate(current):
        attempts.append(current.status)
        if len(attempts) == 1:
            raise KeyboardInterrupt('interrupted in gates')
        return {'gate_statuses': dict.fromkeys(('content-fidelity', 'structure', 'compile', 'format'), 'passed')}

    runtime = RepairRuntime(None, model, FakeTools(), lambda current: {}, validate,
        lambda *args: {}, lambda: None, lambda: (None, ''), authorized=lambda current: True)
    graph = build_quality_graph(runtime=runtime, checkpointer=InMemorySaver())
    config = {'configurable': {'thread_id': state.run_id}}
    with pytest.raises(KeyboardInterrupt):
        graph.invoke({'agent': state.to_dict()}, config=config)
    assert graph.get_state(config).values['agent']['status'] == 'validating'
    result = graph.invoke(None, config=config)['agent']
    assert result['status'] == 'ready' and attempts == ['validating', 'validating']
    assert model.calls == 0 and result['planning_round'] == 0


def test_duplicate_action_stops_second_execution_and_rolls_back_batch():
    graph, state, model, tools, events = setup_run([action(), action()], satisfied=False)
    result = invoke(graph, state)["agent"]
    assert result["status"] == "stopped" and result["stop_reason"] == "duplicate_action"
    assert len(tools.executed) == 1 and model.calls == 2
    assert "rollback_action" in events
    assert events[-1] == "rollback_all"


def test_same_tool_can_change_parameters_for_three_rounds_then_roll_back():
    graph, state, model, tools, events = setup_run([action(value=f"{n}pt") for n in (6, 5, 4)], satisfied=False)
    result = invoke(graph, state)["agent"]
    assert result["stop_reason"] == "planning_limit"
    assert result["planning_round"] == 3 and model.calls == 3 and len(tools.executed) == 3
    assert events[-1] == "rollback_all"


def test_full_content_failure_cannot_be_replanned_or_published():
    graph, state, model, tools, events = setup_run([action()], gates={
        "content-fidelity": "failed", "structure": "not_run", "compile": "not_run", "format": "not_run",
    })
    result = invoke(graph, state)["agent"]
    assert result["status"] == "failed" and result["stop_reason"] == "protected_content_changed"
    assert model.calls == 1 and events[-1] == "rollback_all"


def test_finish_does_not_override_unsatisfied_goals():
    graph, state, model, tools, events = setup_run([{"status": "finish"}], satisfied=False)
    result = invoke(graph, state)["agent"]
    assert result["status"] != "ready"
    assert "rollback_all" in events


##### 只读与暂停恢复板块 #####

def test_recognition_finish_is_readonly_and_keeps_user_confirmation_separate():
    suggestion = {"unit_id": "u-1", "suggested_level": "section", "rationale": "编号证据"}
    graph, state, model, tools, events = setup_run([{"status": "finish", "suggestions": [suggestion]}], mode="recognition_review")
    result = invoke(graph, state)["agent"]
    assert result["suggestions"] == [suggestion]
    assert not tools.executed and "full_validation" not in events
    assert result["status"] == "waiting_user" and result["stop_reason"] == "structure_confirmation"
    confirmed = graph.invoke(Command(resume={"continue": True, "structure_confirmed": True}),
                            config={"configurable": {"thread_id": state.run_id}})["agent"]
    assert confirmed["status"] == "ready" and model.calls == 1


def test_pause_resumes_same_graph_without_resetting_planning_round():
    graph, state, model, tools, events = setup_run([
        {"status": "ask_user", "detail": "请明确段后距"}, action(),
    ])
    result = invoke(graph, state)
    assert result["agent"]["status"] == "waiting_user"
    assert result["agent"]["pending_action"] is None
    assert result["__interrupt__"]
    resumed = graph.invoke(Command(resume={"continue": True}),
                           config={"configurable": {"thread_id": state.run_id}})
    assert resumed["agent"]["planning_round"] == 2
    assert resumed["agent"]["status"] == "ready"


def test_initial_generation_uses_the_same_graph():
    graph, state, model, tools, events = setup_run([action()], mode="initial_generation")
    assert invoke(graph, state)["agent"]["status"] == "ready"


def test_internal_observer_failure_rolls_back_and_is_not_a_paper_issue():
    graph, state, model, tools, events = setup_run([action()])
    # 不修改门禁期望，通过真实抛出程序异常验证统一异常边界。
    def fail_execute(action, state):
        raise RuntimeError("injected internal failure")
    tools.execute = fail_execute
    result = invoke(graph, state)["agent"]
    assert result["status"] == "failed" and result["stop_reason"] == "internal_error"
    assert events[-1] == "rollback_all"


def test_observation_completes_only_the_goal_it_proves_for_shared_target():
    graph, state, model, tools, events = setup_run([action(value="6pt"), action(value="8pt")])
    state.goals.append({"goal_id": "g-2", "target_unit_ids": ["u-1"], "confirmed": True, "status": "pending"})
    runtime = RepairRuntime(None, model, tools, lambda state: {"goals": state.goals},
        lambda state: {"gate_statuses": dict.fromkeys(("content-fidelity", "structure", "compile", "format"), "passed")},
        lambda state, observation: {"goal_satisfied": True, "completed_goal_ids": [f"g-{len(tools.executed)}"]},
        lambda: events.append("rollback_all"), lambda: (None, ""), authorized=lambda state: True)
    result = invoke(build_quality_graph(runtime=runtime, checkpointer=InMemorySaver()), state)["agent"]
    assert result["status"] == "ready" and model.calls == 2 and len(tools.executed) == 2
    assert all(goal["status"] == "satisfied" for goal in result["goals"])


def test_five_deterministic_replacements_leave_all_three_planning_rounds_available():
    graph, state, model, tools, events = setup_run([action("read_source", value="first"),
        action("read_source", value="second"), action()])
    state.goals = [{"goal_id": f"body-{index}", "target_unit_ids": [f"u-{index}"],
                    "kind": "body_replace", "confirmed": True, "status": "pending"} for index in range(2, 7)] + state.goals
    def deterministic(current):
        goal = next((item for item in current.goals if item.get("kind") == "body_replace" and item["status"] == "pending"), None)
        return {"kind": "action", "tool": "apply_protected_patch", "arguments": {"unit_id": goal["target_unit_ids"][0]}} if goal else None
    def observe(current, observation):
        unit_id = tools.executed[-1]["arguments"]["unit_id"]
        return {"goal_satisfied": True, "completed_goal_ids": [item["goal_id"] for item in current.goals if item["target_unit_ids"] == [unit_id]]}
    runtime = RepairRuntime(None, model, tools, lambda state: {"goals": state.goals},
        lambda state: {"gate_statuses": dict.fromkeys(("content-fidelity", "structure", "compile", "format"), "passed")},
        observe, lambda: events.append("rollback_all"), lambda: (None, ""),
        deterministic_action=deterministic, authorized=lambda state: True)
    result = invoke(build_quality_graph(runtime=runtime, checkpointer=InMemorySaver()), state)["agent"]
    assert result["status"] == "ready" and result["planning_round"] == 3 and model.calls == 3
    assert len(tools.executed) == 8 and not events
