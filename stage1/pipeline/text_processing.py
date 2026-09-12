# -*- coding: utf-8 -*-
"""普通文字的统一转义与完整网址边界；公式由独立语义 token 渲染。"""

import re
from urllib.parse import quote


##### 字符等价板块 #####


UNICODE_MATH = {
    "±": r"\pm",
    "×": r"\times",
    "√": r"\surd",
    "✓": r"\checkmark",
    "✗": r"\times",
    "≥": r"\geq",
    "≤": r"\leq",
    "∈": r"\in",
    "→": r"\rightarrow",
    "←": r"\leftarrow",
    "≈": r"\approx",
    "≠": r"\neq",
    "²": "^{2}",
    "³": "^{3}",
    "°": r"^{\circ}",
    "α": r"\alpha",
    "β": r"\beta",
    "γ": r"\gamma",
    "δ": r"\delta",
    "μ": r"\mu",
    "σ": r"\sigma",
    "π": r"\pi",
    "λ": r"\lambda",
    "θ": r"\theta",
    "τ": r"\tau",
    "ρ": r"\rho",
    "∞": r"\infty",
    "∑": r"\sum",
    "∇": r"\nabla",
    "∂": r"\partial",
    "ℓ": r"\ell",
    "ℝ": r"\mathbb{R}",
}
UNICODE_TEXT = {
    "—": "---",
    "–": "--",
    "～": "--",
    "％": r"\%",
    "•": r"\textbullet{}",
    "…": r"\dots{}",
    # 西文字体通常不包含 Unicode 罗马数字，等价转为可稳定渲染的 ASCII 组合。
    "Ⅰ": "I",
    "Ⅱ": "II",
    "Ⅲ": "III",
    "Ⅳ": "IV",
    "Ⅴ": "V",
    "Ⅵ": "VI",
    "Ⅶ": "VII",
    "Ⅷ": "VIII",
    "Ⅸ": "IX",
    "Ⅹ": "X",
    "Ⅺ": "XI",
    "Ⅻ": "XII",
    "\xa0": " ",
    "​": "",
}

_PLAIN_URL_PATTERN = re.compile(
    r"(?<!\\url\{)(https?://[A-Za-z0-9._~:/?#@!$&*+,;=%\-\[\]()]{1,})"
)


##### 文本转义板块 #####


_SPECIAL_CHARACTERS = {
    character: "\\" + character for character in "#$%&_{}"
} | {
    "~": r"\textasciitilde{}", "^": r"\textasciicircum{}",
    "\\": r"\textbackslash{}",
}


def _escape_characters(text: str) -> str:
    # 单次转换避免再次转义刚生成的命令，不推测普通文字中的数学环境。
    return "".join(
        _SPECIAL_CHARACTERS.get(character,
            UNICODE_TEXT.get(character,
                f"${UNICODE_MATH[character]}$" if character in UNICODE_MATH else character))
        for character in text
    )


def escape_text(text: str) -> str:
    """先划定原始网址，再转义普通文字；空白 token 保留词间分隔。"""
    if not text:
        return ""
    if not text.strip():
        return " "
    parts = []
    cursor = 0
    for match in _PLAIN_URL_PATTERN.finditer(text):
        core, trailing = _url_boundary(match.group(1))
        parts.extend([
            _escape_characters(text[cursor:match.start()]),
            rf"\url{{{core}}}", _escape_characters(trailing),
        ])
        cursor = match.end()
    parts.append(_escape_characters(text[cursor:]))
    return "".join(parts)


def escape_cell(text):
    """单元格和正文共用字符规则，换行由语义 token 决定。"""
    return escape_text(str(text))


##### URL边界板块 #####


def hyperlink_argument(url: str) -> str:
    """保留网址已有编码和片段语义，再保护 LaTeX 参数中的控制字符。"""
    encoded = quote(url, safe=":/?#[]@!$&'()*+,;=%-._~")
    return ''.join('\\' + char if char in '%#&' else char for char in encoded)


def _url_boundary(raw: str) -> tuple[str, str]:
    core, trailing = raw, ""
    while core and core[-1] in ".,;:!?":
        trailing = core[-1] + trailing
        core = core[:-1]
    for opening, closing in (("(", ")"), ("[", "]")):
        while core.endswith(closing) and core.count(closing) > core.count(opening):
            trailing = closing + trailing
            core = core[:-1]
    return core, trailing
