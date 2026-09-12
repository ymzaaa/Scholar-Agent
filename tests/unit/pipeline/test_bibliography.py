# -*- coding: utf-8 -*-
"""G3-B1引用解析、BibTeX映射和Word原始列表降级测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.support.paths import STAGE1_DIR


##### 测试环境板块 #####


from pipeline.bibliography import (build_bibliography_plan, load_explicit_mapping, prepare_bibliography_output, replace_numeric_citations)
from content_extraction.citations import (classify_numeric_citation_candidates, expand_numeric_expression)


def _paragraphs_with_references() -> list[dict]:
    paragraphs = [
        {"index": index, "text": f"正文段落{index}", "style": "Normal"}
        for index in range(8)
    ]
    paragraphs.extend([
        {"index": 8, "text": "参考文献", "style": "Heading 1"},
        {"index": 9, "text": "张三. 示例研究[J]. 示例期刊, 2023.", "style": "List Paragraph"},
        {"index": 10, "text": "Li A. Another study[J]. Journal, 2024.", "style": "List Paragraph"},
    ])
    return paragraphs


##### 引用语法板块 #####


class NumericCitationTests(unittest.TestCase):
    def test_ranges_and_chinese_separators_expand(self):
        self.assertEqual(
            expand_numeric_expression("1，3-5；7"),
            ["1", "3", "4", "5", "7"],
        )
        rendered, resolutions = replace_numeric_citations(
            "已有研究[1，3-4]。", {"1": "a", "3": "c", "4": "d"}
        )
        self.assertEqual(rendered, r"已有研究\cite{a,c,d}。")
        self.assertTrue(resolutions[0]["resolved"])

    def test_invalid_or_missing_range_does_not_fake_success(self):
        with self.assertRaises(ValueError):
            expand_numeric_expression("5-2")
        rendered, resolutions = replace_numeric_citations("结果[1-3]", {"1": "a", "3": "c"})
        self.assertEqual(rendered, "结果[1-3]")
        self.assertEqual(resolutions[0]["missing_numbers"], ["2"])
        interval, interval_resolutions = replace_numeric_citations("归一化到[0,1]", {"1": "a"})
        self.assertEqual(interval, "归一化到[0,1]")
        self.assertEqual(interval_resolutions, [])

    def test_candidates_are_classified_before_rendering(self):
        paragraphs = [
            {"index": 0, "text": "已有研究给出结果[1]。", "style": "Normal"},
            {"index": 1, "text": "数组索引示例为[1,2]。", "style": "Normal"},
            *[
                {"index": index, "text": f"填充正文{index}", "style": "Normal"}
                for index in range(2, 7)
            ],
            {"index": 7, "text": "参考文献", "style": "Heading 1"},
            {"index": 8, "text": "[1] Zhang A. Study[J]. Journal, 2024.", "style": "List Paragraph"},
            {"index": 9, "text": "[2] Li B. Study[J]. Journal, 2023.", "style": "List Paragraph"},
        ]
        units = [
            {
                "unit_id": "cite-body", "unit_type": "citation", "text": "[1]",
                "source": {"paragraph_index": 0, "start_offset": 8, "end_offset": 11},
                "payload": {"numbers": ["1"]},
            },
            {
                "unit_id": "not-cite", "unit_type": "citation", "text": "[1,2]",
                "source": {"paragraph_index": 1, "start_offset": 7, "end_offset": 12},
                "payload": {"numbers": ["1", "2"]},
            },
            {
                "unit_id": "ref-label", "unit_type": "citation", "text": "[1]",
                "source": {"paragraph_index": 8, "start_offset": 0, "end_offset": 3},
                "payload": {"numbers": ["1"]},
            },
        ]
        review = classify_numeric_citation_candidates(paragraphs, units)
        self.assertEqual(
            [item["decision"] for item in review["decisions"]],
            ["body_citation", "non_citation", "reference_label"],
        )

    def test_unresolved_number_rolls_back_all_body_citations(self):
        paragraphs = _paragraphs_with_references()
        units = [{
            "unit_id": "cite-1-3", "unit_type": "citation", "text": "[1-3]",
            "source": {"paragraph_index": 1, "start_offset": 4, "end_offset": 9},
            "payload": {"numbers": ["1", "2", "3"]},
        }]
        review = {
            "decisions": [{
                "unit_id": "cite-1-3", "decision": "body_citation",
                "numbers": ["1", "2", "3"],
            }]
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            adapter = type("Adapter", (), {
                "entrypoint": "paper.tex",
                "update_bibliography": lambda *_args: None,
            })()
            plan = prepare_bibliography_output(
                paragraphs=paragraphs, content_units=units, bib_path=None,
                output_dir=root, template_adapter=adapter,
                explicit_mapping=None, source_mode="word", citation_review=review,
            )
        self.assertEqual(plan["unresolved_numbers"], ["3"])
        self.assertEqual(plan["transaction_status"], "rolled_back")
        self.assertEqual(plan["render_number_to_key"], {})


##### 数据源匹配板块 #####


class BibliographyPlanTests(unittest.TestCase):
    def test_general_bibtex_keys_use_explicit_mapping(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            bib = root / "library.bib"
            bib.write_text(
                "@article{Bray2024,\n title={Global cancer statistics},\n year={2024}\n}\n"
                "@article{Liu2021,\n title={示例研究},\n year={2021}\n}\n",
                encoding="utf-8",
            )
            mapping_path = root / "mapping.json"
            mapping_path.write_text('{"1":"Bray2024","2":"Liu2021"}', encoding="utf-8")
            plan = build_bibliography_plan(
                _paragraphs_with_references(),
                str(bib),
                load_explicit_mapping(mapping_path),
            )
        self.assertEqual(plan["number_to_key"], {"1": "Bray2024", "2": "Liu2021"})
        self.assertEqual(plan["methods"]["1"], "explicit_mapping")
        self.assertTrue(plan["all_sources_valid"])

    def test_duplicate_numeric_prefix_is_blocking(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            bib = Path(temp_dir) / "duplicate.bib"
            bib.write_text(
                "@article{1+first, title={A}}\n@article{1_second, title={B}}\n",
                encoding="utf-8",
            )
            plan = build_bibliography_plan([], str(bib))
        self.assertFalse(plan["all_sources_valid"])
        self.assertIn("AMBIGUOUS_NUMERIC_BIB_KEYS", [item["code"] for item in plan["issues"]])

    def test_word_list_falls_back_to_stable_bibitems(self):
        plan = build_bibliography_plan(_paragraphs_with_references(), None)
        self.assertEqual(plan["mode"], "word")
        self.assertEqual(
            plan["number_to_key"],
            {"1": "word-ref-0001", "2": "word-ref-0002"},
        )
        self.assertTrue(plan["all_sources_valid"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
