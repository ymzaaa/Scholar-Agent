# -*- coding: utf-8 -*-
"""从高度可变的 Word 封面提取通用论文元数据。"""

from __future__ import annotations

import re

from pipeline.metadata_resolution import PLACEHOLDER


##### 标签解析板块 #####


_LABEL_ALIASES = {
    "分类号": "classification", "udc": "udc", "学号": "student_id",
    "学生学号": "student_id", "作者": "author", "学生姓名": "author",
    "姓名": "author", "指导教师": "advisor", "导师": "advisor",
    "学位类型": "degree_type", "申请学位类型": "degree_type",
    "专业名称": "major", "专业": "major", "研究方向": "research_direction",
    "完成日期": "completion_date", "日期": "completion_date",
    "论文题目": "title", "题目": "title", "中文题目": "title",
    "英文题目": "title_en", "论文英文题目": "title_en",
}

_IGNORED_LABELS = {"学校代码", "合作导师", "授予学位单位"}
_ALL_LABELS = sorted(
    {*_LABEL_ALIASES, *_IGNORED_LABELS}, key=len, reverse=True,
)
_LABEL_PATTERN = re.compile(
    "(" + "|".join(
        r"\s*".join(re.escape(character) for character in label)
        for label in _ALL_LABELS
    ) + r")\s*[：:]",
    re.IGNORECASE,
)


def _normalize_label(value: str) -> str:
    return re.sub(r"\s+", "", value).lower()


def _usable_value(value: str) -> bool:
    compact = re.sub(r"[\s：:·.]+", "", value)
    return bool(compact) and compact not in {"年月", "年月日"}


def _parse_labeled_line(text: str) -> dict[str, str]:
    matches = list(_LABEL_PATTERN.finditer(text))
    result: dict[str, str] = {}
    for index, match in enumerate(matches):
        label = _normalize_label(match.group(1))
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        value = text[match.end():end].strip()
        field = _LABEL_ALIASES.get(label)
        if field and _usable_value(value):
            result.setdefault(field, value)
    return result


##### 元数据汇总板块 #####


def resolve_cover_metadata(paragraphs: list[dict], tables: list[dict]) -> dict[str, str]:
    """优先使用显式标签；无标签题目只采用保守的封面前区候选。"""
    values: dict[str, str] = {}
    cover_table = tables[0].get("data", []) if tables else []
    for row in cover_table:
        cells = [str(cell).strip() for cell in row]
        if not cells:
            continue
        label = _normalize_label(cells[0].rstrip("：:"))
        field = _LABEL_ALIASES.get(label)
        candidates = [cell for cell in cells[1:] if _usable_value(cell)]
        if field and candidates:
            values.setdefault(field, candidates[-1])

    texts = [str(item.get("text", "")).strip() for item in paragraphs]
    for text in texts[:20]:
        for field, value in _parse_labeled_line(text).items():
            values.setdefault(field, value)

    if "title" not in values:
        for item in paragraphs[:20]:
            text = str(item.get("text", "")).strip()
            style = str(item.get("style", "")).replace(" ", "").lower()
            if text and style == "title":
                values["title"] = text
                break
    if "title" not in values:
        values["title"] = next((
            text for text in texts[:15]
            if len(text) > 10 and not re.match(r"^[A-Za-z]", text)
            and not _LABEL_PATTERN.search(text)
        ), PLACEHOLDER)
    if "title_en" not in values:
        values["title_en"] = next((
            text for text in texts[:15]
            if len(text) > 20 and re.match(r"^[A-Z]", text)
            and not _LABEL_PATTERN.search(text)
            and len(re.findall(r"[A-Za-z]", text)) >= len(text.replace(" ", "")) // 2
        ), PLACEHOLDER)

    fields = (
        "classification", "udc", "student_id", "title", "title_en",
        "author", "advisor", "degree_type", "major", "research_direction",
        "completion_date",
    )
    return {field: values.get(field, PLACEHOLDER) for field in fields}
