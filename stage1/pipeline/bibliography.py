# -*- coding: utf-8 -*-
"""数字引用、BibTeX键和Word原始参考文献列表的确定性适配。"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

from pipeline.text_processing import escape_text
from content_extraction.citations import (
    NUMERIC_CITATION_PATTERN, expand_numeric_expression,
    detect_word_reference_list, classify_numeric_citation_candidates,
)


def replace_numeric_citations(
    text: str,
    number_to_key: dict[str, str],
) -> tuple[str, list[dict[str, Any]]]:
    resolutions = []

    def replace(match: re.Match) -> str:
        raw = match.group(0)
        try:
            numbers = expand_numeric_expression(match.group("expression"))
        except ValueError as exc:
            resolutions.append({"raw": raw, "numbers": [], "keys": [], "resolved": False, "error": str(exc)})
            return raw
        keys = [number_to_key[number] for number in numbers if number in number_to_key]
        resolved = len(keys) == len(numbers)
        resolutions.append({
            "raw": raw,
            "numbers": numbers,
            "keys": keys,
            "resolved": resolved,
            "missing_numbers": [number for number in numbers if number not in number_to_key],
        })
        return r"\cite{" + ",".join(keys) + "}" if resolved else raw

    return NUMERIC_CITATION_PATTERN.sub(replace, text), resolutions


##### BibTeX读取板块 #####


def _bibtex_entries(content: str) -> list[dict[str, str]]:
    """读取条目边界和键；字段只用于保守的DOI/标题核对，不重写BibTeX。"""
    entries = []
    cursor = 0
    header = re.compile(r"@(\w+)\s*\{\s*([^,\s]+)\s*,", re.MULTILINE)
    while match := header.search(content, cursor):
        depth = 1
        index = match.end()
        while index < len(content) and depth:
            if content[index] == "{":
                depth += 1
            elif content[index] == "}":
                depth -= 1
            index += 1
        raw = content[match.start():index]
        fields = {}
        for name in ("doi", "title", "author", "year"):
            field_match = re.search(
                rf"(?ims)^\s*{name}\s*=\s*(?:\{{([^\n]*?)\}}|\"([^\n]*?)\")\s*,?\s*$",
                raw,
            )
            if field_match:
                fields[name] = next(value for value in field_match.groups() if value is not None).strip()
        entries.append({"type": match.group(1), "key": match.group(2), "raw": raw, **fields})
        cursor = max(index, match.end())
    return entries


def load_bibtex_entries(path: str | Path) -> list[dict[str, str]]:
    return _bibtex_entries(Path(path).read_text(encoding="utf-8-sig"))


##### 数据源选择与匹配板块 #####


def _normalized(value: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", value.lower())


def _metadata_match(word_entry: dict[str, Any], bib_entries: list[dict[str, str]]) -> list[str]:
    raw = word_entry["raw_text"]
    doi_match = re.search(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", raw, re.IGNORECASE)
    if doi_match:
        doi = doi_match.group(0).rstrip(".,").lower()
        return [entry["key"] for entry in bib_entries if entry.get("doi", "").lower() == doi]
    normalized_raw = _normalized(raw)
    return [
        entry["key"] for entry in bib_entries
        if len(_normalized(entry.get("title", ""))) >= 12
        and _normalized(entry["title"]) in normalized_raw
    ]


def build_bibliography_plan(
    paragraphs: list[dict[str, Any]],
    bib_path: str | None,
    explicit_mapping: dict[str, str] | None = None,
    source_mode: str = "auto",
) -> dict[str, Any]:
    """生成编号到稳定键的映射；不确定项保留为失败，不猜测条目顺序。"""
    word = detect_word_reference_list(paragraphs)
    bib_entries = load_bibtex_entries(bib_path) if bib_path else []
    bib_keys = {entry["key"] for entry in bib_entries}
    if source_mode not in {"auto", "bibtex", "word"}:
        raise ValueError(f"未知参考文献来源模式：{source_mode}")
    mode = "bibtex" if (source_mode == "bibtex" or source_mode == "auto" and bib_entries) else "word"
    mapping: dict[str, str] = {}
    methods: dict[str, str] = {}
    issues = []

    if mode == "bibtex":
        if not bib_entries:
            issues.append({"code": "BIBTEX_SOURCE_UNAVAILABLE", "blocking": True})
        for number, key in (explicit_mapping or {}).items():
            normalized_number = str(int(number))
            if key not in bib_keys:
                issues.append({"code": "EXPLICIT_BIB_KEY_MISSING", "number": normalized_number, "key": key, "blocking": True})
            else:
                mapping[normalized_number] = key
                methods[normalized_number] = "explicit_mapping"
        candidates: dict[str, list[str]] = {}
        for entry in bib_entries:
            match = re.match(r"^(\d+)(?:$|[+_-])", entry["key"])
            if match:
                candidates.setdefault(str(int(match.group(1))), []).append(entry["key"])
        for number, keys in candidates.items():
            if number in mapping:
                continue
            if len(keys) == 1:
                mapping[number] = keys[0]
                methods[number] = "numeric_key" if keys[0].isdigit() else "compatibility_numeric_prefix"
            else:
                issues.append({"code": "AMBIGUOUS_NUMERIC_BIB_KEYS", "number": number, "keys": keys, "blocking": True})
        for word_entry in word.get("entries", []):
            number = word_entry["number"]
            if number in mapping:
                continue
            matches = _metadata_match(word_entry, bib_entries)
            if len(matches) == 1:
                mapping[number] = matches[0]
                methods[number] = "word_metadata_to_bibtex"
            elif len(matches) > 1:
                issues.append({"code": "AMBIGUOUS_WORD_BIB_MATCH", "number": number, "keys": matches, "blocking": True})
        if word.get("status") == "recognized" and len(word["entries"]) != len(bib_entries):
            issues.append({
                "code": "WORD_BIB_COUNT_MISMATCH",
                "word_entries": len(word["entries"]),
                "bib_entries": len(bib_entries),
                "blocking": source_mode == "word",
            })
    else:
        if word.get("status") != "recognized":
            issues.append({"code": "WORD_REFERENCE_LIST_UNAVAILABLE", "blocking": True})
        if word.get("duplicate_numbers"):
            issues.append({
                "code": "WORD_REFERENCE_NUMBER_DUPLICATE",
                "numbers": word["duplicate_numbers"],
                "blocking": True,
            })
        for entry in word.get("entries", []):
            mapping[entry["number"]] = f"word-ref-{int(entry['number']):04d}"
            methods[entry["number"]] = "word_reference_order"

    return {
        "report_version": "1.0.0",
        "mode": mode,
        "number_to_key": mapping,
        "methods": methods,
        "bibtex_entry_count": len(bib_entries),
        "word_reference_list": word,
        "issues": issues,
        "all_sources_valid": not any(issue.get("blocking") for issue in issues),
        "current_policy": "产品默认使用 Word 文末列表，BibTeX 仅用于显式兼容调用",
    }


##### 输出文件板块 #####


def write_word_bibliography(output_dir: str | Path, plan: dict[str, Any]) -> str:
    entries = plan["word_reference_list"].get("entries", [])
    width = "9" * max((len(str(int(entry["number"]))) for entry in entries), default=1)
    lines = [rf"\begin{{thebibliography}}{{{width}}}"]
    for entry in entries:
        key = plan["number_to_key"][entry["number"]]
        lines.extend([
            rf"\bibitem[{entry['number']}]{{{key}}}",
            escape_text(entry["raw_text"]),
            "",
        ])
    lines.append(r"\end{thebibliography}")
    path = Path(output_dir) / "data" / "references.tex"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path.relative_to(Path(output_dir)).as_posix()


def prepare_bibliography_output(
    *,
    paragraphs: list[dict[str, Any]],
    content_units: list[dict[str, Any]],
    bib_path: str | None,
    output_dir: str | Path,
    template_adapter: Any,
    explicit_mapping: dict[str, str] | None,
    source_mode: str,
    citation_review: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """完成数据源选择、所需编号核对和模板后端配置。"""
    plan = build_bibliography_plan(
        paragraphs, bib_path, explicit_mapping=explicit_mapping, source_mode=source_mode
    )
    review = citation_review or classify_numeric_citation_candidates(
        paragraphs, content_units, plan["word_reference_list"]
    )
    body_decisions = [
        item for item in review.get("decisions", [])
        if item.get("decision") == "body_citation"
    ]
    required = sorted({
        number
        for item in body_decisions
        for number in item.get("numbers", [])
    }, key=int)
    word_list_missing = (
        plan.get("mode") == "word"
        and plan.get("word_reference_list", {}).get("status") == "not_found"
    )
    if word_list_missing and not required:
        # Word 没有正文引用也没有文末列表是合法输入，不应为了用户选择的
        # “优先读取 Word 列表”制造空数据源阻断。
        plan["mode"] = "none"
        plan["issues"] = [
            item for item in plan.get("issues", [])
            if item.get("code") != "WORD_REFERENCE_LIST_UNAVAILABLE"
        ]
        plan["all_sources_valid"] = True
    review_required = [
        item["unit_id"] for item in review.get("decisions", [])
        if item.get("decision") == "needs_review"
    ]
    if review_required:
        plan["issues"].append({
            "code": "CITATION_REVIEW_REQUIRED",
            "unit_ids": review_required,
            "blocking": True,
        })
        plan["all_sources_valid"] = False
    plan["required_numbers"] = required
    plan["unresolved_numbers"] = [
        number for number in required if number not in plan["number_to_key"]
    ]
    plan["all_citations_resolved"] = (
        plan["all_sources_valid"] and not plan["unresolved_numbers"]
    )
    plan["citation_review"] = review
    plan["body_citation_unit_ids"] = [item["unit_id"] for item in body_decisions]
    # 引用转换是一个事务；失败时保留全部正文方括号原文，禁止部分转换。
    plan["render_number_to_key"] = (
        dict(plan["number_to_key"]) if plan["all_citations_resolved"] else {}
    )
    plan["transaction_status"] = (
        "not_applicable" if plan["mode"] == "none" and not required
        else "committed" if plan["all_citations_resolved"] else "rolled_back"
    )
    output = Path(output_dir)
    if plan["mode"] == "bibtex":
        if bib_path and Path(bib_path).is_file():
            shutil.copy2(bib_path, output / "cite.bib")
    else:
        write_word_bibliography(output, plan)
    template_adapter.update_bibliography(
        output / template_adapter.entrypoint, plan["mode"],
    )
    return plan


def load_explicit_mapping(path: str | Path | None) -> dict[str, str]:
    if not path:
        return {}
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not all(str(key).isdigit() and isinstance(value, str) for key, value in data.items()):
        raise ValueError("引用映射必须是编号到BibTeX键的JSON对象。")
    return {str(int(key)): value for key, value in data.items()}
