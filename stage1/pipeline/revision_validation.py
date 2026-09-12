# -*- coding: utf-8 -*-
"""从同一持久化抽取与冻结结构建立反馈修订的完整终验，不重新识别 Word。"""

from copy import deepcopy
import json
from pathlib import Path

from content_extraction.persistence import load_extraction
from pipeline.confirmed_structure import apply_confirmed_structure
from pipeline.content_fidelity_gate import _parse_marked_files
from pipeline.results import RenderResult
from pipeline.validation_runner import run_validation_gates
from template_registry.registry import resolve_builtin_template


##### 冻结来源板块 #####

def build_revision_validator(*, root: Path, project_dir: Path, snapshot: dict,
                             source_sha256: str, structure_revision: int, template_id: str,
                             journal, confirmed_goals=()):
    """外层先校验父产物与快照；这里仅加载缓存和已经继承的完整渲染追踪。"""
    from pipeline_api import PipelineRunner
    extracted = load_extraction(project_dir, source_sha256=source_sha256)
    recognized = apply_confirmed_structure(extracted, None, snapshot,
        source_sha256=source_sha256, structure_revision=structure_revision)
    resolved = resolve_builtin_template(template_id)
    runner = PipelineRunner(resolved_template=resolved)
    trace = json.loads((root / "reports/render_trace.json").read_text(encoding="utf-8"))
    files = [f'{resolved.adapter.content_directory}/section_{chapter["num"]:02d}.tex'
             for chapter in recognized.review["chapters"]]
    before, _, _ = _parse_marked_files(root, files)
    goals = deepcopy(list(confirmed_goals))
    body_replacements = deepcopy(trace.get("body_replacements", [])) + [
        {key: deepcopy(goal[key]) for key in ("goal_id", "kind", "confirmed", "target_unit_ids", "replacement")}
        for goal in goals if goal.get("kind") == "body_replace"]

    def validate(candidate_root):
        if Path(candidate_root).resolve() != root.resolve():
            raise ValueError("修订终验目标不是本次私有工作副本。")
        actual, _, _ = _parse_marked_files(root, files)
        changes = deepcopy(trace.get("approved_changes", []))
        changes.extend(_journal_changes(journal.entries(), goals=goals, before=before, actual=actual))
        candidate_trace = {**deepcopy(trace), "approved_changes": changes, "body_replacements": body_replacements}
        rendered = RenderResult(str(root), {}, template_adapter=resolved.adapter,
                                chapter_files=files, render_trace=candidate_trace,
                                confirmed_body_replacements=body_replacements)
        return run_validation_gates(runner, root, extracted, recognized, rendered)

    return validate


##### 动作与用户确认板块 #####

def _journal_changes(entries, *, goals, before, actual):
    """动作日志只证明实际执行；正文修改授权必须来自独立的确认目标。"""
    approved = {goal["goal_id"]: goal for goal in goals if goal.get("confirmed") is True}
    changes, replaced = [], set()
    for entry in entries:
        if entry["status"] == "rolled_back":
            continue
        if entry["status"] != "applied":
            raise ValueError("存在未完成的文件动作，不能进入终验。")
        identifier = entry["unit_id"]
        relative = entry["relative_path"]
        if (identifier not in before or identifier not in actual
            or before[identifier]["target_file"] != relative or actual[identifier]["target_file"] != relative):
            raise ValueError("动作日志的目标或章节文件缺少可信来源。")
        proposal = entry["proposal"]
        args = proposal["arguments"]
        if args.get("unit_id") != identifier or proposal.get("action_id") != entry["action_id"]:
            raise ValueError("动作提案与文件日志不一致。")
        change = {key: deepcopy(entry[key]) for key in
                  ("action_id", "unit_id", "relative_path", "old_fragment", "new_fragment")}
        if proposal["tool"] == "replace_confirmed_text":
            goal = approved.get(args.get("goal_id"))
            if (not goal or goal.get("kind") != "body_replace" or goal["target_unit_ids"] != [identifier]
                or goal.get("replacement") != entry.get("confirmed_replacement") or goal["goal_id"] in replaced):
                raise ValueError("正文动作不符合本批用户确认的唯一替换。")
            replaced.add(goal["goal_id"])
            change.update(kind="body_replace", goal_id=goal["goal_id"])
        elif proposal["tool"] == "apply_protected_patch":
            if not any(goal.get("kind") == "format" and identifier in goal["target_unit_ids"] for goal in approved.values()):
                raise ValueError("格式动作没有对应的已确认目标。")
            change["kind"] = "format"
        else:
            raise ValueError("文件日志包含未允许的修改工具。")
        changes.append(change)
    expected_replacements = {key for key, goal in approved.items() if goal.get("kind") == "body_replace"}
    if replaced != expected_replacements:
        raise ValueError("本批已确认正文替换尚未全部执行。")
    return changes
