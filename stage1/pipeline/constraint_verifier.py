# -*- coding: utf-8 -*-
"""论文级约束的白名单终验；未知检查器不得被当作自动通过。"""

from __future__ import annotations

import re
import json
from pathlib import Path
from typing import Any

from pipeline.source_content_check import strip_latex_comments


##### 安全来源板块 #####

UNIT_BLOCK = re.compile(
    r"% SCHOLAR_UNIT_BEGIN (?P<unit_id>\S+) (?P<role>\S+)\n"
    r"(?P<body>.*?)% SCHOLAR_UNIT_END (?P=unit_id)",
    re.DOTALL,
)


def _safe_tex_path(root: Path, relative_path: str) -> Path:
    path = (root / relative_path).resolve()
    if root not in path.parents or path.suffix.lower() != ".tex" or not path.is_file():
        raise ValueError("约束检查目标不在候选 TeX 文件白名单中。")
    return path


def _unit_text(source: str, unit_id: str) -> str:
    matches = [
        item.group("body") for item in UNIT_BLOCK.finditer(source)
        if item.group("unit_id") == unit_id
    ]
    if len(matches) != 1:
        raise ValueError("内容单元约束必须唯一命中渲染追踪标记。")
    return strip_latex_comments(matches[0])


def _confirmed_fragment(root: Path, verifier: dict, unit_id: str) -> str:
    """动作片段复用父版本已校验追踪；持久约束只保存动作与单元引用。"""
    path = root / 'reports' / 'render_trace.json'
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError('保护追踪不能经过符号链接。')
    try:
        trace = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ValueError('保护追踪不可读取。') from exc
    changes = trace.get('approved_changes') if isinstance(trace, dict) else None
    if not verifier.get('action_id') or not isinstance(changes, list) or any(
        not isinstance(item, dict) for item in changes
    ):
        raise ValueError('保护追踪缺少有效动作。')
    matches = [item for item in changes if item.get('action_id') == verifier['action_id']]
    if (len(matches) != 1 or matches[0].get('unit_id') != unit_id
        or matches[0].get('relative_path') != verifier['relative_path']
        or matches[0].get('kind') not in {'format', 'body_replace'}
        or not isinstance(matches[0].get('new_fragment'), str)
        or not matches[0]['new_fragment'].strip()):
        raise ValueError('保护动作没有唯一且一致的已确认来源。')
    return strip_latex_comments(matches[0]['new_fragment']).strip()


##### 已确认约束验证板块 #####


def verify_paper_constraints(
    root: Path, constraints: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """执行确定性检查；人工复核项明确降级，失败项阻断候选发布。"""
    resolved_root = root.resolve()
    results: list[dict[str, Any]] = []
    for constraint in constraints:
        verifier = dict(constraint.get("verifier", {}))
        mode = str(verifier.get("mode", "human_review"))
        constraint_id = str(constraint.get("constraint_id", "candidate"))
        if mode == "human_review":
            results.append({
                "constraint_id": constraint_id, "status": "manual_review",
                "passed": None, "detail": "该约束没有可靠自动检查器，等待用户查看 PDF。",
            })
            continue
        checker = verifier.get('checker')
        if mode != "deterministic" or checker not in {'tex_unit_contains', 'confirmed_action'}:
            raise ValueError("论文约束声明了未注册的确定性检查器。")
        relative_path = str(verifier.get("relative_path", ""))
        needle = str(verifier.get("needle", ""))
        unit_id = str(constraint.get("scope_ref") or verifier.get("unit_id") or "")
        if not relative_path or not unit_id:
            raise ValueError('约束缺少目标文件或内容单元。')
        source = _safe_tex_path(resolved_root, relative_path).read_text(encoding="utf-8")
        actual = _unit_text(source, unit_id)
        if checker == 'tex_unit_contains':
            if not needle or len(needle) > 500:
                raise ValueError("tex_unit_contains 检查器参数不完整或超限。")
            passed = needle in actual
        else:
            passed = actual.strip() == _confirmed_fragment(resolved_root, verifier, unit_id)
        result = {
            "constraint_id": constraint_id,
            "status": "passed" if passed else "failed", "passed": passed,
            "detail": "目标内容单元保持约束片段。" if passed else "目标内容单元不再满足已确认约束。",
        }
        results.append(result)
        if not passed:
            raise RuntimeError(f"论文级约束终验失败：{constraint_id}")
    return results
