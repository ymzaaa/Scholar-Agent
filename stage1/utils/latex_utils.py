# -*- coding: utf-8 -*-
"""
utils/latex_utils.py —— LaTeX 语法相关工具

包含：
- escape_latex_special_chars : 正文段落的保留字符转义（公式内容不经过此函数）
- validate_latex_formula_syntax : 公式的基础合法性校验（括号/定界符/环境配平）
"""
from __future__ import annotations

import re

# 转义顺序很重要：必须先处理反斜杠，否则会把后续转义产物再转义一遍
_ESCAPE_ORDER = [
    ("\\", r"\textbackslash{}"),
    ("&", r"\&"),
    ("%", r"\%"),
    ("$", r"\$"),
    ("#", r"\#"),
    ("_", r"\_"),
    ("{", r"\{"),
    ("}", r"\}"),
    ("~", r"\textasciitilde{}"),
    ("^", r"\textasciicircum{}"),
]


def escape_latex_special_chars(text: str) -> str:
    """转义 LaTeX 保留字符。仅用于正文/标题/图注等纯文本，公式内容不得调用。"""
    for char, repl in _ESCAPE_ORDER:
        text = text.replace(char, repl)
    return text


# --------------------------------------------------------------------------- #
def validate_latex_formula_syntax(latex: str) -> tuple[bool, str]:
    """
    对公式做基础合法性校验，返回 (是否通过, 失败原因)。

    校验项（对应文件二 C3 规格中的"括号闭合、常见符号合法性"）：
      1. 花括号 {} 配平（忽略被 \\ 转义的 \\{ \\}）
      2. $ 定界符出现次数为偶数
      3. \\left 与 \\right 数量一致
      4. \\begin{env} 与 \\end{env} 逐环境配平
    """
    body = latex

    # 1. 花括号配平（先移除 \{ \} 转义形式再统计）
    stripped = body.replace(r"\{", "").replace(r"\}", "")
    depth = 0
    for ch in stripped:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth < 0:
                return False, "花括号闭合顺序错误（出现了多余的 '}'）"
    if depth != 0:
        return False, f"花括号未配平（差 {depth} 个 '}}'）"

    # 2. $ 定界符（移除 \$ 转义后统计）
    dollar_count = len(re.findall(r"(?<!\\)\$", body))
    if dollar_count % 2 != 0:
        return False, "$ 定界符数量为奇数，公式未闭合"

    # 3. \left / \right 配平
    n_left = len(re.findall(r"\\left\b", body))
    n_right = len(re.findall(r"\\right\b", body))
    if n_left != n_right:
        return False, f"\\left({n_left}) 与 \\right({n_right}) 数量不一致"

    # 4. \begin / \end 环境配平
    begins = re.findall(r"\\begin\{([^{}]+)\}", body)
    ends = re.findall(r"\\end\{([^{}]+)\}", body)
    if sorted(begins) != sorted(ends):
        return False, f"环境未配平：begin={begins} end={ends}"

    return True, ""
