# -*- coding: utf-8 -*-
"""
content_extraction/omml.py —— OMML (Office Math Markup Language) → LaTeX 转换器

设计原则：
- 自包含实现，不依赖外部转换服务；覆盖论文中最常见的公式结构：
  分式 m:f、上下标 m:sSup/m:sSub/m:sSubSup、根式 m:rad、大型运算符 m:nary、
  定界符 m:d、矩阵 m:m、函数 m:func、上下极限 m:limLow/m:limUpp、
  重音 m:acc、上下横线 m:bar、组合符号 m:groupChr、公式组 m:eqArr、前置上下标 m:sPre 等。
- 遇到无法识别的节点【立即抛出 OmmlParseError】而不是产出可能错误的结果——
  记录转换失败并在模板渲染前阻断，不输出猜测的公式。
"""
from __future__ import annotations

from lxml import etree

M_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _m(tag: str) -> str:
    return f"{{{M_NS}}}{tag}"


class OmmlParseError(Exception):
    """OMML 结构无法被本转换器识别。"""


# 常见 Unicode 数学符号 → LaTeX 命令映射（可持续扩充）
UNICODE_MAP = {
    "×": r"\times ", "÷": r"\div ", "±": r"\pm ", "∓": r"\mp ",
    "≤": r"\le ", "≥": r"\ge ", "≠": r"\ne ", "≈": r"\approx ", "≡": r"\equiv ",
    "∞": r"\infty ", "∂": r"\partial ", "∇": r"\nabla ", "√": r"\surd ",
    "∑": r"\sum ", "∏": r"\prod ", "∫": r"\int ", "∈": r"\in ", "∉": r"\notin ",
    "⊂": r"\subset ", "⊆": r"\subseteq ", "∪": r"\cup ", "∩": r"\cap ",
    "→": r"\rightarrow ", "←": r"\leftarrow ", "⇒": r"\Rightarrow ", "⇔": r"\Leftrightarrow ",
    "⋅": r"\cdot ", "·": r"\cdot ", "…": r"\ldots ", "⋯": r"\cdots ",
    "′": r"'", "″": r"''", "°": r"^{\circ}",
    # 希腊字母
    "α": r"\alpha ", "β": r"\beta ", "γ": r"\gamma ", "δ": r"\delta ",
    "ε": r"\varepsilon ", "ζ": r"\zeta ", "η": r"\eta ", "θ": r"\theta ",
    "ι": r"\iota ", "κ": r"\kappa ", "λ": r"\lambda ", "μ": r"\mu ",
    "ν": r"\nu ", "ξ": r"\xi ", "π": r"\pi ", "ρ": r"\rho ",
    "σ": r"\sigma ", "τ": r"\tau ", "υ": r"\upsilon ", "φ": r"\varphi ",
    "χ": r"\chi ", "ψ": r"\psi ", "ω": r"\omega ",
    "Γ": r"\Gamma ", "Δ": r"\Delta ", "Θ": r"\Theta ", "Λ": r"\Lambda ",
    "Ξ": r"\Xi ", "Π": r"\Pi ", "Σ": r"\Sigma ", "Φ": r"\Phi ",
    "Ψ": r"\Psi ", "Ω": r"\Omega ",
}

# nary（大型运算符）字符 → LaTeX
NARY_MAP = {
    "∑": r"\sum", "∏": r"\prod", "∫": r"\int", "∬": r"\iint", "∭": r"\iiint",
    "∮": r"\oint", "⋃": r"\bigcup", "⋂": r"\bigcap", "⋁": r"\bigvee", "⋀": r"\bigwedge",
}

# 重音符（m:acc 的 chr）→ LaTeX
ACCENT_MAP = {
    "\u0302": r"\hat", "\u0303": r"\tilde", "\u0304": r"\bar", "\u0305": r"\bar",
    "\u0307": r"\dot", "\u0308": r"\ddot", "\u20d7": r"\vec", "\u2192": r"\vec",
    "\u030c": r"\check", "\u0306": r"\breve",
}

GROUP_CHR_MAP = {"⏟": r"\underbrace", "⏞": r"\overbrace", "\u0332": r"\underline"}

# 需要在文本模式转 LaTeX 时按符号翻译，其他 ASCII 原样保留
_PASSTHROUGH_OK = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 +-*/=()[],.;:!<>|'?")


def omml_to_latex(omml_xml: str) -> str:
    """入口：接收 <m:oMath>...</m:oMath> 的 XML 字符串，返回 LaTeX 公式体（不含 $ 定界符）。"""
    try:
        root = etree.fromstring(omml_xml.encode("utf-8"))
    except etree.XMLSyntaxError as e:
        raise OmmlParseError(f"OMML XML 解析失败：{e}") from e
    latex = _convert(root).strip()
    if not latex:
        raise OmmlParseError("OMML 转换结果为空")
    return latex


##### XML 辅助板块 #####
def _children(el, tag: str):
    return el.findall(_m(tag))


def _child(el, tag: str):
    return el.find(_m(tag))


def _convert_children(el) -> str:
    return "".join(_convert(c) for c in el)


def _convert(el) -> str:  # noqa: C901  （分发函数，圈复杂度可接受）
    tag = etree.QName(el).localname if el.tag.startswith("{") else el.tag
    ns = etree.QName(el).namespace if el.tag.startswith("{") else None

    # 忽略 wordprocessing 命名空间的属性类节点
    if ns == W_NS:
        return ""

    if ns != M_NS:
        raise OmmlParseError(f"无法识别的命名空间节点 <{el.tag}>")

    handler = _HANDLERS.get(tag)
    if handler is None:
        raise OmmlParseError(f"无法识别的 OMML 节点 <m:{tag}>")
    return handler(el)


##### 公式节点转换板块 #####
def _h_container(el) -> str:
    return _convert_children(el)


def _h_skip(_el) -> str:
    return ""


def _h_run(el) -> str:
    """m:r —— 文本 run。将 m:t 中的字符逐个翻译为 LaTeX。"""
    out = []
    for t in _children(el, "t"):
        for ch in t.text or "":
            if ch in UNICODE_MAP:
                out.append(UNICODE_MAP[ch])
            elif ch in _PASSTHROUGH_OK:
                out.append(ch)
            elif ch in "{}":
                out.append("\\" + ch)
            elif ch in "%&#_":
                out.append("\\" + ch)
            else:
                # 未知非 ASCII 字符：保守起见视为不可靠转换
                raise OmmlParseError(f"公式文本中包含未映射字符 {ch!r} (U+{ord(ch):04X})")
    return "".join(out)


def _h_frac(el) -> str:
    num = _child(el, "num")
    den = _child(el, "den")
    if num is None or den is None:
        raise OmmlParseError("m:f 缺少分子或分母")
    kind = _prop_val(el, "fPr", "type")
    if kind is None:
        kind = "bar"
    numerator, denominator = _convert_children(num), _convert_children(den)
    if kind == "bar":
        return r"\frac{%s}{%s}" % (numerator, denominator)
    if kind == "noBar":
        # 使用 TeX 原生无横线分式，保留数学样式且不增加宏包依赖。
        return r"{%s \atop %s}" % (numerator, denominator)
    raise OmmlParseError(f"暂不支持的分式类型：{kind}")


def _h_ssup(el) -> str:
    base = _convert_children(_require(el, "e"))
    sup = _convert_children(_require(el, "sup"))
    return "{%s}^{%s}" % (base, sup)


def _h_ssub(el) -> str:
    base = _convert_children(_require(el, "e"))
    sub = _convert_children(_require(el, "sub"))
    return "{%s}_{%s}" % (base, sub)


def _h_ssubsup(el) -> str:
    base = _convert_children(_require(el, "e"))
    sub = _convert_children(_require(el, "sub"))
    sup = _convert_children(_require(el, "sup"))
    return "{%s}_{%s}^{%s}" % (base, sub, sup)


def _h_spre(el) -> str:
    base = _convert_children(_require(el, "e"))
    sub = _child(el, "sub")
    sup = _child(el, "sup")
    pre = "{}"
    if sub is not None:
        pre += "_{%s}" % _convert_children(sub)
    if sup is not None:
        pre += "^{%s}" % _convert_children(sup)
    return pre + base


def _h_rad(el) -> str:
    e = _convert_children(_require(el, "e"))
    deg_el = _child(el, "deg")
    deg_hide = _prop_val(el, "radPr", "degHide") == "1"
    if deg_el is not None and not deg_hide:
        deg = _convert_children(deg_el)
        if deg.strip():
            return r"\sqrt[%s]{%s}" % (deg, e)
    return r"\sqrt{%s}" % e


def _h_nary(el) -> str:
    chr_val = _prop_val(el, "naryPr", "chr") or "∫"
    op = NARY_MAP.get(chr_val)
    if op is None:
        raise OmmlParseError(f"未映射的大型运算符 {chr_val!r}")
    sub_el, sup_el = _child(el, "sub"), _child(el, "sup")
    out = op
    if sub_el is not None and _convert_children(sub_el).strip():
        out += "_{%s}" % _convert_children(sub_el)
    if sup_el is not None and _convert_children(sup_el).strip():
        out += "^{%s}" % _convert_children(sup_el)
    out += " " + _convert_children(_require(el, "e"))
    return out


def _h_delim(el) -> str:
    beg = _prop_val(el, "dPr", "begChr")
    end = _prop_val(el, "dPr", "endChr")
    beg = "(" if beg is None else beg
    end = ")" if end is None else end
    beg_l = {"(": "(", "[": "[", "{": r"\{", "|": "|", "": "."}.get(beg)
    end_l = {")": ")", "]": "]", "}": r"\}", "|": "|", "": "."}.get(end)
    if beg_l is None or end_l is None:
        raise OmmlParseError(f"未映射的定界符 {beg!r} {end!r}")
    inner = " , ".join(_convert_children(e) for e in _children(el, "e"))
    return r"\left%s %s \right%s" % (beg_l, inner, end_l)


def _h_matrix(el) -> str:
    rows = []
    for mr in _children(el, "mr"):
        cells = [_convert_children(e) for e in _children(mr, "e")]
        rows.append(" & ".join(cells))
    if not rows:
        raise OmmlParseError("m:m 矩阵没有任何行")
    return "\\begin{matrix} %s \\end{matrix}" % r" \\ ".join(rows)


def _h_func(el) -> str:
    name = _convert_children(_require(el, "fName")).strip()
    arg = _convert_children(_require(el, "e"))
    known = {"sin", "cos", "tan", "cot", "sec", "csc", "log", "ln", "exp",
             "max", "min", "lim", "arg", "det", "gcd", "sup", "inf"}
    if name in known:
        return r"\%s %s" % (name, arg)
    return r"\operatorname{%s} %s" % (name, arg)


def _h_limlow(el) -> str:
    base = _convert_children(_require(el, "e")).strip()
    lim = _convert_children(_require(el, "lim"))
    if base in (r"\lim", r"\max", r"\min", r"\sup", r"\inf", "lim", "max", "min"):
        base_cmd = base if base.startswith("\\") else "\\" + base
        return r"%s_{%s}" % (base_cmd, lim)
    return r"\underset{%s}{%s}" % (lim, base)


def _h_limupp(el) -> str:
    base = _convert_children(_require(el, "e"))
    lim = _convert_children(_require(el, "lim"))
    return r"\overset{%s}{%s}" % (lim, base)


def _h_bar(el) -> str:
    pos = _prop_val(el, "barPr", "pos")
    if pos is None:
        pos = "bot"
    if pos not in {"top", "bot"}:
        raise OmmlParseError(f"未知横线位置：{pos}")
    e = _convert_children(_require(el, "e"))
    return (r"\overline{%s}" if pos == "top" else r"\underline{%s}") % e


def _h_acc(el) -> str:
    chr_val = _prop_val(el, "accPr", "chr") or "\u0302"
    cmd = ACCENT_MAP.get(chr_val)
    if cmd is None:
        raise OmmlParseError(f"未映射的重音符 {chr_val!r}")
    return r"%s{%s}" % (cmd, _convert_children(_require(el, "e")))


def _h_groupchr(el) -> str:
    chr_val = _prop_val(el, "groupChrPr", "chr") or "⏟"
    cmd = GROUP_CHR_MAP.get(chr_val)
    if cmd is None:
        raise OmmlParseError(f"未映射的组合符号 {chr_val!r}")
    return r"%s{%s}" % (cmd, _convert_children(_require(el, "e")))


def _h_eqarr(el) -> str:
    rows = [_convert_children(e) for e in _children(el, "e")]
    return "\\begin{aligned} %s \\end{aligned}" % r" \\ ".join(rows)


def _h_box(el) -> str:
    return _convert_children(_require(el, "e"))


def _h_phantom(el) -> str:
    """保留公式占位尺寸而不绘制内容。"""
    return r"\phantom{%s}" % _h_box(el)


def _h_borderbox(el) -> str:
    """为公式内容绘制外框。"""
    return r"\boxed{%s}" % _h_box(el)


##### 节点分派板块 #####


_HANDLERS = {
    "oMath": _h_container, "oMathPara": _h_container,
    "e": _h_container, "num": _h_container, "den": _h_container,
    "sub": _h_container, "sup": _h_container, "deg": _h_container,
    "lim": _h_container, "fName": _h_container,
    "r": _h_run,
    "f": _h_frac,
    "sSup": _h_ssup, "sSub": _h_ssub, "sSubSup": _h_ssubsup, "sPre": _h_spre,
    "rad": _h_rad, "nary": _h_nary, "d": _h_delim, "m": _h_matrix,
    "func": _h_func, "limLow": _h_limlow, "limUpp": _h_limupp,
    "bar": _h_bar, "acc": _h_acc, "groupChr": _h_groupchr,
    "eqArr": _h_eqarr, "box": _h_box, "borderBox": _h_borderbox, "phant": _h_phantom,
    # 属性节点：不产出内容
    "oMathParaPr": _h_skip, "rPr": _h_skip, "ctrlPr": _h_skip,
    "fPr": _h_skip, "sSupPr": _h_skip, "sSubPr": _h_skip, "sSubSupPr": _h_skip,
    "sPrePr": _h_skip, "radPr": _h_skip, "naryPr": _h_skip, "dPr": _h_skip,
    "mPr": _h_skip, "funcPr": _h_skip, "limLowPr": _h_skip, "limUppPr": _h_skip,
    "barPr": _h_skip, "accPr": _h_skip, "groupChrPr": _h_skip, "eqArrPr": _h_skip,
    "boxPr": _h_skip, "borderBoxPr": _h_skip, "phantPr": _h_skip, "argPr": _h_skip,
    "t": _h_skip,  # m:t 由 _h_run 内部消费；单独出现时忽略
}


##### 必需节点和属性板块 #####
def _require(el, tag: str):
    c = _child(el, tag)
    if c is None:
        raise OmmlParseError(f"<m:{etree.QName(el).localname}> 缺少必需子节点 <m:{tag}>")
    return c


def _prop_val(el, pr_tag: str, attr_tag: str):
    """读取属性节点，如 m:naryPr/m:chr@m:val。"""
    pr = _child(el, pr_tag)
    if pr is None:
        return None
    node = pr.find(_m(attr_tag))
    if node is None:
        return None
    return node.get(_m("val"))
