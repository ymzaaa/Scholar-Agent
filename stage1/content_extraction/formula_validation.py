# -*- coding: utf-8 -*-
"""确定性扫描用户输入的 LaTeX 美元公式，并生成编译前阻断契约。"""

from __future__ import annotations

from typing import Any


##### 扫描辅助板块 #####


SENTENCE_BOUNDARIES = {"。", "！", "？", "；", "\n", "\r"}


def _escaped(text: str, index: int) -> bool:
    backslashes = 0
    cursor = index - 1
    while cursor >= 0 and text[cursor] == "\\":
        backslashes += 1
        cursor -= 1
    return backslashes % 2 == 1


def _dollar_run(text: str, index: int) -> int:
    cursor = index
    while cursor < len(text) and text[cursor] == "$":
        cursor += 1
    return cursor - index


def _unbalanced_braces(body: str) -> bool:
    depth = 0
    for index, character in enumerate(body):
        if _escaped(body, index):
            continue
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth < 0:
                return True
    return depth != 0


def _boundary_after(text: str, start: int) -> int:
    for index in range(start, len(text)):
        if text[index] in SENTENCE_BOUNDARIES:
            return index
    return len(text)


##### 公式片段板块 #####


def scan_latex_formula_fragments(text: str) -> list[dict[str, Any]]:
    """按原始偏移返回合法公式和异常候选，不修改或补全用户内容。"""
    fragments: list[dict[str, Any]] = []
    cursor = 0
    while cursor < len(text):
        if text[cursor] != "$" or _escaped(text, cursor):
            cursor += 1
            continue
        opener_run = _dollar_run(text, cursor)
        opener_length = min(opener_run, 2)
        if opener_run > 2:
            end = cursor + opener_run
            fragments.append({
                "status": "malformed", "start": cursor, "end": end,
                "raw_text": text[cursor:end], "delimiter": "$" * opener_length,
                "error_code": "unexpected_delimiter_run",
            })
            cursor = end
            continue

        search = cursor + opener_length
        boundary = _boundary_after(text, search)
        closer_index = None
        closer_length = None
        while search < boundary:
            if text[search] == "$" and not _escaped(text, search):
                closer_index = search
                closer_length = _dollar_run(text, search)
                break
            search += 1

        delimiter = "$" * opener_length
        if closer_index is None:
            end = boundary
            fragments.append({
                "status": "malformed", "start": cursor, "end": end,
                "raw_text": text[cursor:end], "delimiter": delimiter,
                "error_code": "unclosed_delimiter",
            })
            cursor = max(end, cursor + opener_length)
            continue

        end = closer_index + int(closer_length)
        raw_text = text[cursor:end]
        if closer_length != opener_length:
            fragments.append({
                "status": "malformed", "start": cursor, "end": end,
                "raw_text": raw_text, "delimiter": delimiter,
                "closing_delimiter": "$" * int(closer_length),
                "error_code": "mismatched_delimiter",
            })
            cursor = end
            continue

        body = text[cursor + opener_length:closer_index]
        if _unbalanced_braces(body):
            fragments.append({
                "status": "malformed", "start": cursor, "end": end,
                "raw_text": raw_text, "delimiter": delimiter,
                "error_code": "unbalanced_braces",
            })
        else:
            fragments.append({
                "status": "valid", "start": cursor, "end": end,
                "raw_text": raw_text, "delimiter": delimiter, "body": body,
            })
        cursor = end
    return fragments


##### 预检报告板块 #####


ERROR_MESSAGES = {
    "unclosed_delimiter": "公式定界符未闭合。",
    "mismatched_delimiter": "公式起止定界符的美元符号数量不一致。",
    "unbalanced_braces": "公式花括号未配对。",
    "unexpected_delimiter_run": "检测到不支持的连续美元符号。",
}


class FormulaPreflightBlocked(RuntimeError):
    """异常公式候选存在时，禁止进入模板渲染和编译。"""

    def __init__(self, review: dict[str, Any]):
        super().__init__(
            f"公式预检未通过：{review.get('malformed_formula_count', 0)} 个异常公式候选或报告缺失。"
        )
        self.review = review


def ensure_formula_preflight(review: dict[str, Any] | None) -> None:
    """只有明确通过的预检报告允许进入渲染。"""
    if not isinstance(review, dict) or review.get("status") != "passed":
        raise FormulaPreflightBlocked(review if isinstance(review, dict) else {})


def build_formula_review(content_units: list[dict[str, Any]]) -> dict[str, Any]:
    """汇总模板无关的公式预检结果；异常候选属于发布阻断项。"""
    formulas = [
        unit for unit in content_units if unit.get("unit_type") == "formula"
    ]
    malformed = [
        unit for unit in formulas
        if unit.get("status") != "extracted"
    ]
    candidates = []
    for unit in malformed:
        payload = unit.get("payload", {})
        code = payload.get("error_code", "unknown_formula_error")
        candidates.append({
            "unit_id": unit["unit_id"],
            "parent_unit_id": unit.get("relations", {}).get("parent_unit_id"),
            "raw_text": payload.get("raw_text", unit.get("text", "")),
            "error_code": code,
            "message": ERROR_MESSAGES.get(code, "公式语法无法安全解析。"),
            "source": unit.get("source", {}),
        })
    valid = [
        unit for unit in formulas
        if unit.get("status") == "extracted"
        and unit.get("payload", {}).get("source_syntax") in {"latex", "omml"}
    ]
    return {
        "schema_version": "1.0.0",
        "status": "blocked" if candidates else "passed",
        "blocking": bool(candidates),
        "valid_formula_count": len(valid),
        "malformed_formula_count": len(candidates),
        "candidates": candidates,
        "policy": "preserve_raw_text_and_block_before_render",
    }
