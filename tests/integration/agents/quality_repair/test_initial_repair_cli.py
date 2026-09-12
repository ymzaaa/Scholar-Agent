# -*- coding: utf-8 -*-
"""首次统一修复与 CLI、工作区和完整终验的虚拟集成测试。"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from llm.policy import HEADING_TRANSLATION_TASK, LLMTaskResult
from pipeline.results import ProcessResult
from models.render_trace import RenderRecord
import run_pipeline
from adapters.ouc import OUCTemplateAdapter
from pipeline.structural_gate import FatalChapterReachability


pytestmark = pytest.mark.agent


##### CLI修复链路板块 #####


def test_cli_repairs_unclosed_safe_environment_and_publishes(tmp_path: Path) -> None:
    calls = {"compile": 0}

    class FakeRunner:
        def __init__(self, *args, **kwargs):
            self.template_adapter = OUCTemplateAdapter()

        def extract(self, _path):
            return SimpleNamespace(
                paragraphs=[], tables=[], word_structure={"paragraphs": [], "tables": []}
            )

        def recognize(self, _extract):
            return SimpleNamespace(review={"chapters": [], "formula_review": {"status": "passed"}}, internal={})

        def render(self, *_args):
            output = Path(_args[3])
            (output / "main.tex").write_text(
                "\\begin{document}\n\\input{contents/section_01}\n\\end{document}\n",
                encoding="utf-8",
            )
            (output / 'contents').mkdir(exist_ok=True)
            (output / 'contents/section_01.tex').write_text(
                '% SCHOLAR_UNIT_BEGIN u-000001 body_paragraph\n'
                '\\begin{center}\nX\n% SCHOLAR_UNIT_END u-000001\n', encoding='utf-8')
            record = RenderRecord(record_id='r-000001', marker_unit_id='u-000001',
                source_unit_ids=['u-000001'], source_order=0, role='body_paragraph', status='rendered',
                target_file='contents/section_01.tex', output_order=0)
            return SimpleNamespace(
                stats={}, chapter_files=['contents/section_01.tex'],
                render_trace={'records': [record.to_dict()]}, fidelity_report={}
            )

        def compile(self, output):
            calls["compile"] += 1
            path = Path(output) / "contents/section_01.tex"
            content = path.read_text(encoding="utf-8")
            passed = content.count(r"\begin{center}") == content.count(r"\end{center}")
            process = ProcessResult(
                "xelatex#1",
                ["xelatex"],
                0 if passed else 1,
                0.01,
                stdout_tail=('' if passed else
                    r'contents/section_01.tex:3: \begin{center} on input line 2 ended by \end{document}.'),
            )
            return SimpleNamespace(
                success=passed,
                pdf_path=str(Path(output) / "main.pdf") if passed else "",
                errs=0 if passed else 1,
                raw_error="" if passed else "xelatex#1: return_code=1",
                started_at_ns=1,
                finished_at_ns=2,
                process_results=[process],
                gates={"processes_passed": {"passed": passed}},
                warnings=[],
            )

        def check_format(self, _output):
            return SimpleNamespace(
                all_passed=True,
                publish_allowed=True,
                quality_status="passed",
                fails=[],
                degradations=[],
                counts={},
                template_profile={},
                results={},
            )

    workspace = tmp_path / "workspace"
    argv = [
        "run_pipeline.py",
        "--docx", "dummy.docx",
        "--template-id", "ouc-graduate",
        "--workspace-root", str(workspace),
    ]
    with (
        patch.object(sys, "argv", argv),
        patch.object(run_pipeline, "PipelineRunner", FakeRunner),
        patch("pipeline.confirmed_structure.resolve_render_structure", side_effect=lambda _extracted, recognized: recognized),
        patch(
            "pipeline.translate.translate_headings_for_toc",
            return_value=LLMTaskResult.disabled(HEADING_TRANSLATION_TASK),
        ),
        patch(
            "pipeline.validation_runner.build_content_fidelity_report",
            return_value={"all_passed": True, "blocking_or_unimplemented": []},
        ),
        patch("pipeline.validation_runner.write_fidelity_artifacts", return_value={}),
        patch("pipeline.validation_runner.structural_fidelity_gate", return_value={}),
    ):
        exit_code = run_pipeline.main()

    report_path = next(workspace.glob("projects/*/reports/*/pipeline_log.json"))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    output = Path(report["output_dir"])
    assert exit_code == 0
    # 初始门禁、动作局部观察、完整终验分别执行，成功后不询问模型。
    assert calls["compile"] == 3
    assert report["agent_run"]["status"] == "ready"
    assert report['agent_run']['planning_round'] == 0
    assert report["published"] is True
    assert r"\end{center}" in (output / "contents/section_01.tex").read_text(encoding="utf-8")
