# -*- coding: utf-8 -*-
"""结构审查、首次修复和反馈修订共用的唯一 LangGraph。"""

from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from .models import AgentState, RunStatus
from .planner import plan_one_action
from .runtime import RepairRuntime, action_signature, reserve_planning_round, validate_parent


##### 三模式统一执行板块 #####

class QualityGraphState(TypedDict, total=False):
    agent: dict[str, Any]
    next_node: str


def build_quality_graph(*, runtime: RepairRuntime, checkpointer: Any):
    """一个动作一个观察；完整门禁和回滚均由确定性外层执行。"""
    def output(state: AgentState, next_node: str = "end") -> dict:
        return {"agent": state.to_dict(), "next_node": next_node}

    def halt(state: AgentState, reason: str, detail: str, *, failed=False) -> dict:
        state.stop(reason, detail, failed=failed)
        try:
            runtime.rollback_all()
        except Exception as exc:
            state.stop("rollback_failed", f"回滚未完成：{type(exc).__name__}", failed=True)
        return output(state)

    def pause(state: AgentState, reason: str, detail: str) -> dict:
        # 等待不等于终态失败：私有动作、目标进度和保护随同一 checkpoint 保留。
        # 用户停止、父版本变化或安全失败仍由 halt 整批回滚，等待阶段绝不发布。
        state.pause(reason, detail)
        return output(state, "wait")

    def prepare(value: QualityGraphState) -> dict:
        state = AgentState.from_dict(value["agent"])
        try:
            if not validate_parent(state, *runtime.parent_identity()):
                return halt(state, state.stop_reason, state.detail)
            if state.status in {RunStatus.READY, RunStatus.STOPPED, RunStatus.FAILED}:
                return output(state)
            if state.goals and all(item.get("status") == "satisfied" for item in state.goals):
                return output(state, "begin_validation")
            deterministic = runtime.deterministic_action(state)
            if deterministic is not None:
                state.pending_action = deterministic
                return output(state, "reserve_action")
            if runtime.model is None or not runtime.authorized(state):
                return pause(state, "authorization_required", "需要针对本次具体任务确认外部处理授权。")
            if not reserve_planning_round(state):
                return halt(state, state.stop_reason, state.detail)
            # 轮次在本节点结束时进入 checkpoint；模型调用发生在下一节点。
            return output(state, "plan")
        except Exception as exc:
            return halt(state, "internal_error", f"准备运行失败：{type(exc).__name__}", failed=True)

    def plan(value: QualityGraphState) -> dict:
        state = AgentState.from_dict(value["agent"])
        try:
            runtime.claim_planning_call(state)
            decision = plan_one_action(runtime.model, mode=state.mode,
                                       tools=runtime.tools.definitions(state.mode),
                                       context=runtime.context(state))
            if decision["kind"] == "action":
                state.pending_action = decision
                return output(state, "reserve_action")
            if decision["kind"] == "ask_user":
                return pause(state, "missing_information", decision["detail"])
            if decision["kind"] == "stop":
                return halt(state, "model_stopped", decision["detail"])
            if state.mode == "recognition_review":
                known = {identifier for goal in state.goals for identifier in goal.get("target_unit_ids", [])}
                suggestions = decision["suggestions"]
                identifiers = [item.get("unit_id") for item in suggestions]
                if len(identifiers) != len(set(identifiers)) or set(identifiers) != known:
                    raise ValueError("结构建议必须覆盖全部已有候选且不能重复。")
                for item in suggestions:
                    if set(item) - {"unit_id", "suggested_level", "rationale", "confidence", "evidence"}:
                        raise ValueError("结构建议不能改写内容。")
                    if item.get("suggested_level") not in {"chapter", "section", "subsection", "body"}:
                        raise ValueError("结构建议层级无效。")
                    rationale = item.get("rationale")
                    confidence = item.get("confidence")
                    evidence = item.get("evidence", [])
                    if not isinstance(rationale, str) or not rationale.strip() or len(rationale) > 2000:
                        raise ValueError("结构建议缺少有效理由。")
                    if confidence is not None and (type(confidence) not in {int, float} or not 0 <= confidence <= 1):
                        raise ValueError("结构建议置信度无效。")
                    if not isinstance(evidence, list) or len(evidence) > 10 or any(
                        not isinstance(value, str) or len(value) > 400 for value in evidence
                    ):
                        raise ValueError("结构建议证据必须是有限文字列表。")
                state.suggestions = suggestions
                return pause(state, "structure_confirmation", "结构建议已准备好，请在集中确认页面核对。")
            if not state.goals or any(item.get("status") != "satisfied" for item in state.goals):
                return halt(state, "goals_unresolved", "仍有目标未经实际操作或检查证明完成。")
            return output(state, "begin_validation")
        except ValueError as exc:
            return halt(state, "invalid_model_response", str(exc), failed=True)
        except Exception as exc:
            return halt(state, "model_call_failed", f"模型调用未完成：{type(exc).__name__}", failed=True)

    def reserve_action(value: QualityGraphState) -> dict:
        state = AgentState.from_dict(value["agent"])
        proposal = state.pending_action
        if not proposal:
            return halt(state, "internal_error", "执行节点没有动作提案。", failed=True)
        try:
            tool = proposal["tool"]
            definitions = {item["name"] for item in runtime.tools.definitions(state.mode)}
            if tool not in definitions:
                return halt(state, "tool_not_permitted", "该模式不允许执行此动作。", failed=True)
            arguments = proposal["arguments"]
            targets = arguments.get("target_unit_ids", [arguments["unit_id"]] if "unit_id" in arguments else [])
            signature = action_signature(tool, targets, arguments, runtime.tools.input_hash(proposal, state))
            if signature in state.action_signatures:
                return halt(state, "duplicate_action", "相同输入的动作已经尝试，已停止重复执行。")
            state.action_signatures.append(signature)
            state.pending_action = {**proposal, "action_id": f'{state.run_id}-{signature}'}
            return output(state, "execute")
        except Exception as exc:
            return halt(state, "internal_error", f"动作预留失败：{type(exc).__name__}", failed=True)

    def execute(value: QualityGraphState) -> dict:
        state = AgentState.from_dict(value["agent"])
        proposal = state.pending_action
        state.pending_action = None
        if not proposal or proposal.get("action_id") not in {
            f'{state.run_id}-{signature}' for signature in state.action_signatures
        }:
            return halt(state, "internal_error", "执行动作尚未完成持久化预留。", failed=True)
        try:
            if not validate_parent(state, *runtime.parent_identity()):
                return halt(state, state.stop_reason, state.detail)
            tool, signature, arguments = proposal["tool"], proposal["action_id"], proposal["arguments"]
            targets = arguments.get("target_unit_ids", [arguments["unit_id"]] if "unit_id" in arguments else [])
            observation = runtime.tools.execute(proposal, state)
            state.observations.append({"action_id": signature, "tool": tool, **observation})
            if observation["status"] == "needs_user_input":
                return pause(state, "missing_information", observation.get("detail", "请补充目标信息。"))
            if observation["status"] == "rejected":
                return halt(state, "safety_violation", observation.get("detail", "动作超出保护边界。"), failed=True)
            if observation["status"] != "ok":
                return output(state, "prepare")
            if not observation.get("changed_files"):
                return output(state, "prepare")
            evaluation = runtime.observe(state, observation)
            state.observations[-1]["evaluation"] = evaluation
            if evaluation.get("safety_violation"):
                return halt(state, "protected_content_changed", "动作违反内容或结构保护，已回滚。", failed=True)
            if evaluation.get('stop_reason'):
                return halt(state, evaluation['stop_reason'], evaluation.get('detail', '当前问题不能继续自动处理。'))
            if not evaluation.get("goal_satisfied") and not evaluation.get('goal_progress'):
                runtime.rollback_action(proposal)
                state.observations[-1]["rolled_back"] = True
            if evaluation.get("goal_satisfied"):
                eligible = {goal["goal_id"] for goal in state.goals
                            if goal.get("status") != "satisfied" and goal.get("target_unit_ids")
                            and set(goal['target_unit_ids']).intersection(targets)
                            and set(goal["target_unit_ids"]) <= set(targets) | set(goal.get('completed_unit_ids', []))}
                completed = set(evaluation.get("completed_goal_ids", []))
                if not completed and len(eligible) == 1:
                    completed = eligible
                if not completed or not completed <= eligible:
                    raise ValueError("局部观察没有明确证明本动作完成的目标。")
                for goal in state.goals:
                    if goal["goal_id"] in completed:
                        goal["status"] = "satisfied"
            return output(state, "prepare")
        except Exception as exc:
            return halt(state, "internal_error", f"动作执行失败：{type(exc).__name__}", failed=True)

    def begin_validation(value: QualityGraphState) -> dict:
        """在耗时门禁前持久化阶段，恢复只重跑终验，不重复规划或动作。"""
        state = AgentState.from_dict(value["agent"])
        state.status = RunStatus.VALIDATING
        return output(state, "validate")

    def validate(value: QualityGraphState) -> dict:
        state = AgentState.from_dict(value["agent"])
        try:
            result = runtime.final_validate(state)
            state.gate_snapshots["final"] = result
            statuses = result.get("gate_statuses", {})
            expected = {"content-fidelity", "structure", "compile", "format"}
            if set(statuses) != expected or "internal_error" in statuses.values():
                return halt(state, "internal_error", "完整终验发生内部错误或返回不完整。", failed=True)
            if statuses["content-fidelity"] not in {"passed", "degraded"} or statuses["structure"] != "passed":
                return halt(state, "protected_content_changed", "内容或结构终验未通过，已回滚整批。", failed=True)
            if statuses["compile"] == "passed" and statuses["format"] in {"passed", "degraded"}:
                if any(item.get("severity") == "block" for item in result.get("issues", [])):
                    return halt(state, "internal_error", "终验通过状态与阻断问题相矛盾。", failed=True)
                state.status = RunStatus.READY
                state.stop_reason = ""
                state.detail = "目标已完成并通过完整终验，等待外层交付。"
                return output(state)
            blocking = [item for item in result.get("issues", []) if item.get("severity") == "block"]
            if blocking and all(item.get("repairable") for item in blocking):
                state.gate_snapshots["current"] = result
                reset_goals = set()
                for goal in state.goals:
                    # 内容门禁已证明正文替换完成；后续编译或格式修复不能重复替换原文。
                    if goal.get('kind') != 'body_replace':
                        goal["status"] = "pending"
                        goal.pop('completed_unit_ids', None)
                        reset_goals.add(goal['goal_id'])
                state.protections = [item for item in state.protections
                                     if not reset_goals.intersection(item.get('goal_ids', []))]
                state.status = RunStatus.RUNNING
                return output(state, "prepare")
            return halt(state, "final_validation_failed", "完整终验未通过且不能继续自动修复。")
        except Exception as exc:
            return halt(state, "internal_error", f"完整终验异常：{type(exc).__name__}", failed=True)

    def wait_for_user(value: QualityGraphState) -> dict:
        state = AgentState.from_dict(value["agent"])
        answer = interrupt({"run_id": state.run_id, "detail": state.detail, "stop_reason": state.stop_reason})
        if not isinstance(answer, dict) or answer.get("continue") is not True:
            return halt(state, "user_stopped", "用户停止了本次运行。")
        if not validate_parent(state, *runtime.parent_identity()):
            return halt(state, state.stop_reason, state.detail)
        if state.mode == "recognition_review" and answer.get("structure_confirmed") is True:
            # 标题、引用和对象决定由结构事务校验；图恢复不再次询问模型。
            state.status = RunStatus.READY
            state.stop_reason = ""
            state.detail = "用户已完成集中结构确认。"
            return output(state)
        state.status = RunStatus.RUNNING
        state.stop_reason = ""
        return output(state, "prepare")

    graph = StateGraph(QualityGraphState)
    for name, function in {"prepare": prepare, "plan": plan, "reserve_action": reserve_action, "execute": execute,
                           "begin_validation": begin_validation, "validate": validate, "wait": wait_for_user}.items():
        graph.add_node(name, function)
        graph.add_conditional_edges(name, lambda value: value["next_node"], {
            "prepare": "prepare", "plan": "plan", "execute": "execute", "validate": "validate",
            "reserve_action": "reserve_action", "begin_validation": "begin_validation",
            "wait": "wait", "end": END,
        })
    graph.add_edge(START, "prepare")
    return graph.compile(checkpointer=checkpointer)
