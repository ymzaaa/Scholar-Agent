# -*- coding: utf-8 -*-
"""结构审查使用通用只读工具和唯一执行图，不制造降级建议。"""

from copy import deepcopy
import json

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from agents.quality_repair.graph_executor import build_quality_graph
from agents.quality_repair.models import AgentState
from agents.quality_repair.runtime import RepairRuntime
from agents.quality_repair.tools import QualityTools
from recognition.review import _bounded_evidence


##### 有限证据夹具板块 #####

def setup(model=None):
    snapshot = {
        "headings": [{"unit_id": "p3", "text": "候选标题", "level": "body", "suggested_level": "section",
                      "requires_review": True, "source": {"path": "D:/private/source.docx"}, "evidence": ["bold"]}],
        "paragraph_evidence": [{"unit_id": f"p{index}", "text": f"普通段落{index}", "style": "Normal"}
                               for index in range(8)],
    }
    evidence = _bounded_evidence(snapshot)
    tools = QualityTools(root=None, generation_id="", regions=[], evidence=evidence, template_context={})
    state = AgentState(run_id="run", project_id="project", mode="recognition_review",
        template_id="ouc-bachelor", goals=[{"goal_id": "g", "target_unit_ids": ["p3"]}])
    def forbidden(*args):
        raise AssertionError("只读审查不能修改文件或运行交付门禁")
    runtime = RepairRuntime(None, model, tools, lambda state: {"evidence": evidence, "goals": state.goals},
                            forbidden, forbidden, lambda: None, lambda: (None, ""), authorized=lambda state: True)
    return build_quality_graph(runtime=runtime, checkpointer=InMemorySaver()), tools, state, evidence


class Model:
    def __init__(self, responses):
        self.responses, self.calls = responses, 0

    def invoke_tools(self, **kwargs):
        value = self.responses[self.calls]
        self.calls += 1
        return deepcopy(value)


def suggestion(**changes):
    return {"status": "finish", "suggestions": [{"unit_id": "p3", "suggested_level": "section",
            "rationale": "依据已有编号和样式证据。", **changes}]}


##### 只读与输出校验板块 #####

def test_bounded_evidence_excludes_full_document_paths_and_distant_paragraphs():
    graph, tools, state, evidence = setup()
    neighbors = evidence[0]["evidence"][0]["neighbors"]
    assert [item["unit_id"] for item in neighbors] == ["p1", "p2", "p3", "p4", "p5"]
    assert "D:/" not in json.dumps(evidence)
    assert not {"apply_protected_patch", "replace_confirmed_text"} & {item["name"] for item in tools.definitions(state.mode)}
    observation = tools.execute({"tool": "locate_content_unit", "arguments": {"unit_id": "unknown"}}, state)
    assert observation["status"] == "rejected" and observation["changed_files"] == []


def test_shared_graph_reads_evidence_then_waits_for_structure_confirmation():
    model = Model([{"tool_calls": [{"name": "locate_content_unit", "arguments": {"unit_id": "p3"}}]}, suggestion()])
    graph, tools, state, evidence = setup(model)
    config = {"configurable": {"thread_id": state.run_id}}
    result = graph.invoke({"agent": state.to_dict()}, config=config)
    assert result["agent"]["status"] == "waiting_user" and result["__interrupt__"]
    assert result["agent"]["observations"][0]["changed_files"] == []
    resumed = graph.invoke(Command(resume={"continue": True, "structure_confirmed": True}), config=config)
    assert resumed["agent"]["status"] == "ready" and model.calls == 2


def test_unavailable_model_waits_without_fabricated_body_suggestions():
    graph, tools, state, evidence = setup()
    result = graph.invoke({"agent": state.to_dict()}, config={"configurable": {"thread_id": state.run_id}})
    assert result["agent"]["status"] == "waiting_user"
    assert result["agent"]["suggestions"] == [] and result["agent"]["planning_round"] == 0


@pytest.mark.parametrize("changes", [
    {"unit_id": "unknown"}, {"confidence": "high"}, {"confidence": 2},
    {"rationale": ""}, {"evidence": [{"path": "private"}]}, {"new_text": "新增论文"},
])
def test_invalid_model_suggestions_fail_without_fallback_or_confirmation(changes):
    model = Model([suggestion(**changes)])
    graph, tools, state, evidence = setup(model)
    result = graph.invoke({"agent": state.to_dict()}, config={"configurable": {"thread_id": state.run_id}})
    assert result["agent"]["status"] == "failed" and model.calls == 1
    assert result["agent"]["suggestions"] == [] and "__interrupt__" not in result
