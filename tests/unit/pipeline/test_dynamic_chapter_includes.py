# -*- coding: utf-8 -*-
"""G2-B 动态章节插槽与 main.tex 可达性门禁测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.support.paths import GRADUATE_TEMPLATE, REPO_ROOT


##### 测试环境板块 #####


from adapters.ouc import (
    CHAPTERS_BEGIN,
    CHAPTERS_END,
    OUCTemplateAdapter,
    TemplateAdapterError,
)
from pipeline.structural_gate import (
    FatalChapterReachability,
    validate_chapter_reachability,
)


def _create_project(root: Path, count: int, *, with_markers: bool = True):
    contents = root / "contents"
    contents.mkdir(parents=True)
    chapter_files = []
    for number in range(1, count + 1):
        relative = f"contents/section_{number:02d}.tex"
        (root / relative).write_text(
            f"\\chapter{{第{number}章}}\n正文{number}", encoding="utf-8"
        )
        chapter_files.append(relative)
    slot = f"{CHAPTERS_BEGIN}\nplaceholder\n{CHAPTERS_END}" if with_markers else "placeholder"
    main = root / "main.tex"
    main.write_text(
        "\\begin{document}\n" + slot + "\n\\end{document}\n",
        encoding="utf-8",
    )
    return main, contents, chapter_files


##### 动态数量板块 #####


class DynamicIncludeTests(unittest.TestCase):
    def test_one_seven_and_ten_chapters_preserve_order(self):
        for count in (1, 7, 10):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                main, contents, chapter_files = _create_project(root, count)
                adapter = OUCTemplateAdapter()
                references = adapter.update_chapter_includes(main, chapter_files)
                checks = validate_chapter_reachability(main, contents, chapter_files, template_adapter=OUCTemplateAdapter())
                text = main.read_text(encoding="utf-8")
                self.assertEqual(len(references), count)
                self.assertEqual(text.count("\\include{contents/section_"), count)
                self.assertEqual(text.count("\\insertblankpageFancy"), count - 1)
                self.assertEqual(checks["chapter_include_count"], (count, count))

    def test_real_ouc_template_has_exactly_one_controlled_slot(self):
        text = (GRADUATE_TEMPLATE / "main.tex").read_text(encoding="utf-8")
        self.assertEqual(text.count(CHAPTERS_BEGIN), 1)
        self.assertEqual(text.count(CHAPTERS_END), 1)
        outside = text.replace(
            text[text.index(CHAPTERS_BEGIN) : text.index(CHAPTERS_END) + len(CHAPTERS_END)],
            "",
        )
        self.assertNotIn("\\include{contents/section_", outside)


##### 故障注入板块 #####


class IncludeFailureTests(unittest.TestCase):
    def test_missing_or_duplicate_markers_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            main, _, chapter_files = _create_project(root, 1, with_markers=False)
            adapter = OUCTemplateAdapter()
            with self.assertRaises(TemplateAdapterError):
                adapter.update_chapter_includes(main, chapter_files)
            main.write_text(
                f"{CHAPTERS_BEGIN}\n{CHAPTERS_BEGIN}\n{CHAPTERS_END}", encoding="utf-8"
            )
            with self.assertRaises(TemplateAdapterError):
                adapter.update_chapter_includes(main, chapter_files)

    def test_missing_chapter_file_is_rejected_before_main_update(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            main, _, chapter_files = _create_project(root, 2)
            before = main.read_bytes()
            (root / chapter_files[1]).unlink()
            with self.assertRaises(TemplateAdapterError):
                OUCTemplateAdapter().update_chapter_includes(main, chapter_files)
            self.assertEqual(main.read_bytes(), before)

    def test_wrong_order_is_blocked_even_when_counts_match(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            main, contents, chapter_files = _create_project(root, 3)
            OUCTemplateAdapter().update_chapter_includes(main, chapter_files)
            text = main.read_text(encoding="utf-8")
            text = text.replace(
                "\\include{contents/section_01}",
                "\\include{contents/section_TMP}",
            ).replace(
                "\\include{contents/section_02}",
                "\\include{contents/section_01}",
            ).replace(
                "\\include{contents/section_TMP}",
                "\\include{contents/section_02}",
            )
            main.write_text(text, encoding="utf-8")
            with self.assertRaises(FatalChapterReachability):
                validate_chapter_reachability(main, contents, chapter_files, template_adapter=OUCTemplateAdapter())

    def test_orphan_chapter_file_is_blocked(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            main, contents, chapter_files = _create_project(root, 2)
            OUCTemplateAdapter().update_chapter_includes(main, chapter_files)
            (contents / "section_99.tex").write_text("\\chapter{孤立章}", encoding="utf-8")
            with self.assertRaises(FatalChapterReachability):
                validate_chapter_reachability(main, contents, chapter_files, template_adapter=OUCTemplateAdapter())


if __name__ == "__main__":
    unittest.main(verbosity=2)
