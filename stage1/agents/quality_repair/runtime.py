# -*- coding: utf-8 -*-
"""固定模式权限与运行依赖；路径、模型和门禁函数不写入 checkpoint。"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
from pathlib import Path
from typing import Callable

from .models import AgentState
from .planner import PlanningClient


##### 固定权限板块 #####

MAX_PLANNING_ROUNDS = 3
READ_TOOLS = frozenset({
    "read_quality_report", "read_compile_diagnostics", "search_source",
    "read_source", "locate_content_unit", "read_template_context",
})
MODE_TOOLS = {
    "recognition_review": READ_TOOLS,
    "initial_generation": READ_TOOLS | {"apply_protected_patch"},
    "user_feedback": READ_TOOLS | {"apply_protected_patch", "replace_confirmed_text"},
}


def permitted_tools(mode: str, requested: set[str] | None = None) -> frozenset[str]:
    """调用方只能收窄固定权限，不能授权编译、发布或模板写入工具。"""
    if mode not in MODE_TOOLS:
        raise ValueError("未知 Agent 模式。")
    allowed = MODE_TOOLS[mode]
    return frozenset(allowed if requested is None else allowed.intersection(requested))


def reserve_planning_round(state: AgentState) -> bool:
    """在独立图节点预留并持久化轮次，网络失败也不能归还次数。"""
    if state.planning_round >= MAX_PLANNING_ROUNDS:
        state.stop("planning_limit", "三轮规划后仍未完成，已停止本批处理。")
        return False
    state.planning_round += 1
    return True


def validate_parent(state: AgentState, generation_id: str | None, source_sha256: str) -> bool:
    """恢复只能继续原可信父版本；发生变化时终止原运行。"""
    if (state.parent_generation_id, state.parent_source_sha256) != (generation_id, source_sha256):
        state.stop("parent_changed", "原可信父版本已经变化，请基于当前版本重新提交反馈。")
        return False
    return True


def action_signature(tool: str, targets: list[str], arguments: dict, input_hash: str) -> str:
    """精确动作签名只去重相同输入，允许同一工具处理后续的新问题。"""
    payload = json.dumps({"tool": tool, "targets": targets, "arguments": arguments,
                          "input_hash": input_hash}, sort_keys=True, ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


##### 非持久化依赖板块 #####


def locate_initial_compile_target(root: Path, *, generation_id: str, diagnostics: str, records: list[dict]):
    """文件行号必须落在唯一已有内容单元内；不给模型猜测文件或发送整篇源码。"""
    from pathlib import PurePosixPath
    from .patching import WriteRegion, region_bounds, writable_path

    targets = {}
    locations = list(re.finditer(r'(?m)^(?:\./)?([^\r\n:]+\.tex):(\d+):\s*([^\r\n]+)', diagnostics))
    for location in locations:
        relative = PurePosixPath(location[1].replace('\\', '/'))
        if relative.is_absolute() or '..' in relative.parts:
            return None
        known = re.search(
            r"Extra \}|Too many \}'s\.|Missing \} inserted|Missing \$ inserted|"
            r'\\begin\{(?:center|figure|table|itemize|enumerate)\}'
            r'(?: on input line \d+)? ended by \\end\{[A-Za-z]+\}', location[3],
        )
        matched = [record for record in records if record.get('target_file') == relative.as_posix()]
        if known is None or not matched:
            return None
        path = writable_path(root, relative.as_posix(), generation_id)
        original = path.read_bytes()
        source = original.decode('utf-8')
        lines = source.splitlines(keepends=True)
        number = int(location[2])
        if not 1 <= number <= len(lines):
            return None
        offset = sum(len(line) for line in lines[:number - 1])
        found = []
        for record in matched:
            region = WriteRegion(relative.as_posix(), record['marker_unit_id'], record['role'])
            start, end = region_bounds(source, region)
            if start <= offset < end:
                begin = max(start, offset - 2000)
                found.append({'unit_id': region.unit_id, 'role': region.role,
                    'relative_path': region.relative_path, 'sha256': hashlib.sha256(original).hexdigest(),
                    'text': source[begin:min(end, offset + 2000)], 'offset': begin - start,
                    'diagnostics': known.group()})
        if len(found) != 1:
            return None
        targets[found[0]['unit_id']] = found[0]
    return next(iter(targets.values())) if len(targets) == 1 else None


def initial_syntax_action(root: Path, *, generation_id: str, context: dict | None):
    """仅补齐单元内唯一、最外层的已知居中环境，不猜测分组或嵌套结构。"""
    from .patching import WriteRegion, region_bounds, writable_path
    from pipeline.source_content_check import strip_latex_comments

    if not context or not context['diagnostics'].startswith(r'\begin{center}'):
        return None
    path = writable_path(root, context['relative_path'], generation_id)
    original = path.read_bytes()
    source = original.decode('utf-8')
    start, end = region_bounds(source, WriteRegion(context['relative_path'], context['unit_id'], context['role']))
    fragment = source[start:end]
    visible = strip_latex_comments(fragment).strip()
    if (len(fragment) > 4000 or not visible.startswith(r'\begin{center}')
        or re.findall(r'\\(?:begin|end)\{[^{}]+\}', visible) != [r'\begin{center}']):
        return None
    newline = '\r\n' if '\r\n' in fragment else '\n'
    updated = fragment + ('' if fragment.endswith('\n') else newline) + r'\end{center}' + newline
    return {'kind': 'action', 'tool': 'apply_protected_patch', 'arguments': {
        'unit_id': context['unit_id'], 'expected_sha256': hashlib.sha256(original).hexdigest(),
        'edits': [{'old_text': fragment, 'new_text': updated}]}}


@dataclass(frozen=True, slots=True)
class AuthorizedPlanningModel:
    """三种模式共用客户端绑定；授权只属于当前具体任务，不继承父版本。"""

    client: object
    authorization: object
    task: str

    def invoke_tools(self, *, system: str, user: str, tools: list[dict]) -> dict:
        return self.client.complete_with_tools(system, user, tools,
            authorization=self.authorization, task=self.task)


@dataclass(slots=True)
class RepairRuntime:
    """唯一图的执行依赖；终验与发布属于确定性外层，不暴露为工具。"""

    workspace_root: Path | None
    model: PlanningClient | None
    tools: object
    context: Callable[[AgentState], dict]
    final_validate: Callable[[AgentState], dict]
    observe: Callable[[AgentState, dict], dict]
    rollback_all: Callable[[], None]
    parent_identity: Callable[[], tuple[str | None, str]]
    deterministic_action: Callable[[AgentState], dict | None] = lambda state: None
    authorized: Callable[[AgentState], bool] = lambda state: False
    journal_root: Path | None = None
    temporary_protections: list[dict] = field(default_factory=list)
    rollback_action: Callable[[dict], None] = lambda action: None

    def claim_planning_call(self, state: AgentState) -> None:
        """进程中断后不重发结果未知的请求；调用占用记录不保存提示词。"""
        if self.journal_root is None:
            return
        if not re.fullmatch(r"[A-Za-z0-9_-]+", state.run_id):
            raise ValueError("运行编号不能用于安全的调用日志路径。")
        directory = self.journal_root.resolve() / state.run_id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"planning-{state.planning_round}.json"
        try:
            with path.open("x", encoding="utf-8") as stream:
                json.dump({"run_id": state.run_id, "planning_round": state.planning_round,
                           "status": "requested"}, stream)
        except FileExistsError as exc:
            raise RuntimeError("该轮模型请求已有记录，禁止在恢复时隐式重发。") from exc
