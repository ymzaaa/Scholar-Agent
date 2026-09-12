# -*- coding: utf-8 -*-
"""中文学位论文标题规则与内容单元模式版本测试。"""

import json

from pipeline.structure_recognizer import infer_heading
from tests.support.paths import STAGE1_DIR


##### 标题规则板块 #####


def test_common_chinese_thesis_heading_variants() -> None:
    cases = [
        ("1 绪论", "chapter"),
        ("第1章 绪论", "chapter"),
        ("第一章 绪论", "chapter"),
        ("一、绪论", "chapter"),
        ("1. 绪论", "chapter"),
        ("1.1研究背景", "section"),
        ("1.1.1 研究对象", "subsection"),
    ]
    for text, expected in cases:
        assert infer_heading(text)["level"] == expected


def test_style_heading_and_false_positive() -> None:
    assert infer_heading("绪论", style="Heading 1")["level"] == "chapter"
    assert infer_heading("2024 年度研究结果") is None
    assert infer_heading("第一章：绪论。本章介绍研究背景。") is None
    assert infer_heading("2.3节介绍上一节的相关工作。") is None


##### 模式版本板块 #####


def test_content_unit_schema_version_is_explicit() -> None:
    schema_path = STAGE1_DIR / "content_extraction" / "content_units.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert schema["properties"]["schema_version"]["const"] == "1.7.0"
