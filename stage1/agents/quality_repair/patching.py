# -*- coding: utf-8 -*-
"""受保护区域的补丁准备；执行与原字节回滚由动作日志统一负责。"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PureWindowsPath
import re

from pipeline.source_content_check import strip_latex_comments
from pipeline.text_processing import escape_text


##### 区域和安全常量板块 #####

@dataclass(frozen=True, slots=True)
class WriteRegion:
    """来自可信渲染追踪的目标，不由模型提供可写文件白名单。"""

    relative_path: str
    unit_id: str
    role: str


class PatchSafetyError(ValueError):
    """补丁不能被证明局限于已确认的内容与区域。"""


_FORBIDDEN = re.compile(
    r"\\(?:input|include(?!graphics)|openout|openin|read|write|immediate|csname|catcode|"
    r"def|edef|gdef|xdef|let|futurelet|usepackage|documentclass|special|directlua)\b|\^\^",
)
_DIMENSION = r"[-+]?\d+(?:\.\d+)?(?:pt|bp|mm|cm|in|em|ex|\\(?:textwidth|linewidth))"
_FORMULAS = re.compile(
    r"(?<!\\)\$(?!\$).*?(?<!\\)\$|\\\(.*?\\\)|\\\[.*?\\\]|"
    r"\\begin\{(equation\*?|align\*?|gather\*?)\}.*?\\end\{\1\}", re.S,
)


##### 所有权和读取板块 #####

def writable_path(root: Path, relative_path: str, generation_id: str) -> Path:
    """检查 staging 状态、候选身份、每级符号链接以及实际绝对路径。"""
    root = root.resolve()
    try:
        marker = json.loads((root / ".scholar-generation.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PatchSafetyError("候选缺少有效 staging 所有权标记。") from exc
    if marker.get("status") != "staging" or marker.get("generation_id") != generation_id:
        raise PatchSafetyError("候选状态或身份与本次运行不一致。")
    relative = PureWindowsPath(relative_path)
    if relative.is_absolute() or relative.drive or ".." in relative.parts:
        raise PatchSafetyError("补丁路径必须是候选内的安全相对路径。")
    path = root
    for part in relative.parts:
        path = path / part
        if path.is_symlink():
            raise PatchSafetyError("补丁路径不能经过符号链接。")
    path = path.resolve()
    if root not in path.parents or path.suffix.lower() != ".tex" or not path.is_file():
        raise PatchSafetyError("补丁只能修改既有候选 TeX 文件。")
    return path


def region_bounds(source: str, region: WriteRegion) -> tuple[int, int]:
    """在唯一已登记标记内定位正文，不使用全文件文本唯一性替代区域边界。"""
    if region.role == "overrides":
        if region.relative_path != "scholar_overrides.tex":
            raise PatchSafetyError("格式覆盖文件位置无效。")
        return 0, len(source)
    if region.role == "main_config":
        if region.relative_path != "main.tex":
            raise PatchSafetyError("入口配置区域位置无效。")
        begin, end = "% SCHOLAR_AGENT_CONFIG_BEGIN", "% SCHOLAR_AGENT_CONFIG_END"
        pattern = re.compile(re.escape(begin) + r"\r?\n(?P<body>.*?)" + re.escape(end), re.S)
    else:
        pattern = re.compile(
            r"% SCHOLAR_UNIT_BEGIN " + re.escape(region.unit_id) + " " + re.escape(region.role)
            + r"\r?\n(?P<body>.*?)% SCHOLAR_UNIT_END " + re.escape(region.unit_id) + r"(?=\s|$)", re.S,
        )
    matches = list(pattern.finditer(source))
    if len(matches) != 1:
        raise PatchSafetyError("目标必须对应唯一已登记区域。")
    return matches[0].span("body")


##### 普通内容保护板块 #####

def _graphics_layout(match):
    """只把图片尺寸视为排版；裁切、草稿替代及页选择不能冒充尺寸调整。"""
    for option in match.group(2).split(","):
        option = option.strip()
        if option == "keepaspectratio":
            continue
        key, separator, value = option.partition("=")
        if (not separator or key.strip() not in {"width", "height", "totalheight"}
            or not re.fullmatch(_DIMENSION + r"|\\(?:textwidth|linewidth)", value.strip())):
            raise PatchSafetyError("图片选项包含不能证明保持完整图像的操作。")
        number = re.match(r"[-+]?\d+(?:\.\d+)?", value.strip())
        if number and float(number.group()) <= 0:
            raise PatchSafetyError("图片尺寸必须为正，不能隐藏原始图像。")
    return match.group(1)

def semantic_signature(text: str) -> tuple:
    """只消去已知排版语法；文字、标点、词边界和数学岛保持严格一致。"""
    provenance = tuple(re.findall(r"(?m)^% SCHOLAR_(?:CELL|UNIT)_(?:BEGIN|END)[^\r\n]*", text))
    text = strip_latex_comments(text)
    text = _FORMULAS.sub(lambda match: "MATH" + hashlib.sha256(match.group().encode()).hexdigest(), text)
    # longtable 的题注需要独立行结束符，它不属于论文数据行。
    text = re.sub(r"(\\(?:caption\{[^{}]*\}|tablecaption\{[^{}]*\}\{[^{}]*\}))\s*\\\\", r"\1", text)
    text = re.sub(r"\\begin\{(?:tabular|longtable)\}(?:\[[^\]]*\])?\{[lcr| ]+\}", "", text)
    text = re.sub(r"\\(?:begin|end)\{(?:figure\*?|table\*?|tabular|longtable|center|flushleft|flushright)\}(?:\[[^\]]*\])?", "", text)
    text = re.sub(r"\\(?:begingroup|endgroup|centering|raggedright|raggedleft|selectfont|small|normalsize|footnotesize|large|noindent|indent)\b", "", text)
    text = re.sub(r"\\setlength\{\\[A-Za-z]+\}\{" + _DIMENSION + r"\}", "", text)
    text = re.sub(r"\\fontsize\{" + _DIMENSION + r"\}\{" + _DIMENSION + r"\}", "", text)
    text = re.sub(r"\\(?:vspace|hspace)\*?\{" + _DIMENSION + r"\}", "", text)
    text = re.sub(r"\\renewcommand\{\\arraystretch\}\{\d+(?:\.\d+)?\}", "", text)
    text = re.sub(r"\\(?:toprule|midrule|bottomrule)(?:\[[^\]]*\])?", "", text)
    text = re.sub(r"(\\includegraphics)\[([^\]]*)\]", _graphics_layout, text)
    # 不吞掉转义花括号、转义标点或数学内容；仅普通 TeX 分组不构成可见文字。
    text = re.sub(r"\\.|[{}]", lambda match: match.group() if match.group().startswith("\\") else "", text)
    tokens = tuple(re.findall(r"[A-Za-z0-9]+|\\.|[^\s]", text))
    return provenance, tokens


def _prepare_region(root: Path, generation_id: str, region: WriteRegion, expected_sha256: str):
    path = writable_path(root, region.relative_path, generation_id)
    original = path.read_bytes()
    if hashlib.sha256(original).hexdigest() != expected_sha256:
        raise PatchSafetyError("目标证据已经过期，请重新读取当前单元。")
    source = original.decode("utf-8")
    start, end = region_bounds(source, region)
    return original, source, start, end


def prepare_patch(
    root: Path, *, generation_id: str, region: WriteRegion, expected_sha256: str, edits: list[dict],
) -> dict:
    """先在内存中完成整组局部编辑和语义验证，任何失败都不写文件。"""
    original, source, start, end = _prepare_region(root, generation_id, region, expected_sha256)
    before = source[start:end]
    current = before
    if not 1 <= len(edits) <= 5:
        raise PatchSafetyError("一个动作只能包含少量同一目标的局部编辑。")
    if sum(len(item.get("new_text", "").encode("utf-8")) for item in edits) > 64000:
        raise PatchSafetyError("局部补丁超出允许大小。")
    for edit in edits:
        if set(edit) != {"old_text", "new_text"}:
            raise PatchSafetyError("补丁只能提供原文和新片段。")
        old, new = edit["old_text"], edit["new_text"]
        if not isinstance(old, str) or not isinstance(new, str) or not old or current.count(old) != 1:
            raise PatchSafetyError("原文必须在目标区域内唯一命中。")
        if _FORBIDDEN.search(new):
            raise PatchSafetyError("补丁包含禁止的文件、命令或宏定义操作。")
        current = current.replace(old, new, 1)
    visible = strip_latex_comments(current).strip()
    if r'\begin{longtable}' in visible:
        prefix = visible.split(r'\begin{longtable}', 1)[0]
        if re.search(r'\\(?:centering|raggedright|raggedleft|fontsize|zihao|songti|setlength|renewcommand|small|footnotesize)\b', prefix):
            if not visible.startswith(r'\begingroup') or not visible.endswith(r'\endgroup'):
                raise PatchSafetyError('跨页表格的格式声明必须保留局部分组。')
            depth = 0
            for match in re.finditer(r'\\(?:begingroup|endgroup)\b', visible):
                depth += 1 if match.group() == r'\begingroup' else -1
                if depth < 0 or (depth == 0 and match.end() != len(visible)):
                    raise PatchSafetyError('跨页表格局部分组不能提前结束。')
            if depth:
                raise PatchSafetyError('跨页表格局部分组不完整。')
    if semantic_signature(before) != semantic_signature(current):
        raise PatchSafetyError("补丁改变了论文文字、语义对象或来源标记。")
    after = (source[:start] + current + source[end:]).encode("utf-8")
    if after == original:
        raise PatchSafetyError("补丁没有产生实际修改。")
    return {"relative_path": region.relative_path, "unit_id": region.unit_id,
            "before": original, "after": after, "old_fragment": before, "new_fragment": current}


##### 已确认普通正文替换板块 #####

def project_approved_fragments(segments: dict, changes: list[dict], *, confirmed_replacements=()) -> dict:
    """逐项核对真实补丁后还原来源视图，让原有 token 门禁继续独立证明源内容。

    仅作用于内容证明的内存副本；结构、编译和格式门禁仍读取真实候选文件。
    更改来源、偷偷追加文字、重复动作或改变公式均不能被日志放行。
    """
    from copy import deepcopy
    identifiers = [item["action_id"] for item in changes]
    if len(identifiers) != len(set(identifiers)):
        raise PatchSafetyError("修订动作重复，无法证明唯一来源。")
    projected = deepcopy(segments)
    approvals = {item["goal_id"]: item for item in confirmed_replacements}
    if len(approvals) != len(confirmed_replacements):
        raise PatchSafetyError("正文替换确认目标重复。")
    for change in reversed(changes):
        segment = projected.get(change["unit_id"])
        if not segment or segment["target_file"] != change["relative_path"]:
            raise PatchSafetyError("修订动作与实际单元或章节文件不一致。")
        old = change["old_fragment"].replace("\r\n", "\n").rstrip("\n")
        new = change["new_fragment"].replace("\r\n", "\n").rstrip("\n")
        if segment["fragment"] != new or _FORBIDDEN.search(new):
            raise PatchSafetyError("实际片段与已验证修订动作不一致。")
        if change.get("kind") == "body_replace":
            _check_confirmed_replacement(change, segment, old, new, approvals)
        elif change.get("kind") != "format" or semantic_signature(old) != semantic_signature(new):
            raise PatchSafetyError("修订动作不能证明普通内容与语义对象保持不变。")
        segment["fragment"] = old
    return projected


def _check_confirmed_replacement(change, segment, old, new, approvals):
    """用独立确认事实证明完整替换，候选账本不能自行声明正文修改权限。"""
    goal = approvals.get(change.get("goal_id"))
    if (not goal or goal.get("confirmed") is not True or goal.get("kind") != "body_replace"
        or goal.get("target_unit_ids") != [change["unit_id"]] or segment["role"] != "body_paragraph"):
        raise PatchSafetyError("正文变化缺少对应的用户确认。")
    replacement = goal.get("replacement", {})
    if set(replacement) != {"old_text", "new_text"} or any(
        not isinstance(value, str) or not value.strip() for value in replacement.values()
    ):
        raise PatchSafetyError("正文替换确认文本不完整。")
    original, updated = escape_text(replacement["old_text"]), escape_text(replacement["new_text"])
    if (original == updated or old.count(original) != 1
        or semantic_signature(old) != semantic_signature(original)
        or old.replace(original, updated, 1) != new):
        raise PatchSafetyError("实际正文修改超出用户确认的完整替换范围。")


def prepare_confirmed_text(
    root: Path, *, generation_id: str, region: WriteRegion, expected_sha256: str, goal: dict,
) -> dict:
    """完整确认文本经普通文字转义写入，不能由模型临时改变替换内容。"""
    if goal.get("confirmed") is not True or goal.get("kind") != "body_replace":
        raise PatchSafetyError("正文替换尚未获得用户明确确认。")
    if region.role != "body_paragraph" or goal.get("target_unit_ids") != [region.unit_id]:
        raise PatchSafetyError("正文替换目标不符合已确认范围。")
    original, source, start, end = _prepare_region(root, generation_id, region, expected_sha256)
    replacement = goal["replacement"]
    old, new = escape_text(replacement["old_text"]), escape_text(replacement["new_text"])
    current = source[start:end]
    if not old or current.count(old) != 1 or semantic_signature(current) != semantic_signature(old):
        raise PatchSafetyError("目标并非完整普通正文，或原文已经发生变化。")
    updated = current.replace(old, new, 1)
    return {"relative_path": region.relative_path, "unit_id": region.unit_id,
            "before": original, "after": (source[:start] + updated + source[end:]).encode("utf-8"),
            "old_fragment": current, "new_fragment": updated, "confirmed_replacement": replacement}
