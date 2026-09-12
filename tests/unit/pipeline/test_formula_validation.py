# -*- coding: utf-8 -*-
"""美元公式扫描器的合法路径、异常分类和原文边界。"""

from content_extraction.formula_validation import scan_latex_formula_fragments


def test_scanner_preserves_valid_inline_and_display_formulas() -> None:
    fragments = scan_latex_formula_fragments(
        r"行内 $a^2+b^2=c^2$，行间 $$\frac{1}{3}$$。"
    )
    assert [(item["status"], item["delimiter"], item["body"]) for item in fragments] == [
        ("valid", "$", "a^2+b^2=c^2"),
        ("valid", "$$", r"\frac{1}{3}"),
    ]


def test_scanner_classifies_w09_errors_without_repair() -> None:
    examples = {
        "未闭合 $x+y。": ("$x+y", "unclosed_delimiter"),
        "混用 $$x+y$。": ("$$x+y$", "mismatched_delimiter"),
        r"括号 $$\frac{a}{b$$。": (r"$$\frac{a}{b$$", "unbalanced_braces"),
    }
    for text, expected in examples.items():
        fragment = scan_latex_formula_fragments(text)
        assert len(fragment) == 1
        assert (fragment[0]["raw_text"], fragment[0]["error_code"]) == expected
        assert fragment[0]["status"] == "malformed"


def test_scanner_ignores_escaped_currency_dollar() -> None:
    assert scan_latex_formula_fragments(r"预算写作 \$100，不是公式。") == []
