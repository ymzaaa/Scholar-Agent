# -*- coding: utf-8 -*-
"""P2：同一 W01 在研究生、本科模板中的首版 PDF 验收。"""

from __future__ import annotations

import json
import hashlib
from pathlib import Path
import re
import shutil
import subprocess

import pytest
from pypdf import PdfReader

from tests.support.paths import REPO_ROOT
from tests.support.fake_llm import FakeLLMClient

from pipeline.minimal_registered_generation import run_minimal_registered_generation
from pipeline.source_clone import clone_latex_source
from pipeline.workspace import WorkspaceManager
from template_registry.registry import resolve_template
from template_registry.registry import load_builtin_template_registry
from stage1.pipeline_api import PipelineRunner


@pytest.mark.parametrize("template_id", ["ouc-graduate", "ouc-bachelor"])
def test_w01_generates_degraded_but_publishable_pdf_and_rebuildable_source(
    tmp_path: Path, template_id: str
) -> None:
    registry = load_builtin_template_registry(REPO_ROOT)
    output = tmp_path / template_id
    client = FakeLLMClient()
    report = run_minimal_registered_generation(
        runner=PipelineRunner(), resolved_template=resolve_template(registry, template_id),
        repository_root=REPO_ROOT,
        docx_path=REPO_ROOT / "tests/fixtures/word/W01_minimal_body.docx",
        output_directory=output,
        translation_client=client,
    )
    assert report["published"] is True
    assert report["quality_status"] == "degraded"
    assert all(report["gates"].values())
    assert report["content"]["role_counts"] == {
        "chapter_heading": 1, "body_paragraph": 3, "section_heading": 1,
    }
    assert len(PdfReader(str(output / "main.pdf")).pages) >= 1
    assert (output / "latex-source.zip").is_file()
    stored = json.loads((output / "reports/p2-report.json").read_text(encoding="utf-8"))
    assert stored["template"]["template_id"] == template_id
    if template_id == "ouc-bachelor":
        assert client.calls == 0
        assert report["llm_tasks"][0]["status"] == "skipped_not_needed"
        assert report["source_package"]["includes_sty"] is True
        assert report["source_package"]["includes_eps"] is True
        assert not (output / "openingreport.tex").exists()
    else:
        assert client.calls == 0
        assert report["llm_tasks"][0]["status"] == "disabled"
        chapter = (output / "contents" / "section_01.tex").read_text(encoding="utf-8")
        assert r"\enchapter{EN-0}" not in chapter
        assert r"\ensection{EN-1}" not in chapter
        cover = (output / "data" / "cover.tex").read_text(encoding="utf-8")
        assert "学校代码：10423" in cover and "ScholarStudentId" in cover

    # 源码包在全新 staging 中按正式 Manifest 重建，禁止借用原编译缓存。
    resolved = resolve_template(registry, template_id)
    workspace = WorkspaceManager(tmp_path / "rebuild-workspace")
    project = workspace.create_project()
    with workspace.begin_generation(project.project_id) as rebuilt:
        archive = output / "latex-source.zip"
        cloned = clone_latex_source(archive, rebuilt.staging_dir,
            expected_size=archive.stat().st_size, expected_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
            report_path=tmp_path / "rebuild-clone.json", parent_version_id="published-fixture",
            candidate_version_id=rebuilt.generation_id)
        assert cloned["file_count"] > 0
        assert not (rebuilt.staging_dir / "main.pdf").exists()
        result = PipelineRunner(resolved_template=resolved).compile(str(rebuilt.staging_dir))
        assert result.success, result.raw_error
        assert all(item["passed"] for item in result.gates.values())
        assert len(PdfReader(str(result.pdf_path)).pages) >= 1


def test_graduate_cover_right_labels_share_the_same_left_edge(tmp_path: Path) -> None:
    pdftotext = shutil.which("pdftotext")
    if pdftotext is None:
        pytest.skip("pdftotext 不可用，跳过 PDF 坐标检查。")
    registry = load_builtin_template_registry(REPO_ROOT)
    output = tmp_path / "graduate-cover"
    run_minimal_registered_generation(
        runner=PipelineRunner(),
        resolved_template=resolve_template(registry, "ouc-graduate"),
        repository_root=REPO_ROOT,
        docx_path=REPO_ROOT / "tests/fixtures/word/W01_minimal_body.docx",
        output_directory=output,
        translation_client=FakeLLMClient(),
    )
    bbox = tmp_path / "cover-bbox.html"
    subprocess.run(
        [pdftotext, "-f", "1", "-l", "1", "-bbox-layout", str(output / "main.pdf"), str(bbox)],
        check=True, capture_output=True,
    )
    html = bbox.read_text(encoding="utf-8")
    school = re.search(r'<word xMin="([0-9.]+)"[^>]*>学校代码：10423</word>', html)
    student = re.search(
        r'<word xMin="([0-9.]+)" yMin="([0-9.]+)"[^>]*>学</word>', html
    )
    assert school and student
    assert float(student.group(2)) < 120
    assert abs(float(school.group(1)) - float(student.group(1))) <= 0.5
