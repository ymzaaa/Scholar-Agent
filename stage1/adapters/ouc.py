# -*- coding: utf-8 -*-
"""中国海洋大学模板的章节引用插槽。"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

from pipeline.template_profile import (
    TemplateFormatProfile, load_template_format_profile,
)
from pipeline.template_language import TemplateLanguageEdit, TemplateLanguageSlot
from pipeline.source_content_check import strip_latex_comments


##### 常量与异常板块 #####


CHAPTERS_BEGIN = "% SCHOLAR_AGENT_CHAPTERS_BEGIN"
CHAPTERS_END = "% SCHOLAR_AGENT_CHAPTERS_END"
BIBLIOGRAPHY_BEGIN = "% SCHOLAR_AGENT_BIBLIOGRAPHY_BEGIN"
BIBLIOGRAPHY_END = "% SCHOLAR_AGENT_BIBLIOGRAPHY_END"
_INCLUDE_PATTERN = re.compile(
    r"\\include\{(contents/section_[A-Za-z0-9_-]+)(?:\.tex)?\}"
)


class TemplateAdapterError(RuntimeError):
    """模板插槽、章节清单或引用结果不满足安全契约。"""


##### 路径规范化板块 #####


def _normalize_chapter_file(main_tex: Path, value: str) -> str:
    """返回不带 `.tex` 的 POSIX 相对引用，并验证文件位于模板目录内。"""
    normalized = value.replace("\\", "/")
    relative = PurePosixPath(normalized)
    if relative.is_absolute() or ".." in relative.parts:
        raise TemplateAdapterError(f"章节路径不安全：{value}")
    if relative.suffix == ".tex":
        relative = relative.with_suffix("")
    reference = relative.as_posix()
    if not re.fullmatch(r"contents/section_[A-Za-z0-9_-]+", reference):
        raise TemplateAdapterError(f"章节文件名不符合约定：{value}")
    root = main_tex.parent.resolve()
    target = (root / f"{reference}.tex").resolve()
    if root not in target.parents or not target.is_file():
        raise TemplateAdapterError(f"章节文件不存在或越界：{value}")
    return reference


def _normalize_chapter_files(main_tex: Path, chapter_files: list[str]) -> list[str]:
    if not chapter_files:
        raise TemplateAdapterError("章节清单为空，拒绝生成正文入口。")
    normalized = [_normalize_chapter_file(main_tex, item) for item in chapter_files]
    if len(normalized) != len(set(normalized)):
        raise TemplateAdapterError("章节清单包含重复文件。")
    return normalized


##### 插槽读写板块 #####


def _slot_bounds(text: str) -> tuple[int, int]:
    if text.count(CHAPTERS_BEGIN) != 1 or text.count(CHAPTERS_END) != 1:
        raise TemplateAdapterError("main.tex 必须各包含一个章节插槽起止标记。")
    start = text.index(CHAPTERS_BEGIN)
    end = text.index(CHAPTERS_END, start)
    if end <= start:
        raise TemplateAdapterError("main.tex 章节插槽标记顺序错误。")
    return start, end + len(CHAPTERS_END)


def _build_include_block(references: list[str]) -> str:
    lines = [CHAPTERS_BEGIN]
    for index, reference in enumerate(references):
        if index:
            lines.extend(["", r"\insertblankpageFancy%空白页判断"])
        lines.append(rf"\include{{{reference}}}")
    lines.append(CHAPTERS_END)
    return "\n".join(lines)


class OUCTemplateAdapter:
    """仅处理 OUC 模板专有的章节间空白页和主文件插槽。"""

    name = "ouc-graduate"
    content_directory = "contents"
    heading_command_mapping = {
        "chapter": "chapter", "section": "section",
        "subsection": "subsection",
    }
    entrypoint = "main.tex"
    required_metadata_fields = (
        "title", "author", "student_id", "advisor",
        "classification", "udc",
        "title_en", "degree_type", "major", "research_direction", "completion_date",
    )

    def bind_manifest(self, manifest) -> None:
        """把当前 Manifest 的运行布局绑定到适配器实例。"""
        self.entrypoint = manifest.compile_recipe.entrypoint
        self.content_directory = manifest.content_directory
        self.compile_recipe = manifest.compile_recipe

    def prepare_generation_staging(
        self, root: Path, metadata: dict[str, str],
    ) -> None:
        """只在已物化 staging 中准备研究生模板的生成插槽。"""
        self.write_minimal_front_matter(root, metadata)

    def validate_generated_staging(
        self, root: Path, chapter_files: list[str], bibliography_mode: str,
    ) -> bool:
        """确认生成章节之外没有遗留示例正文或错误引用后端。"""
        generated = {
            (root / Path(*item.split("/"))).with_suffix(".tex").resolve()
            for item in chapter_files
        }
        actual = {
            path.resolve() for path in (root / self.content_directory).glob("section_*.tex")
        }
        main = (root / self.entrypoint).read_text(encoding="utf-8")
        expected_word = 1 if bibliography_mode == "word" else 0
        return (
            actual == generated
            and main.count(r"\input{data/references.tex}") == expected_word
            and r"\bibliography{cite}" not in main
        )

    def write_cover_metadata(
        self, root: Path, metadata: dict[str, str]
    ) -> None:
        """写入模板字段宏；调用方负责转义，封面布局不得在此重建。"""
        macro_fields = (
            ("ScholarClassification", "classification"),
            ("ScholarUDC", "udc"),
            ("ScholarStudentId", "student_id"),
            ("ScholarTitleZh", "title"),
            ("ScholarTitleEn", "title_en"),
            ("ScholarAuthor", "author"),
            ("ScholarAdvisor", "advisor"),
            ("ScholarDegreeType", "degree_type"),
            ("ScholarMajor", "major"),
            ("ScholarResearchDirection", "research_direction"),
            ("ScholarCompletionDate", "completion_date"),
        )
        lines = ["% Scholar Agent generation metadata; values are LaTeX-escaped."]
        lines.extend(
            rf"\newcommand{{\{macro}}}{{{metadata[field]}}}"
            for macro, field in macro_fields
        )
        (root / "data" / "metadata.tex").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )
        main_path = root / "main.tex"
        main = main_path.read_text(encoding="utf-8")
        updated, count = re.subn(
            r"\\title\{[^{}]*\}",
            lambda _match: rf"\title{{{metadata['title']}}}",
            main,
            count=1,
        )
        if count != 1:
            raise TemplateAdapterError("研究生 main.tex 缺少唯一标题字段。")
        main_path.write_text(updated, encoding="utf-8")

    def write_minimal_front_matter(
        self, root: Path, metadata: dict[str, str]
    ) -> None:
        """写入最小案例字段与空摘要，不复制正文或伪造摘要。"""
        self.write_cover_metadata(root, metadata)
        (root / "data" / "abstract_ch.tex").write_text(
            "% Word 未提供摘要；首版保持为空。\n"
            "\\begin{abstract}\n\\end{abstract}\n",
            encoding="utf-8",
        )
        (root / "data" / "abstract_en.tex").write_text(
            "% Word 未提供英文摘要；首版保持为空。\n"
            "\\begin{enabstract}\n\\end{enabstract}\n",
            encoding="utf-8",
        )
        main_path = root / "main.tex"
        self.update_bibliography(main_path, "none")

    def write_abstract(self, root: Path, lang: str, latex: str) -> None:
        """研究生模板使用独立摘要文件，保持原模板的输入方式。"""
        filename = "abstract_ch.tex" if lang == "ch" else "abstract_en.tex"
        (root / "data").mkdir(parents=True, exist_ok=True)
        (root / "data" / filename).write_text(latex, encoding="utf-8")

    def format_rule_profile(self) -> Path:
        """返回由适配器显式声明的模板规则画像，而非让核心猜测学校规范。"""
        return Path(__file__).resolve().parent.parent / "rules" / "templates" / "ouc-format-profile.json"

    def format_contract(self) -> TemplateFormatProfile:
        """加载已随项目审核的 OUC 格式画像；运行时不重新解释规范 Word。"""
        return load_template_format_profile(self.format_rule_profile())

    def render_caption(self, kind: str, caption: str, caption_en: str) -> str:
        """研究生专有题注宏仅由适配器声明；参数已作普通文字转义。"""
        if kind == "figure":
            return rf"\figurecaption{{{caption}}}{{{caption}}}{{{caption_en}}}"
        if kind == "table":
            return rf"\tablecaption{{{caption}}}{{{caption_en}}}"
        raise TemplateAdapterError(f"未知题注类型：{kind}")

    def table_preamble(self) -> list[str]:
        """研究生表格字体与行距沿用既有格式。"""
        return [r"\renewcommand{\arraystretch}{1.6}", r"\zihao{5}\songti"]

    def render_heading(self, level: str, title: str, unit_id: str) -> dict[str, str]:
        """把通用标题语义映射为 OUC 正文标题和英文目录插槽。"""
        if level not in {"chapter", "section", "subsection"}:
            raise TemplateAdapterError(f"OUC 不支持标题层级：{level}")
        marker = f"% SCHOLAR_TEMPLATE_HEADING_EN {unit_id} {level}"
        return {
            "primary": rf"\{level}{{{title}}}",
            "supplementary": f"{marker}\n\\en{level}{{{title}}}",
        }

    def discover_language_slots(
        self, output_dir: str | Path, heading_records: list[dict],
    ) -> list[TemplateLanguageSlot]:
        """按研究生模板自身的宏与目录结构发现必需语言插槽。"""
        root = Path(output_dir).resolve()
        profile = self.format_contract()
        slots: list[TemplateLanguageSlot] = []
        if profile.requires_english_toc():
            for record in heading_records:
                level = str(record.get("level", ""))
                unit_id = str(record.get("unit_id", ""))
                source_text = str(record.get("text_zh", "")).strip()
                if level not in self.heading_command_mapping or not source_text:
                    continue
                path = (root / str(record.get("target_file", ""))).resolve()
                if root not in path.parents or not path.is_file():
                    raise TemplateAdapterError("英文目录标题目标文件不存在或越界。")
                content = path.read_text(encoding="utf-8")
                marker = re.escape(f"% SCHOLAR_TEMPLATE_HEADING_EN {unit_id} {level}")
                matches = list(re.finditer(
                    rf"({marker}\s*\n)\\en{level}\{{([^}}]*)\}}", content,
                ))
                if len(matches) != 1:
                    raise TemplateAdapterError(f"英文目录标题插槽不唯一：{unit_id}")
                match = matches[0]
                slots.append(TemplateLanguageSlot(
                    slot_id=f"heading:{unit_id}", category="heading", kind=level,
                    source_text=source_text, current_text=match.group(2),
                    relative_path=path.relative_to(root).as_posix(),
                    payload={
                        "start": match.start(), "end": match.end(),
                        "prefix": match.group(1), "level": level,
                    },
                ))

        contents_dir = root / self.content_directory
        if not contents_dir.is_dir():
            return slots
        for path in sorted(contents_dir.glob("section_*.tex")):
            content = path.read_text(encoding="utf-8")
            relative = path.relative_to(root).as_posix()
            if profile.requires_english_caption("figure"):
                for index, match in enumerate(re.finditer(
                    r"\\figurecaption\{([^}]+)\}\{([^}]+)\}\{([^}]*)\}", content,
                )):
                    slots.append(TemplateLanguageSlot(
                        slot_id=f"caption:{relative}:figure:{index}",
                        category="caption", kind="figure_caption",
                        source_text=match.group(2), current_text=match.group(3),
                        relative_path=relative,
                        payload={
                            "start": match.start(), "end": match.end(),
                            "label": match.group(1), "caption": match.group(2),
                        },
                    ))
            if profile.requires_english_caption("table"):
                for index, match in enumerate(re.finditer(
                    r"\\tablecaption\{([^}]+)\}\{([^}]*)\}", content,
                )):
                    slots.append(TemplateLanguageSlot(
                        slot_id=f"caption:{relative}:table:{index}",
                        category="caption", kind="table_caption",
                        source_text=match.group(1), current_text=match.group(2),
                        relative_path=relative,
                        payload={
                            "start": match.start(), "end": match.end(),
                            "caption": match.group(1),
                        },
                    ))
        return slots

    def build_language_edits(
        self, output_dir: str | Path, slots: list[TemplateLanguageSlot],
        translations: dict[tuple[str, str], str],
    ) -> list[TemplateLanguageEdit]:
        """只替换适配器已发现的语言参数，其他模板源码逐字保持。"""
        root = Path(output_dir).resolve()
        grouped: dict[str, list[TemplateLanguageSlot]] = {}
        for slot in slots:
            grouped.setdefault(slot.relative_path, []).append(slot)
        edits: list[TemplateLanguageEdit] = []
        for relative, items in grouped.items():
            path = (root / relative).resolve()
            if root not in path.parents or not path.is_file():
                raise TemplateAdapterError(f"语言插槽目标不存在或越界：{relative}")
            source = path.read_text(encoding="utf-8")
            updated = source
            for slot in sorted(items, key=lambda item: int(item.payload["start"]), reverse=True):
                start, end = int(slot.payload["start"]), int(slot.payload["end"])
                value = translations.get((slot.kind, slot.source_text))
                if value is None:
                    continue
                if slot.category == "heading":
                    replacement = (
                        str(slot.payload["prefix"])
                        + rf"\en{slot.payload['level']}{{{value}}}"
                    )
                elif slot.kind == "figure_caption":
                    replacement = (
                        rf"\figurecaption{{{slot.payload['label']}}}"
                        rf"{{{slot.payload['caption']}}}{{{value}}}"
                    )
                elif slot.kind == "table_caption":
                    replacement = rf"\tablecaption{{{slot.payload['caption']}}}{{{value}}}"
                else:
                    raise TemplateAdapterError(f"未知语言插槽类型：{slot.kind}")
                if not 0 <= start <= end <= len(source):
                    raise TemplateAdapterError(f"语言插槽偏移越界：{slot.slot_id}")
                updated = updated[:start] + replacement + updated[end:]
            if updated != source:
                edits.append(TemplateLanguageEdit(relative, source, updated))
        return edits

    @staticmethod
    def mask_language_slots(text: str) -> str:
        """掩盖研究生模板允许变化的英文参数，供通用安全事务比较。"""
        value = re.sub(
            r"\\en(?:chapter|section|subsection)\{[^{}]*\}",
            r"\\enheading{__ENGLISH__}", text,
        )
        value = re.sub(
            r"(\\figurecaption\{[^{}]*\}\{[^{}]*\})\{[^{}]*\}",
            r"\1{__ENGLISH__}", value,
        )
        return re.sub(
            r"(\\tablecaption\{[^{}]*\})\{[^{}]*\}",
            r"\1{__ENGLISH__}", value,
        )

    def update_bibliography(self, main_tex_path: str | Path, mode: str) -> None:
        """在模板专有插槽中选择BibTeX或Word原始列表后端。"""
        main_tex = Path(main_tex_path).resolve()
        text = main_tex.read_text(encoding="utf-8")
        if text.count(BIBLIOGRAPHY_BEGIN) != 1 or text.count(BIBLIOGRAPHY_END) != 1:
            raise TemplateAdapterError("main.tex 必须各包含一个参考文献插槽标记。")
        start = text.index(BIBLIOGRAPHY_BEGIN)
        end = text.index(BIBLIOGRAPHY_END, start) + len(BIBLIOGRAPHY_END)
        common = [
            BIBLIOGRAPHY_BEGIN,
            r"\insertblankpageFancy%空白页判断",
            r"\phantomsection\addengcontent{chapter}{References}",
        ]
        if mode == "bibtex":
            common.extend([r"\bibliographystyle{oucauthoryear}", r"\bibliography{cite}"])
        elif mode == "word":
            common.append(r"\input{data/references.tex}")
        elif mode == "none":
            common = [BIBLIOGRAPHY_BEGIN]
        else:
            raise TemplateAdapterError(f"未知参考文献后端：{mode}")
        common.append(BIBLIOGRAPHY_END)
        main_tex.write_text(text[:start] + "\n".join(common) + text[end:], encoding="utf-8")

    def update_chapter_includes(
        self, main_tex_path: str | Path, chapter_files: list[str]
    ) -> list[str]:
        main_tex = Path(main_tex_path).resolve()
        if not main_tex.is_file():
            raise TemplateAdapterError(f"main.tex 不存在：{main_tex}")
        references = _normalize_chapter_files(main_tex, chapter_files)
        text = main_tex.read_text(encoding="utf-8")
        start, end = _slot_bounds(text)
        outside = text[:start] + text[end:]
        if _INCLUDE_PATTERN.search(strip_latex_comments(outside)):
            raise TemplateAdapterError("章节插槽之外存在正文章节引用。")
        updated = text[:start] + _build_include_block(references) + text[end:]
        main_tex.write_text(updated, encoding="utf-8")
        self.validate_chapter_includes(main_tex, chapter_files)
        return references

    def validate_chapter_includes(
        self, main_tex_path: str | Path, chapter_files: list[str]
    ) -> list[str]:
        main_tex = Path(main_tex_path).resolve()
        expected = _normalize_chapter_files(main_tex, chapter_files)
        text = main_tex.read_text(encoding="utf-8")
        start, end = _slot_bounds(text)
        inside = text[start:end]
        outside = text[:start] + text[end:]
        actual = _INCLUDE_PATTERN.findall(strip_latex_comments(inside))
        if _INCLUDE_PATTERN.search(strip_latex_comments(outside)):
            raise TemplateAdapterError("章节插槽之外存在正文章节引用。")
        if actual != expected:
            raise TemplateAdapterError(
                f"main.tex 章节引用与渲染清单不一致：expected={expected}, actual={actual}"
            )
        blank_count = strip_latex_comments(inside).count(r"\insertblankpageFancy")
        if blank_count != max(0, len(expected) - 1):
            raise TemplateAdapterError("OUC 章节间空白页命令数量不正确。")
        return actual
