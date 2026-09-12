# -*- coding: utf-8 -*-
"""统一 Agent 的固定通用工具、受限读取与受保护补丁调度。"""

from __future__ import annotations




##### 通用工具 Schema 板块 #####

def _schema(properties=None, required=()):
    return {"type": "object", "properties": properties or {}, "required": list(required), "additionalProperties": False}


_UNIT = {"type": "string", "minLength": 1, "maxLength": 160}
_STRING = {"type": "string", "maxLength": 24000}
_SOURCE = {"unit_id": _UNIT, "offset": {"type": "integer", "minimum": 0},
           "limit": {"type": "integer", "minimum": 1, "maximum": 16000}}
TOOL_DEFINITIONS = [
    {"name": "read_quality_report", "description": "读取当前统一质量问题摘要。", "input_schema": _schema()},
    {"name": "read_compile_diagnostics", "description": "读取当前编译诊断摘要。", "input_schema": _schema()},
    {"name": "read_template_context", "description": "读取固定模板能力和允许的格式上下文。", "input_schema": _schema()},
    {"name": "locate_content_unit", "description": "读取已有内容单元的来源证据。", "input_schema": _schema({"unit_id": _UNIT}, ["unit_id"])},
    {"name": "read_source", "description": "读取已定位生成单元的有限源码及当前文件哈希。", "input_schema": _schema(_SOURCE, ["unit_id"])},
    {"name": "search_source", "description": "在允许的单元中作普通文字搜索，返回有限上下文。",
     "input_schema": _schema({"query": {"type": "string", "minLength": 1, "maxLength": 200}}, ["query"])},
    {"name": "apply_protected_patch", "description": "仅修改已确认目标的排版，保留论文文字及所有语义对象。",
     "input_schema": _schema({"unit_id": _UNIT, "expected_sha256": {"type": "string", "minLength": 64, "maxLength": 64},
         "edits": {"type": "array", "minItems": 1, "maxItems": 5,
                   "items": _schema({"old_text": _STRING, "new_text": _STRING}, ["old_text", "new_text"])}},
         ["unit_id", "expected_sha256", "edits"])},
    {"name": "replace_confirmed_text", "description": "执行用户已确认的完整普通正文替换；不能提供临时新文本。",
     "input_schema": _schema({"unit_id": _UNIT, "goal_id": _UNIT,
                              "expected_sha256": {"type": "string", "minLength": 64, "maxLength": 64}},
                             ["unit_id", "goal_id", "expected_sha256"])},
]


##### 受限读取与执行板块 #####

class QualityTools:
    """不持有模型、编译器或发布器，只消费外层提供的可信证据和写入区域。"""

    def __init__(self, *, root, generation_id, regions, evidence, template_context,
                 journal=None, quality_report=None, compile_diagnostics=""):
        from copy import deepcopy
        self.root = root
        self.generation_id = generation_id
        self.regions = {item.unit_id: item for item in regions}
        if len(self.regions) != len(regions):
            raise ValueError("可写区域的内容单元编号不能重复。")
        self.evidence = {item["unit_id"]: deepcopy(item) for item in evidence}
        self.template_context = deepcopy(template_context)
        self.quality_report = deepcopy(quality_report or {})
        self.compile_diagnostics = compile_diagnostics
        self.journal = journal

    def definitions(self, mode):
        from copy import deepcopy
        from .runtime import permitted_tools
        return [deepcopy(item) for item in TOOL_DEFINITIONS if item["name"] in permitted_tools(mode)]

    def _source(self, unit_id):
        import hashlib
        from .patching import writable_path, region_bounds
        region = self.regions[unit_id]
        path = writable_path(self.root, region.relative_path, self.generation_id)
        original = path.read_bytes()
        source = original.decode("utf-8")
        start, end = region_bounds(source, region)
        return {"unit_id": unit_id, "relative_path": region.relative_path,
                "role": region.role, "text": source[start:end], "sha256": hashlib.sha256(original).hexdigest()}

    def input_hash(self, action, state):
        import hashlib
        import json
        unit_id = action["arguments"].get("unit_id")
        if unit_id in self.regions:
            return self._source(unit_id)["sha256"]
        payload = {"evidence": self.evidence, "template": self.template_context, "quality": self.quality_report,
                   "compile": self.compile_diagnostics}
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    def execute(self, action, state):
        from copy import deepcopy
        from .patching import PatchSafetyError, prepare_patch, prepare_confirmed_text
        from .planner import _validate_arguments
        definitions = {item["name"]: item for item in self.definitions(state.mode)}
        try:
            name = action["tool"]
            if name not in definitions:
                raise PatchSafetyError("当前模式不允许该工具。")
            args = action["arguments"]
            _validate_arguments(args, definitions[name]["input_schema"])
            if name == "read_template_context":
                data = deepcopy(self.template_context)
            elif name == "read_quality_report":
                data = deepcopy(self.quality_report)
            elif name == "read_compile_diagnostics":
                data = {"diagnostics": self.compile_diagnostics[-12000:]}
            elif name == "locate_content_unit":
                item = self.evidence[args["unit_id"]]
                data = {key: deepcopy(item[key]) for key in ("unit_id", "text", "role", "evidence", "requires_review") if key in item}
                if "text" in data:
                    data["text"] = data["text"][:16000]
            elif name == "read_source":
                data = self._source(args["unit_id"])
                offset, limit = args.get("offset", 0), args.get("limit", 16000)
                data.update({"total_length": len(data["text"]), "text": data["text"][offset:offset+limit], "offset": offset})
            elif name == "search_source":
                matches = []
                for identifier in self.regions:
                    item = self._source(identifier)
                    position = item["text"].find(args["query"])
                    if position >= 0:
                        matches.append({"unit_id": identifier, "relative_path": item["relative_path"],
                                        "text": item["text"][max(0, position-100):position+300]})
                    if len(matches) == 20:
                        break
                data = {"matches": matches}
            else:
                unit_id = args["unit_id"]
                goals = [goal for goal in state.goals if goal.get("confirmed") is True and unit_id in goal.get("target_unit_ids", [])]
                if not goals or self.journal is None:
                    raise PatchSafetyError("写入目标没有已确认目标或私有动作日志。")
                recovered = self.journal.recover(action)
                if recovered is not None:
                    return {"status": "ok", "detail": "已核对并恢复同一动作。", **recovered}
                region = self.regions[unit_id]
                common = dict(generation_id=self.generation_id, region=region, expected_sha256=args["expected_sha256"])
                if name == "replace_confirmed_text":
                    goal = next((item for item in goals if item["goal_id"] == args["goal_id"]), None)
                    if goal is None:
                        raise PatchSafetyError("没有匹配的已确认正文替换目标。")
                    prepared = prepare_confirmed_text(self.root, goal=goal, **common)
                else:
                    if not any(goal.get("kind") != "body_replace" for goal in goals):
                        raise PatchSafetyError("正文替换目标不允许模型另拟格式动作。")
                    prepared = prepare_patch(self.root, edits=args["edits"], **common)
                    if (r'\begin{longtable}' in prepared['new_fragment']
                        and self.template_context.get('capabilities', {}).get('cross_page_tables') != 'supported'):
                        raise PatchSafetyError('当前模板未声明支持跨页表格反馈。')
                result = self.journal.apply(action["action_id"], prepared, proposal=action)
                return {"status": "ok", "detail": "已在确认区域应用补丁，等待确定性检查。", **result}
            return {"status": "ok", "changed_files": [], "data": data}
        except (PatchSafetyError, KeyError, ValueError) as exc:
            return {"status": "rejected", "changed_files": [],
                    "detail": str(exc) if isinstance(exc, PatchSafetyError) else "目标或参数不符合当前工具契约。"}
