# -*- coding: utf-8 -*-
"""G1-A 编译进程、PDF、日志、新鲜度和引用门禁测试。"""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from tests.support.paths import STAGE1_DIR


##### 测试环境板块 #####


from pipeline.compiler import (
    compile_project,
    run_compile_process,
    validate_auxiliary_artifacts,
    validate_log_artifact,
    validate_pdf_artifact,
)
from template_registry.models import CompileRecipe


def _recipe(**overrides) -> CompileRecipe:
    values = {
        "entrypoint": "main.tex", "engine": "xelatex", "engine_runs": 3,
        "bibliography_backend": "bibtex",
        "bibliography_policy": "auto_if_cited", "artifact_stem": "main",
        "auxiliary_directories": ["contents", "data"],
    }
    values.update(overrides)
    return CompileRecipe.from_dict(values)


def _write_pdf(path: Path) -> None:
    path.write_bytes(b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n%%EOF\n")


def _write_complete_log(path: Path, extra: str = "") -> None:
    path.write_text(
        "Output written on main.pdf (1 page).\n"
        f"{extra}\n"
        "PDF statistics:\n",
        encoding="utf-8",
    )


class _FakeProcessRunner:
    """按模式注入编译进程行为，不依赖外部 LaTeX。"""

    def __init__(self, mode: str = "success") -> None:
        self.mode = mode
        self.calls = 0

    def __call__(self, command, **kwargs):
        self.calls += 1
        cwd = Path(kwargs["cwd"])
        if self.mode == "missing_tool":
            raise FileNotFoundError(command[0])
        if self.mode == "timeout":
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        if self.mode == "nonzero":
            return subprocess.CompletedProcess(command, 1, "", "fatal")
        if self.mode != "no_artifacts":
            (cwd / "main.aux").write_text("\\relax\n", encoding="utf-8")
            _write_pdf(cwd / "main.pdf")
            extra = "LaTeX Warning: There were undefined references." if self.mode == "unresolved" else ""
            _write_complete_log(cwd / "main.log", extra)
        return subprocess.CompletedProcess(command, 0, "ok", "")


def _parser_ok(_path: Path) -> tuple[bool, str]:
    return True, "测试解析器确认有效。"


##### 子进程门禁板块 #####


class ProcessGateTests(unittest.TestCase):
    def test_nonzero_return_code_blocks(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            result = run_compile_process(
                "xelatex", ["xelatex", "main.tex"], Path(temp_dir), 1,
                _FakeProcessRunner("nonzero"),
            )
            self.assertEqual(result.return_code, 1)
            self.assertFalse(result.timed_out)

    def test_timeout_and_missing_tool_are_explicit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            timeout = run_compile_process(
                "xelatex", ["xelatex"], Path(temp_dir), 1,
                _FakeProcessRunner("timeout"),
            )
            missing = run_compile_process(
                "xelatex", ["missing-xelatex"], Path(temp_dir), 1,
                _FakeProcessRunner("missing_tool"),
            )
        self.assertTrue(timeout.timed_out)
        self.assertFalse(timeout.output_complete)
        self.assertTrue(missing.missing_tool)
        self.assertFalse(missing.output_complete)


##### 产物门禁板块 #####


class ArtifactGateTests(unittest.TestCase):
    def test_pdf_exists_nonempty_fresh_and_parseable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "main.pdf"
            started = time.time_ns()
            _write_pdf(path)
            gates = validate_pdf_artifact(path, started, pdf_parser=_parser_ok)
        self.assertTrue(all(gate["passed"] for gate in gates.values()))

    def test_stale_and_invalid_pdf_are_blocked(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "main.pdf"
            path.write_bytes(b"not a pdf")
            os.utime(path, (1, 1))
            gates = validate_pdf_artifact(path, time.time_ns(), pdf_parser=_parser_ok)
        self.assertFalse(gates["pdf_fresh"]["passed"])
        self.assertFalse(gates["pdf_signature"]["passed"])
        self.assertFalse(gates["pdf_parseable"]["passed"])

    def test_truncated_log_and_unresolved_reference_are_blocked(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "main.log"
            started = time.time_ns()
            path.write_text(
                "Output written on main.pdf.\n"
                "LaTeX Warning: There were undefined references.\n",
                encoding="utf-8",
            )
            gates = validate_log_artifact(path, started)
        self.assertFalse(gates["log_complete"]["passed"])
        self.assertFalse(gates["references_resolved"]["passed"])

    def test_bibtex_keys_must_resolve(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            started = time.time_ns()
            (root / "main.aux").write_text(
                "\\bibstyle{plain}\n\\bibdata{cite}\n"
                "\\citation{known,missing}\n\\bibcite{known}{1}\n",
                encoding="utf-8",
            )
            (root / "main.bbl").write_text("\\begin{thebibliography}{1}", encoding="utf-8")
            gates = validate_auxiliary_artifacts(root, started)
        self.assertFalse(gates["bibliography_complete"]["passed"])
        self.assertIn("missing", gates["bibliography_complete"]["detail"])

    def test_declared_bibliography_without_citations_does_not_require_bbl(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            started = time.time_ns()
            (root / "main.aux").write_text(
                "\\bibstyle{plain}\n\\bibdata{cite}\n", encoding="utf-8"
            )
            gates = validate_auxiliary_artifacts(root, started)
        self.assertTrue(gates["bibliography_complete"]["passed"])
        self.assertIn("没有实际引用", gates["bibliography_complete"]["detail"])

    def test_citations_in_nested_aux_require_bibliography_resolution(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            started = time.time_ns()
            (root / "main.aux").write_text(
                "\\bibstyle{plain}\n\\bibdata{cite}\n", encoding="utf-8"
            )
            contents = root / "contents"
            contents.mkdir()
            (contents / "section_01.aux").write_text(
                "\\citation{nested}\n", encoding="utf-8"
            )
            gates = validate_auxiliary_artifacts(root, started)
        self.assertFalse(gates["bibliography_complete"]["passed"])


##### 统一结果板块 #####


class CompileProjectTests(unittest.TestCase):
    def _project(self, root: Path) -> None:
        (root / "main.tex").write_text(
            "\\documentclass{article}\\begin{document}x\\end{document}",
            encoding="utf-8",
        )

    def test_all_gates_pass(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._project(root)
            (root / "contents").mkdir()
            old_nested_aux = root / "contents" / "section_01.aux"
            old_nested_aux.write_text("stale", encoding="utf-8")
            runner = _FakeProcessRunner("success")
            result = compile_project(root, _recipe(), process_runner=runner, pdf_parser=_parser_ok)
            self.assertFalse(old_nested_aux.exists())
        self.assertTrue(result.success)
        self.assertEqual(len(result.process_results), 3)
        self.assertTrue(all('-file-line-error' in process.command for process in result.process_results))
        self.assertTrue(result.gates["processes_passed"]["passed"])

    def test_old_pdf_cannot_mask_missing_new_artifact(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._project(root)
            _write_pdf(root / "main.pdf")
            result = compile_project(
                root, _recipe(),
                process_runner=_FakeProcessRunner("no_artifacts"),
                pdf_parser=_parser_ok,
            )
        self.assertFalse(result.success)
        self.assertFalse(result.gates["pdf_exists"]["passed"])

    def test_process_and_reference_failures_block_success(self):
        for mode in ("nonzero", "timeout", "missing_tool", "unresolved"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                self._project(root)
                result = compile_project(
                    root, _recipe(),
                    process_runner=_FakeProcessRunner(mode),
                    pdf_parser=_parser_ok,
                )
                self.assertFalse(result.success)

    def test_recipe_controls_entrypoint_engine_runs_and_auxiliary_directory(self):
        class RecipeRunner:
            def __init__(self):
                self.commands = []

            def __call__(self, command, **kwargs):
                self.commands.append(command)
                cwd = Path(kwargs["cwd"])
                (cwd / "thesis.aux").write_text("\\relax\n", encoding="utf-8")
                _write_pdf(cwd / "thesis.pdf")
                (cwd / "thesis.log").write_text(
                    "Output written on thesis.pdf (1 page).\nPDF statistics:\n",
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(command, 0, "ok", "")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "thesis.tex").write_text(
                "\\documentclass{article}\\begin{document}x\\end{document}",
                encoding="utf-8",
            )
            includes = root / "includes"
            includes.mkdir()
            stale = includes / "old.aux"
            stale.write_text("old", encoding="utf-8")
            runner = RecipeRunner()
            result = compile_project(
                root, _recipe(
                    entrypoint="thesis.tex", engine="lualatex", engine_runs=2,
                    artifact_stem="thesis", auxiliary_directories=["includes"],
                ),
                process_runner=runner,
                pdf_parser=_parser_ok,
            )
            self.assertFalse(stale.exists())
        self.assertTrue(result.success)
        self.assertEqual(len(result.process_results), 2)
        self.assertTrue(all(command[0] == "lualatex" for command in runner.commands))

    def test_biber_backend_runs_between_latex_passes(self):
        class BiberRunner:
            def __init__(self):
                self.commands = []

            def __call__(self, command, **kwargs):
                self.commands.append(command)
                root = Path(kwargs["cwd"])
                (root / "main.aux").write_text("\\relax\n", encoding="utf-8")
                if command[0] == "xelatex":
                    (root / "main.bcf").write_text(
                        "<bcf:controlfile><bcf:citekey>alpha</bcf:citekey>"
                        "</bcf:controlfile>",
                        encoding="utf-8",
                    )
                    _write_pdf(root / "main.pdf")
                    _write_complete_log(root / "main.log")
                if command[0] == "biber":
                    (root / "main.bbl").write_text(
                        "\\entry{alpha}{article}{}", encoding="utf-8"
                    )
                return subprocess.CompletedProcess(command, 0, "ok", "")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._project(root)
            runner = BiberRunner()
            result = compile_project(
                root, _recipe(bibliography_backend="biber"), process_runner=runner,
                pdf_parser=_parser_ok,
            )

        self.assertTrue(result.success)
        self.assertEqual(
            [command[0] for command in runner.commands],
            ["xelatex", "biber", "xelatex", "xelatex"],
        )

    def test_missing_biber_blocks_compile(self):
        class MissingBiberRunner:
            def __call__(self, command, **kwargs):
                root = Path(kwargs["cwd"])
                if command[0] == "biber":
                    raise FileNotFoundError("biber")
                (root / "main.aux").write_text("\\relax\n", encoding="utf-8")
                (root / "main.bcf").write_text(
                    "<bcf:citekey>alpha</bcf:citekey>", encoding="utf-8"
                )
                _write_pdf(root / "main.pdf")
                _write_complete_log(root / "main.log")
                return subprocess.CompletedProcess(command, 0, "ok", "")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._project(root)
            result = compile_project(
                root, _recipe(bibliography_backend="biber"),
                process_runner=MissingBiberRunner(), pdf_parser=_parser_ok,
            )

        self.assertFalse(result.success)
        self.assertTrue(result.process_results[-1].missing_tool)

    def test_disabled_bibliography_requires_none_backend(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._project(root)
            with self.assertRaises(Exception):
                _recipe(bibliography_backend="bibtex", bibliography_policy="disabled")


if __name__ == "__main__":
    unittest.main(verbosity=2)
