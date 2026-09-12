# -*- coding: utf-8 -*-
"""以已确认结构为期望值的确定性结构门禁。"""

import re
from pathlib import Path, PurePosixPath

from pipeline.source_content_check import strip_latex_comments
from pipeline.content_fidelity_gate import _parse_marked_files
from pipeline.text_processing import escape_text



##### 阻断与正文范围板块 #####


class FatalStructuralMismatch(Exception):
    """已确认结构与实际生成的标题、对象或公式数量不一致。"""
    def __init__(self, mismatches, checks):
        self.mismatches = mismatches
        self.checks = checks
        detail = '; '.join(
            f'{k}: Word={checks[k][0]} vs Generated={checks[k][1]}'
            for k in mismatches
        )
        super().__init__(f'Fatal structural mismatch: {detail}')


class FatalChapterReachability(Exception):
    """主文件引用、渲染清单或磁盘章节文件不一致。"""


##### 章节可达性板块 #####


def _normalize_chapter_reference(value):
    relative = PurePosixPath(str(value).replace('\\', '/'))
    if relative.suffix == '.tex':
        relative = relative.with_suffix('')
    return relative.as_posix()


def validate_chapter_reachability(
    main_tex_path, generated_tex_dir, chapter_files, *, template_adapter,
):
    """验证渲染清单中的每章都且仅被 main.tex 按顺序引用。"""
    main_path = Path(main_tex_path)
    contents_dir = Path(generated_tex_dir)
    if not main_path.is_file():
        raise FatalChapterReachability(f'main.tex 不存在：{main_path}')
    expected = [_normalize_chapter_reference(item) for item in chapter_files]
    if not expected or len(expected) != len(set(expected)):
        raise FatalChapterReachability('渲染章节清单为空或包含重复项。')
    try:
        included = template_adapter.validate_chapter_includes(main_path, chapter_files)
    except ValueError as exc:
        raise FatalChapterReachability(str(exc)) from exc
    except RuntimeError as exc:
        raise FatalChapterReachability(str(exc)) from exc
    included = [_normalize_chapter_reference(item) for item in included]
    directory_name = PurePosixPath(expected[0]).parent.as_posix()
    generated = sorted(
        _normalize_chapter_reference(f'{directory_name}/{path.name}')
        for path in contents_dir.glob('section_*.tex')
        if path.is_file()
    )
    if included != expected:
        raise FatalChapterReachability(
            f'main.tex 章节引用顺序或集合不一致：expected={expected}, included={included}'
        )
    if set(generated) != set(expected):
        raise FatalChapterReachability(
            f'磁盘章节文件存在缺失或孤立项：expected={expected}, generated={generated}'
        )
    return {
        'chapter_include_count': (len(expected), len(included)),
        'chapter_file_count': (len(expected), len(generated)),
    }


##### 结构逐项终验板块 #####


def structural_fidelity_gate(
    word_structure, generated_tex_dir, *, confirmed_structure,
    main_tex_path, chapter_files, template_adapter,
):
    """以确认单元、层级、题注绑定和文件顺序核对实际 LaTeX；计数仅为摘要。"""
    checks = validate_chapter_reachability(
        main_tex_path, generated_tex_dir, chapter_files, template_adapter=template_adapter,
    )
    try:
        segments, _, unexpected = _parse_marked_files(Path(main_tex_path).parent, chapter_files)
    except ValueError as exc:
        raise FatalStructuralMismatch(["marked_structure"], {"marked_structure": (1, 0)}) from exc
    headings = [item for item in confirmed_structure["heading_candidates"] if item["level"] != "body"]
    mapping = template_adapter.heading_command_mapping
    paragraphs = word_structure["paragraphs"]
    files_by_id = {}
    for chapter, relative in zip(confirmed_structure["chapters"], chapter_files):
        start, end = chapter["range"]
        files_by_id.update({item["unit_id"]: relative for item in paragraphs[start:end]})
    for heading in headings:
        unit_id, level = heading["unit_id"], heading["level"]
        segment = segments.get(unit_id, {})
        expected = template_adapter.render_heading(level, escape_text(heading["title"]), unit_id)["primary"]
        actual = strip_latex_comments(segment.get("fragment", "")).strip()
        # 仅允许已抽取书签附着于标题，不允许附加另一条标题或正文。
        pattern = re.escape(expected) + r"(?:\s*\\label\{[^{}]+\})*"
        valid = (
            segment.get("role") == f"{level}_heading"
            and segment.get("target_file") == files_by_id.get(unit_id)
            and re.fullmatch(pattern, actual) is not None
        )
        checks[f"heading:{unit_id}"] = (1, int(valid))
    bindings = confirmed_structure["object_bindings"]
    for binding in bindings:
        unit_id, kind = binding["caption_unit_id"], binding["kind"]
        segment = segments.get(unit_id, {})
        actual = strip_latex_comments(segment.get("fragment", ""))
        environments = re.findall(r"\\begin\{(figure|table|longtable)\}", actual)
        caption = escape_text(binding["caption_zh"])
        command = template_adapter.render_caption(kind, caption, binding.get("caption_en", ""))
        # 英文必需插槽由语言追踪验证；此处核对中文题注参数及实际环境。
        caption_prefix = command.split(caption, 1)[0] + caption
        valid = (
            binding.get("status") == "bound"
            and segment.get("role") == f"{kind}_caption"
            and segment.get("target_file") == files_by_id.get(unit_id)
            and len(environments) == 1
            and environments[0] in ({"figure"} if kind == "figure" else {"table", "longtable"})
            and actual.count(caption_prefix) == 1
        )
        checks[f"caption:{unit_id}"] = (1, int(valid))
    actual_text = "\n".join(
        strip_latex_comments((Path(main_tex_path).parent / item).read_text(encoding="utf-8"))
        for item in chapter_files
    )
    for level, command in mapping.items():
        checks[f"{level}_count"] = (
            sum(item["level"] == level for item in headings),
            len(re.findall(r"\\" + re.escape(command) + r"\{", actual_text)),
        )
    for kind in ("figure", "table"):
        environments = r"figure" if kind == "figure" else r"(?:table|longtable)"
        checks[f"{kind}_count"] = (
            sum(item["kind"] == kind for item in bindings),
            len(re.findall(r"\\begin\{" + environments + r"\}", actual_text)),
        )
    mismatches = [key for key, (expected, actual) in checks.items() if expected != actual]
    if mismatches:
        raise FatalStructuralMismatch(mismatches, checks)
    return checks
