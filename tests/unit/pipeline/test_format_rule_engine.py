# -*- coding: utf-8 -*-
"""G5-A 规则状态、模板画像和发布处置测试。"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from tests.support.paths import GRADUATE_TEMPLATE, STAGE1_DIR


##### 测试环境板块 #####


from pipeline.checker.format_checker import FormatChecker
from pipeline.checker.rule_catalog import build_active_rules
from pipeline.checker.rule_models import summarize_rule_results
from pipeline.text_processing import escape_cell, escape_text
from pipeline_api import PipelineRunner
from run_pipeline import _quality_exit_code


COMMON_RULES = STAGE1_DIR / "rules" / "common-format-rules.json"
OUC_PROFILE = STAGE1_DIR / "rules" / "templates" / "ouc-format-profile.json"
BACHELOR_PROFILE = (
    STAGE1_DIR / "rules" / "templates" / "ouc-bachelor-common-only-profile.json"
)


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _write_a4_pdf(path: Path) -> None:
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=595.276, height=841.89)
    with path.open("wb") as stream:
        writer.write(stream)


##### 状态聚合板块 #####


class RuleSummaryTests(unittest.TestCase):
    def test_unimplemented_blocker_cannot_pass(self):
        summary = summarize_rule_results(
            {
                "X": {
                    "status": "not_implemented",
                    "disposition": "block",
                    "category": "测试",
                    "detail": "没有检测器",
                }
            }
        )
        self.assertEqual(summary.quality_status, "blocked")
        self.assertFalse(summary.publish_allowed)
        self.assertFalse(summary.all_passed)

    def test_degradation_is_publishable_but_not_passed(self):
        summary = summarize_rule_results(
            {
                "X": {
                    "status": "not_implemented",
                    "disposition": "degrade",
                    "category": "测试",
                    "detail": "需要人工复核",
                }
            }
        )
        self.assertEqual(summary.quality_status, "degraded")
        self.assertTrue(summary.publish_allowed)
        self.assertFalse(summary.all_passed)

    def test_cli_exit_codes_distinguish_pass_degrade_and_block(self):
        self.assertEqual(_quality_exit_code(True, "passed"), 0)
        self.assertEqual(_quality_exit_code(True, "degraded"), 2)
        self.assertEqual(_quality_exit_code(False, "blocked"), 1)


##### 模板画像板块 #####


class TemplateProfileTests(unittest.TestCase):
    def test_ouc_full_spec_enters_coverage_matrix(self):
        metadata, rules = build_active_rules(COMMON_RULES, OUC_PROFILE, baseline=True)
        ouc_rules = [rule for rule in rules if rule.layer == "template"]
        self.assertGreaterEqual(len(ouc_rules), 100)
        self.assertEqual(metadata["template_id"], "ouc-graduate")
        self.assertIn("OUC-1.1", {rule.rule_id for rule in ouc_rules})
        self.assertIn("OUC-10.9", {rule.rule_id for rule in ouc_rules})
        self.assertIn("OUC-PDF-A4", {rule.rule_id for rule in ouc_rules})

    def test_custom_template_does_not_inherit_ouc_rules(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            common = root / "common.json"
            profile = root / "profile.json"
            _write_json(common, {"rules": []})
            _write_json(
                profile,
                {
                    "template_id": "demo",
                    "rules": [
                        {
                            "rule_id": "DEMO-TITLE",
                            "category": "标题",
                            "item": "示例模板标题命令",
                            "layer": "template",
                            "phase": "source",
                            "disposition": "block",
                            "detector": "require_regex",
                            "config": {"paths": ["main.tex"], "patterns": ["demoTitle"]},
                        }
                    ],
                },
            )
            (root / "main.tex").write_text("\\demoTitle{论文}", encoding="utf-8")
            checker = FormatChecker(common, root, profile, baseline=True)
            results = checker.run_all()
        self.assertEqual(set(results), {"DEMO-TITLE"})
        self.assertEqual(results["DEMO-TITLE"]["status"], "pass")

    def test_bachelor_only_degrades_recorded_font_substitution(self):
        _, graduate_rules = build_active_rules(COMMON_RULES, OUC_PROFILE)
        _, bachelor_rules = build_active_rules(COMMON_RULES, BACHELOR_PROFILE)
        graduate = {item.rule_id: item for item in graduate_rules}
        bachelor = {item.rule_id: item for item in bachelor_rules}
        self.assertEqual(
            graduate["COMMON-LOG-FONT-SUBSTITUTION"].disposition, "block"
        )
        self.assertEqual(
            bachelor["COMMON-LOG-FONT-SUBSTITUTION"].disposition, "degrade"
        )

    def test_common_override_rejects_unknown_rule(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            profile = root / "profile.json"
            _write_json(profile, {
                "template_id": "demo",
                "common_overrides": {"COMMON-NOT-EXISTS": {"disposition": "degrade"}},
            })
            with self.assertRaisesRegex(ValueError, "未知通用规则"):
                build_active_rules(COMMON_RULES, profile)


##### 故障注入板块 #####


class RuleExecutionTests(unittest.TestCase):
    def _ouc_workspace(self, root: Path) -> None:
        shutil.copytree(GRADUATE_TEMPLATE, root, dirs_exist_ok=True)
        main_path = root / "main.tex"
        main_path.write_text(
            main_path.read_text(encoding="utf-8").replace("待生成论文标题", "测试论文标题"),
            encoding="utf-8",
        )
        (root / "data" / "cover.tex").write_text("测试论文封面\n", encoding="utf-8")
        (root / "contents").mkdir(exist_ok=True)
        (root / "contents" / "section_01.tex").write_text(
            "\\chapter{测试}\n正文。\n", encoding="utf-8"
        )
        (root / "data" / "abstract_en.tex").write_text(
            "\\begin{enabstract}\nText.\\par\n"
            "\\noindent\\textbf{Key Words: }one; two; three\n"
            "\\end{enabstract}\n",
            encoding="utf-8",
        )
        (root / "data" / "abstract_ch.tex").write_text(
            "\\begin{abstract}\n正文。\\par\n"
            "\\noindent\\textbf{关键词：}甲；乙；丙\n"
            "\\end{abstract}\n",
            encoding="utf-8",
        )
        (root / "main.log").write_text("Output written on main.pdf.\n", encoding="utf-8")
        _write_a4_pdf(root / "main.pdf")

    def test_unimplemented_rules_are_explicit_degradation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._ouc_workspace(root)
            checker = FormatChecker(COMMON_RULES, root, OUC_PROFILE, baseline=True)
            results = checker.run_all()
            matrix = checker.coverage_matrix()
        self.assertEqual(results["OUC-1.1"]["status"], "not_implemented")
        self.assertEqual(matrix["quality_status"], "degraded")
        self.assertTrue(matrix["publish_allowed"])
        self.assertFalse(matrix["all_passed"])

    def test_placeholder_blocks_and_overfull_degrades(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._ouc_workspace(root)
            (root / "contents" / "section_01.tex").write_text(
                "正文 {{UNKNOWN_FIELD}}", encoding="utf-8"
            )
            (root / "main.log").write_text(
                "Overfull \\hbox (3.0pt too wide) in paragraph\n", encoding="utf-8"
            )
            checker = FormatChecker(COMMON_RULES, root, OUC_PROFILE)
            results = checker.run_all()
            matrix = checker.coverage_matrix()
        self.assertEqual(results["COMMON-PLACEHOLDER"]["status"], "fail")
        self.assertEqual(results["COMMON-LOG-OVERFULL"]["status"], "fail")
        self.assertEqual(results["COMMON-LOG-OVERFULL"]["disposition"], "degrade")
        self.assertEqual(matrix["quality_status"], "blocked")

    def test_bachelor_known_overfull_baseline_does_not_degrade(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "main.tex").write_text("正文。", encoding="utf-8")
            (root / "main.log").write_text(
                "Overfull \\hbox (37.27312pt too wide) in paragraph at lines 15--15\n"
                "Overfull \\hbox (16.90999pt too wide) in paragraph at lines 16--16\n",
                encoding="utf-8",
            )
            _write_a4_pdf(root / "main.pdf")
            checker = FormatChecker(COMMON_RULES, root, BACHELOR_PROFILE)
            results = checker.run_all()
        outcome = results["COMMON-LOG-OVERFULL"]
        self.assertEqual(outcome["status"], "pass")
        self.assertTrue(all(
            item["origin"] == "template_baseline" for item in outcome["evidence"]
        ))

    def test_bachelor_new_overfull_remains_a_current_run_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "main.tex").write_text("正文。", encoding="utf-8")
            (root / "main.log").write_text(
                "Overfull \\hbox (37.27312pt too wide) in paragraph at lines 15--15\n"
                "Overfull \\hbox (8.0pt too wide) in paragraph at lines 30--30\n",
                encoding="utf-8",
            )
            _write_a4_pdf(root / "main.pdf")
            results = FormatChecker(COMMON_RULES, root, BACHELOR_PROFILE).run_all()
        outcome = results["COMMON-LOG-OVERFULL"]
        self.assertEqual(outcome["status"], "fail")
        self.assertEqual(
            [item["origin"] for item in outcome["evidence"]],
            ["template_baseline", "current_run"],
        )

    def test_ouc_named_placeholder_blocks(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._ouc_workspace(root)
            main_path = root / "main.tex"
            main_path.write_text(
                main_path.read_text(encoding="utf-8").replace("测试论文标题", "待生成论文标题"),
                encoding="utf-8",
            )
            results = FormatChecker(COMMON_RULES, root, OUC_PROFILE).run_all()
        self.assertEqual(results["OUC-TEMPLATE-PLACEHOLDER"]["status"], "fail")

    def test_english_toc_detector_rejects_chinese_copy_and_accepts_english(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._ouc_workspace(root)
            section = root / "contents" / "section_01.tex"
            section.write_text(
                "\\chapter{研究方法}\n\\enchapter{研究方法}\n",
                encoding="utf-8",
            )
            failed = FormatChecker(COMMON_RULES, root, OUC_PROFILE).run_all()
            section.write_text(
                "\\chapter{研究方法}\n\\enchapter{Research Methods}\n",
                encoding="utf-8",
            )
            passed = FormatChecker(COMMON_RULES, root, OUC_PROFILE).run_all()
        self.assertEqual(failed["OUC-TOC-ENGLISH-HEADINGS"]["status"], "fail")
        self.assertEqual(passed["OUC-TOC-ENGLISH-HEADINGS"]["status"], "pass")

    def test_pipeline_api_writes_coverage_matrix(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._ouc_workspace(root)
            result = PipelineRunner().check_format(str(root))
            matrix_path = root / "reports" / "format_coverage_matrix.json"
            matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
        self.assertEqual(result.quality_status, "degraded")
        self.assertTrue(result.publish_allowed)
        self.assertFalse(result.all_passed)
        self.assertEqual(matrix["template"]["template_id"], "ouc-graduate")
        self.assertEqual(matrix["counts"], result.counts)

    def test_unicode_roman_numerals_are_renderable_ascii(self):
        self.assertEqual(escape_text("Ⅱ期和Ⅲ期"), "II期和III期")
        self.assertEqual(escape_cell("Ⅰ/Ⅳ"), "I/IV")

    def test_citation_checker_pairs_math_delimiters_without_cross_paragraph_false_positive(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._ouc_workspace(root)
            section = root / "contents" / "section_01.tex"
            section.write_text(
                "变量$x$用于正文。后续文字\\cite{a}。\n"
                "错误公式$y+\\cite{b}$。\n",
                encoding="utf-8",
            )
            results = FormatChecker(COMMON_RULES, root, OUC_PROFILE, baseline=True).run_all()
        citation_result = results["COMMON-CITE-MATH"]
        self.assertEqual(citation_result["status"], "fail")
        self.assertEqual(len(citation_result["evidence"]), 1)

    def test_bibliography_rule_is_not_applicable_without_citation_demand(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "main.tex").write_text("正文。", encoding="utf-8")
            (root / "main.log").write_text("Output written on main.pdf.\n", encoding="utf-8")
            _write_a4_pdf(root / "main.pdf")
            results = FormatChecker(COMMON_RULES, root, BACHELOR_PROFILE, baseline=True).run_all()
        self.assertEqual(results["COMMON-BIB-INSTITUTION"]["status"], "not_applicable")

    def test_bibliography_rule_blocks_citation_without_backend(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "main.tex").write_text(r"正文\cite{missing}。", encoding="utf-8")
            (root / "main.log").write_text("Output written on main.pdf.\n", encoding="utf-8")
            _write_a4_pdf(root / "main.pdf")
            results = FormatChecker(COMMON_RULES, root, BACHELOR_PROFILE, baseline=True).run_all()
        self.assertEqual(results["COMMON-BIB-INSTITUTION"]["status"], "fail")

    def test_ouc_bibliography_style_only_applies_to_bibtex_backend(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "main.tex").write_text("正文。", encoding="utf-8")
            absent = FormatChecker(COMMON_RULES, root, OUC_PROFILE, baseline=True).run_all()

            (root / "data").mkdir()
            (root / "data" / "references.tex").write_text(
                "参考文献原始列表。", encoding="utf-8"
            )
            (root / "main.tex").write_text(
                r"\input{data/references.tex}", encoding="utf-8"
            )
            word_list = FormatChecker(COMMON_RULES, root, OUC_PROFILE, baseline=True).run_all()

            (root / "main.tex").write_text(
                r"\bibliography{cite}", encoding="utf-8"
            )
            missing_style = FormatChecker(COMMON_RULES, root, OUC_PROFILE, baseline=True).run_all()

        self.assertEqual(absent["OUC-10.9"]["status"], "not_applicable")
        self.assertEqual(word_list["OUC-10.9"]["status"], "not_applicable")
        self.assertEqual(missing_style["OUC-10.9"]["status"], "fail")

    def test_plain_url_stops_before_chinese_parenthesis_and_text(self):
        source = "平台（https://www.cbioportal.org）中对应的研究（Pan-Cancer Atlas）。"
        self.assertEqual(
            escape_text(source),
            r"平台（\url{https://www.cbioportal.org}）中对应的研究（Pan-Cancer Atlas）。",
        )
        self.assertEqual(
            escape_text("已有https://example.com/a?q=1。"),
            r"已有\url{https://example.com/a?q=1}。",
        )


if __name__ == "__main__":
    unittest.main()
