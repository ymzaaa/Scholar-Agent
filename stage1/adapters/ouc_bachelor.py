# -*- coding: utf-8 -*-
"""OUC 本科模板在 generation staging 内的槽位与标题适配。"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

from adapters.ouc import TemplateAdapterError
from pipeline.source_content_check import strip_latex_comments


##### 常量板块 #####


CHAPTERS_BEGIN = "% SCHOLAR_AGENT_CHAPTERS_BEGIN"
CHAPTERS_END = "% SCHOLAR_AGENT_CHAPTERS_END"
BIBLIOGRAPHY_BEGIN = "% SCHOLAR_AGENT_BIBLIOGRAPHY_BEGIN"
BIBLIOGRAPHY_END = "% SCHOLAR_AGENT_BIBLIOGRAPHY_END"
_INPUT_PATTERN = re.compile(r"\\input\{(includes/section_[A-Za-z0-9_-]+)\}")
_ABSTRACT_MARKERS = {
    "ch": ("% SCHOLAR_AGENT_CN_ABSTRACT_BEGIN", "% SCHOLAR_AGENT_CN_ABSTRACT_END"),
    "en": ("% SCHOLAR_AGENT_EN_ABSTRACT_BEGIN", "% SCHOLAR_AGENT_EN_ABSTRACT_END"),
}


##### 本科适配器板块 #####


class OUCBachelorTemplateAdapter:
    """只处理本科模板专有命令；原始注册模板始终保持只读。"""

    name = "ouc-bachelor"
    content_directory = "includes"
    heading_command_mapping = {
        "chapter": "section", "section": "subsection",
        "subsection": "subsubsection",
    }
    entrypoint = "main.tex"
    required_metadata_fields = (
        "title", "title_en", "author", "student_id", "advisor",
        "major",
    )

    @staticmethod
    def mask_language_slots(text: str) -> str:
        """本科没有派生英文插槽，全部可见题注均须保留。"""
        return text

    def bind_manifest(self, manifest) -> None:
        self.entrypoint = manifest.compile_recipe.entrypoint
        self.content_directory = manifest.content_directory
        self.compile_recipe = manifest.compile_recipe

    def prepare_generation_staging(
        self, root: Path, metadata: dict[str, str],
    ) -> None:
        """只在已物化 staging 中建立本科模板所需的干净主文件。"""
        self.write_cover_metadata(root, metadata)
        main_path = root / self.entrypoint
        main = main_path.read_text(encoding="utf-8")
        packages = r"\usepackage{graphicx,booktabs,multirow,makecell,diagbox,adjustbox,url,amssymb}"
        # Word 零宽分隔符不要求字形；活动字符保留命令边界，避免直接忽略后粘连命令名。
        unicode_compatibility = (
            r'\catcode"200B=13\relax\protected\def^^^^200b{}' + "\n"
            # 本科传统数学字体使用等价命令显示集合成员符号，不改写公式源文本。
            + r'\catcode"2208=13\relax\protected\def^^^^2208{\ensuremath{\in}}'
        )
        main = main.replace(r"\begin{document}",
                            packages + "\n" + unicode_compatibility + "\n" + r"\begin{document}", 1)
        main_path.write_text(main, encoding="utf-8")

    def validate_generated_staging(
        self, root: Path, chapter_files: list[str], bibliography_mode: str,
    ) -> bool:
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
            and not (root / "openingreport.tex").exists()
            and r"\begin{thebibliography}" not in main
            and "致谢" not in main
            and main.count(r"\input{data/references.tex}") == expected_word
            and r"\bibliography{cite}" not in main
        )

    def format_rule_profile(self) -> Path:
        """本科尚无已审核学校规范画像，格式门禁只装载通用安全规则。"""
        return (
            Path(__file__).resolve().parent.parent
            / "rules" / "templates" / "ouc-bachelor-common-only-profile.json"
        )

    def render_caption(self, kind: str, caption: str, caption_en: str) -> str:
        """本科使用标准题注命令，已有双语文字保留在同一个题注中。"""
        if kind not in {"figure", "table"}:
            raise TemplateAdapterError(f"未知题注类型：{kind}")
        visible = caption + (" " + caption_en if caption_en else "")
        return rf"\caption{{{visible}}}"

    def table_preamble(self) -> list[str]:
        """本科表格继承模板字体。"""
        return []

    def render_heading(self, level: str, title: str, unit_id: str) -> dict[str, str]:
        del unit_id
        mapping = {
            "chapter": "section", "section": "subsection",
            "subsection": "subsubsection",
        }
        command = mapping.get(level)
        if command is None:
            raise TemplateAdapterError(f"本科模板不支持逻辑标题层级：{level}")
        return {"primary": rf"\{command}{{{title}}}", "supplementary": ""}

    def write_cover_metadata(
        self, root: Path, metadata: dict[str, str],
    ) -> None:
        """按本科适配器契约建立干净 staging；缺失字段保持待填写。"""
        from pipeline.metadata_resolution import PLACEHOLDER

        values = {
            "title": metadata.get("title", PLACEHOLDER),
            "title_en": metadata.get("title_en", PLACEHOLDER),
            "author": metadata.get("author", PLACEHOLDER),
            "student_id": metadata.get("student_id", PLACEHOLDER),
            "advisor": metadata.get("advisor", PLACEHOLDER),
            "college": PLACEHOLDER,
            "department": metadata.get("major", PLACEHOLDER),
            "grade": PLACEHOLDER,
        }
        main_path = root / self.entrypoint
        text = main_path.read_text(encoding="utf-8")
        commands = {
            "title": values["title"], "entitle": values["title_en"],
            "author": values["author"], "studentid": values["student_id"],
            "advisor": values["advisor"], "grade": values["grade"],
        }
        for command, value in commands.items():
            text, count = re.subn(
                rf"\\{command}\{{[^{{}}]*\}}",
                lambda _match, name=command, item=value: rf"\{name}{{{item}}}",
                text, count=1,
            )
            if count != 1:
                raise TemplateAdapterError(f"本科 main.tex 缺少唯一字段：{command}")
        text, count = re.subn(
            r"\\department\{[^{}]*\}\{[^{}]*\}",
            lambda _match: rf"\department{{{values['college']}}}{{{values['department']}}}",
            text, count=1,
        )
        if count != 1:
            raise TemplateAdapterError("本科 main.tex 缺少唯一院系字段。")
        main_path.write_text(text, encoding="utf-8")

    def write_abstract(self, root: Path, lang: str, latex: str) -> None:
        """把通用抽取结果写入本科模板自己的摘要宏，不创建无效旁路文件。"""
        environment = "abstract" if lang == "ch" else "enabstract"
        match = re.search(
            rf"\\begin\{{{environment}\}}\s*(.*?)\s*\\end\{{{environment}\}}",
            latex, flags=re.DOTALL,
        )
        if not match:
            raise TemplateAdapterError(f"摘要内容缺少 {environment} 环境。")
        body = match.group(1).strip()
        keyword_match = re.search(
            r"\\noindent\\textbf\{[^}]*\}([^\n]*)", body,
        )
        keywords = keyword_match.group(1).strip() if keyword_match else ""
        if keyword_match:
            body = (body[:keyword_match.start()] + body[keyword_match.end():]).strip()
        command = "cnabstractkeywords" if lang == "ch" else "enabstractkeywords"
        begin, end = _ABSTRACT_MARKERS[lang]
        main_path = root / self.entrypoint
        text = main_path.read_text(encoding="utf-8")
        if text.count(begin) != 1 or text.count(end) != 1:
            raise TemplateAdapterError(f"本科 main.tex 缺少唯一摘要插槽：{lang}")
        start = text.index(begin)
        finish = text.index(end, start) + len(end)
        replacement = f"{begin}\n\\{command}{{{body}}}{{{keywords}}}\n{end}"
        main_path.write_text(text[:start] + replacement + text[finish:], encoding="utf-8")

    def update_chapter_includes(self, main_tex_path: Path, chapter_files: list[str]) -> list[str]:
        references = []
        root = main_tex_path.parent.resolve()
        for value in chapter_files:
            relative = PurePosixPath(value)
            if relative.suffix == ".tex":
                relative = relative.with_suffix("")
            reference = relative.as_posix()
            if not re.fullmatch(r"includes/section_[A-Za-z0-9_-]+", reference):
                raise TemplateAdapterError(f"本科章节路径无效：{value}")
            if not (root / f"{reference}.tex").is_file():
                raise TemplateAdapterError(f"本科章节文件不存在：{value}")
            references.append(reference)
        text = main_tex_path.read_text(encoding="utf-8")
        if text.count(CHAPTERS_BEGIN) != 1 or text.count(CHAPTERS_END) != 1:
            raise TemplateAdapterError("本科 main.tex 缺少唯一章节插槽。")
        start = text.index(CHAPTERS_BEGIN)
        end = text.index(CHAPTERS_END, start) + len(CHAPTERS_END)
        block = "\n".join([CHAPTERS_BEGIN, *[rf"\input{{{item}}}" for item in references], CHAPTERS_END])
        main_tex_path.write_text(text[:start] + block + text[end:], encoding="utf-8")
        return references

    def update_bibliography(self, main_tex_path: str | Path, mode: str) -> None:
        """只在 staging 的受控插槽写入本科模板所需参考文献后端。"""
        main_tex = Path(main_tex_path).resolve()
        text = main_tex.read_text(encoding="utf-8")
        if text.count(BIBLIOGRAPHY_BEGIN) != 1 or text.count(BIBLIOGRAPHY_END) != 1:
            raise TemplateAdapterError("本科 main.tex 缺少唯一参考文献插槽。")
        start = text.index(BIBLIOGRAPHY_BEGIN)
        end = text.index(BIBLIOGRAPHY_END, start) + len(BIBLIOGRAPHY_END)
        if mode == "word":
            lines = [BIBLIOGRAPHY_BEGIN, r"\input{data/references.tex}", BIBLIOGRAPHY_END]
        elif mode == "none":
            lines = [BIBLIOGRAPHY_BEGIN, BIBLIOGRAPHY_END]
        elif mode == "bibtex":
            raise TemplateAdapterError("本科固定模板当前未声明 BibTeX 后端能力。")
        else:
            raise TemplateAdapterError(f"未知参考文献后端：{mode}")
        main_tex.write_text(text[:start] + "\n".join(lines) + text[end:], encoding="utf-8")

    def validate_chapter_includes(self, main_tex_path: Path, chapter_files: list[str]) -> list[str]:
        expected = [PurePosixPath(item).with_suffix("").as_posix() for item in chapter_files]
        actual = _INPUT_PATTERN.findall(strip_latex_comments(main_tex_path.read_text(encoding="utf-8")))
        if actual != expected:
            raise TemplateAdapterError(f"本科章节入口不一致：expected={expected}, actual={actual}")
        return actual
