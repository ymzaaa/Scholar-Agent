"""文档级标题证据、后置区域及图表方向规则。"""

import pytest

from pipeline.structure_recognizer import (
    recognize_regions, recognize_heading_candidates, chapters_from_headings,
    recognize_object_bindings,
)


##### 标题证据板块 #####


def paragraphs(*texts, styles=None):
    return [{"unit_id": f"u-{i:06d}", "index": i, "block_index": i,
             "text": text, "style": styles[i] if styles else "Normal"}
            for i, text in enumerate(texts)]


def candidates(items):
    regions = recognize_regions(items, [])
    return recognize_heading_candidates(items, regions=regions)


def test_one_style_cannot_assign_multiple_levels():
    items = paragraphs("第一章 测试", "1.1 节", "1.1.1 小节", styles=["Heading 1"] * 3)
    result = candidates(items)
    assert [item["level"] for item in result] == ["chapter", "section", "subsection"]
    assert all("mixed_level_style" in item["evidence"] for item in result)
    assert not any(item["requires_review"] for item in result)


def test_reliable_style_conflicts_with_text_number():
    items = paragraphs("第一章 测试", "1.1 节", styles=["Heading 1", "Heading 3"])
    item = candidates(items)[1]
    assert item["level"] == "body" and item["suggested_level"] == "section"
    assert item["requires_review"] and "heading_evidence_conflict" in item["evidence"]


def test_automatic_numbering_conflicts_with_style():
    items = paragraphs("第一章 测试", "自动编号标题", styles=["Normal", "Heading 3"])
    items[1].update(numbering_id="7", numbering_level=1)
    result = candidates(items)[1]
    assert result["requires_review"] and result["suggested_level"] == "section"
    assert {"paragraph_style", "word_numbering"}.issubset(result["evidence"])


def test_source_number_order_conflict_is_reviewed_but_gaps_are_allowed():
    items = paragraphs("第三章 测试", "第七章 测试", "第二章 测试")
    result = candidates(items)
    assert [item["requires_review"] for item in result] == [False, False, True]
    regions = recognize_regions(items, [])
    chapters = chapters_from_headings(result, regions)
    assert [item["num"] for item in chapters] == [1, 2]
    assert [item["source_number"] for item in chapters] == ["3", "7"]


@pytest.mark.parametrize("titles", [
    ("1.3 先出现的节", "1.1 后出现的节"),
    ("1.3 先出现的节", "1.3 重复编号的节"),
    ("1.1 节", "1.1.3 先出现的小节", "1.1.2 后出现的小节"),
])
def test_section_number_order_conflicts_require_review(titles):
    result = candidates(paragraphs("第一章 测试", *titles))
    assert result[-1]["requires_review"]
    assert result[-1]["level"] == "body"
    assert "source_number_order_conflict" in result[-1]["evidence"]


@pytest.mark.parametrize("tail", ["参考文献", "致谢", "附录", "研究成果", "第二章 参考文献", "附录 A 数据说明"])
def test_heading_one_back_matter_is_not_a_chapter(tail):
    items = paragraphs("第一章 测试", "正文。", tail, "后置原文。", styles=["Heading 1"] * 4)
    regions = recognize_regions(items, [])
    result = recognize_heading_candidates(items, regions=regions)
    assert regions["back_matter_start"] == 2
    assert [item["text"] for item in result] == ["第一章 测试"]
    assert len(items) == 4


def test_contents_and_sentences_are_not_headings():
    items = paragraphs("第一章 目录缓存", "第一章 测试", "1.1 本段是一个完整句子。")
    items[0]["generated_contents"] = True
    assert [item["text"] for item in candidates(items)] == ["第一章 测试"]


def test_semantic_empty_paragraph_remains_inside_body():
    items = paragraphs("第一章 测试", "正文。", "")
    for kind in ("footnote", "field", "hyperlink", "text_box"):
        regions = recognize_regions(items, [{"unit_type": kind, "source": {"paragraph_index": 2}}])
        assert regions["body_range"] == [0, 3]


def test_only_bold_is_a_review_candidate_not_an_automatic_heading():
    items = paragraphs("第一章 测试", "无编号短标题", "普通正文。")
    items[1]["presentation"] = {"bold_ratio": 1.0}
    result = candidates(items)[1]
    assert result["level"] == "body" and result["requires_review"]


def test_blank_before_trailing_table_keeps_table_inside_body():
    items = paragraphs("第一章 测试", "", "")
    items[-1]["block_index"] = 3
    table = {"unit_type": "table", "source": {"block_index": 2}}
    regions = recognize_regions(items, [table])
    assert regions["body_range"] == [0, 2]
    assert items[regions["body_range"][1]]["block_index"] > table["source"]["block_index"]


def test_centered_formula_object_is_not_a_weak_heading():
    items = paragraphs("第一章 测试", "$x+y$")
    items[1]["layout"] = {"alignment": {"value": "CENTER"}}
    units = [{"unit_id": items[1]["unit_id"], "unit_type": "paragraph", "payload": {"inline_tokens": [{"kind": "formula", "unit_id": "formula"}]}}]
    result = recognize_heading_candidates(items, content_units=units)
    assert [item["text"] for item in result] == ["第一章 测试"]


def test_explicit_heading_is_not_limited_by_weak_candidate_length():
    text = "第一章 " + "长标题" * 20
    result = candidates(paragraphs(text))
    assert result[0]["level"] == "chapter" and not result[0]["requires_review"]


##### 对象关系板块 #####


def unit(index, kind, text="", role=None):
    return {"unit_id": f"u-{index:06d}", "unit_type": kind, "text": text,
            "source": {"block_index": index}, "properties": {"semantic_role": role}}


def test_unique_table_across_empty_paragraph_is_bound():
    units = [unit(0, "paragraph", "表1—1：数据"), unit(1, "paragraph"), unit(2, "table", role="data")]
    result = recognize_object_bindings(units)
    assert result[0]["status"] == "bound"
    assert result[0]["object_unit_ids"] == ["u-000002"]
    assert "confidence" not in result[0]


def test_multiple_adjacent_tables_require_confirmation():
    units = [unit(0, "paragraph", "表1-1 数据"), unit(1, "table", role="data"), unit(2, "table", role="data")]
    result = recognize_object_bindings(units)[0]
    assert result["status"] == "needs_review"
    assert result["object_unit_ids"] == []
    assert len(result["candidates"]) == 2


def test_body_text_blocks_distant_caption_binding():
    units = [unit(0, "paragraph", "表1-1 数据"), unit(1, "paragraph", "中间正文。"), unit(2, "table", role="data")]
    assert recognize_object_bindings(units)[0]["status"] == "unbound"


def test_generated_caption_list_is_excluded():
    caption = unit(0, "paragraph", "表1-1 数据")
    caption["properties"]["generated_contents"] = True
    assert recognize_object_bindings([caption, unit(1, "table", role="data")]) == []
