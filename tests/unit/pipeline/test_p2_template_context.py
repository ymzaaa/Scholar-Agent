# -*- coding: utf-8 -*-
"""P2 模板运行上下文和最小元数据解析测试。"""

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.support.paths import REPO_ROOT

from pipeline.metadata_resolution import PLACEHOLDER, resolve_minimal_metadata
from pipeline.minimal_registered_generation import (
    _example_content_absent, _expected_role_counts, _prepare_template,
)
from pipeline.metadata_resolution import MetadataResolution
from template_registry.registry import resolve_builtin_template, resolve_template
from template_registry.models import TemplateManifestError
from template_registry.registry import load_builtin_template_registry


def test_available_templates_bind_distinct_adapters() -> None:
    registry = load_builtin_template_registry(REPO_ROOT)
    graduate = resolve_template(registry, "ouc-graduate")
    bachelor = resolve_template(registry, "ouc-bachelor")
    assert graduate.adapter.name == "ouc-graduate"
    assert bachelor.adapter.name == "ouc-bachelor"
    with pytest.raises(TemplateManifestError):
        resolve_template(registry, "ouc")


def test_feedback_template_resolution_rejects_unknown_template() -> None:
    graduate = resolve_builtin_template("ouc-graduate")
    bachelor = resolve_builtin_template("ouc-bachelor")
    assert graduate.manifest.template_id == "ouc-graduate"
    assert type(graduate.adapter).__name__ == "OUCTemplateAdapter"
    assert bachelor.manifest.template_id == "ouc-bachelor"
    assert type(bachelor.adapter).__name__ == "OUCBachelorTemplateAdapter"
    with pytest.raises(TemplateManifestError, match="未注册模板"):
        resolve_builtin_template("unknown-school")


def test_w01_metadata_uses_title_style_and_consolidated_placeholders() -> None:
    from stage1.pipeline_api import PipelineRunner

    path = REPO_ROOT / "tests" / "fixtures" / "word" / "W01_minimal_body.docx"
    extracted = PipelineRunner().extract(str(path))
    graduate_adapter = resolve_builtin_template("ouc-graduate").adapter
    bachelor_adapter = resolve_builtin_template("ouc-bachelor").adapter
    graduate = resolve_minimal_metadata(
        extracted.paragraphs,
        required_fields=graduate_adapter.required_metadata_fields,
    )
    bachelor = resolve_minimal_metadata(
        extracted.paragraphs,
        required_fields=bachelor_adapter.required_metadata_fields,
    )
    assert graduate.values["title"] == "合成论文测试：最小正文"
    assert {"author", "student_id", "advisor"}.issubset(
        graduate.missing_fields
    )
    assert {
        "classification", "udc", "title_en", "degree_type", "major",
        "research_direction", "completion_date",
    }.issubset(graduate.missing_fields)
    assert all(graduate.values[field] == PLACEHOLDER for field in graduate.missing_fields)
    assert {"major", "title_en"}.issubset(bachelor.missing_fields)


def test_restored_graduate_cover_owns_layout_without_sample_values() -> None:
    registry = load_builtin_template_registry(REPO_ROOT)
    root = registry.source_root(registry.get("ouc-graduate"))
    cover = (root / "data" / "cover.tex").read_text(encoding="utf-8")
    assert "分类号：" in cover and "学校代码：10423" in cover
    assert "UDC：" in cover and "学 ~~~~~~~~ 号：" in cover
    assert "0.66\\textwidth" in cover and "0.34\\textwidth" in cover
    assert "210pt" not in cover and "229.1pt" not in cover
    assert "张三" not in cover and "北大西洋" not in cover


def test_formal_cover_generation_only_writes_template_metadata(tmp_path: Path) -> None:
    from stage1.pipeline_api import generate_cover

    registry = load_builtin_template_registry(REPO_ROOT)
    resolved = resolve_template(registry, "ouc-graduate")
    source = registry.source_root(resolved.manifest)
    target = tmp_path / "graduate"
    shutil.copytree(source, target)
    original_cover = (target / "data" / "cover.tex").read_text(encoding="utf-8")

    paragraphs = [
        {"text": "分类号：TP_3", "style": "Normal"},
        {"text": "UDC：004", "style": "Normal"},
        {"text": "学号：20260001", "style": "Normal"},
        {"text": "中文题目 & 模板保真", "style": "Title"},
        {"text": "Template-Preserving Graduate Thesis Cover", "style": "Normal"},
    ]
    tables = [{"data": [
        ["作者", "", "张_三"],
        ["指导教师", "", "李老师"],
        ["学位类型", "", "学术学位"],
        ["专业名称", "", "计算机科学与技术"],
        ["研究方向", "", "智能文档处理"],
        ["完成日期", "", "二〇二六年八月"],
    ]}]
    generate_cover(paragraphs, tables, target, resolved.adapter)

    assert (target / "data" / "cover.tex").read_text(encoding="utf-8") == original_cover
    metadata = (target / "data" / "metadata.tex").read_text(encoding="utf-8")
    assert r"\newcommand{\ScholarClassification}{TP\_3}" in metadata
    assert r"\newcommand{\ScholarAuthor}{张\_三}" in metadata
    assert r"\newcommand{\ScholarTitleZh}{中文题目 \& 模板保真}" in metadata
    assert "210pt" not in metadata and "229.1pt" not in metadata


def test_bachelor_heading_mapping_preserves_logical_levels() -> None:
    registry = load_builtin_template_registry(REPO_ROOT)
    adapter = resolve_template(registry, "ouc-bachelor").adapter
    assert adapter.render_heading("chapter", "绪论", "u-1")["primary"] == r"\section{绪论}"
    assert adapter.render_heading("section", "目的", "u-2")["primary"] == r"\subsection{目的}"


def test_expected_roles_are_derived_from_confirmed_structure() -> None:
    extracted = SimpleNamespace(paragraphs=[
        {"text": "第一章 绪论", "style": "Heading 1"},
        {"text": "正文甲", "style": "Normal"},
        {"text": "1.1 背景", "style": "Heading 2"},
        {"text": "正文乙", "style": "Normal"},
        {"text": "第二章 方法", "style": "Heading 1"},
        {"text": "正文丙", "style": "Normal"},
    ])
    for index, paragraph in enumerate(extracted.paragraphs):
        paragraph.update(unit_id=f"u-{index:06d}", block_index=index)
    recognized = SimpleNamespace(review={"heading_candidates": [
        {"unit_id": "u-000000", "level": "chapter", "title": "绪论"},
        {"unit_id": "u-000002", "level": "section", "title": "背景"},
        {"unit_id": "u-000004", "level": "chapter", "title": "方法"},
    ], "object_bindings": [], "chapters": [
        {"num": 1, "range": [0, 4]}, {"num": 2, "range": [4, 6]},
    ]})
    assert _expected_role_counts(extracted, recognized) == {
        "chapter_heading": 2, "section_heading": 1, "body_paragraph": 3,
    }


def test_non_ouc_staging_and_metadata_contract_needs_no_template_branch(
    tmp_path: Path,
) -> None:
    class Adapter:
        entrypoint = "thesis.tex"
        content_directory = "body"
        required_metadata_fields = ("title", "candidate")

        def prepare_generation_staging(self, root, metadata):
            (root / self.content_directory).mkdir()
            (root / self.entrypoint).write_text(
                f"TITLE={metadata['title']}\nCANDIDATE={metadata['candidate']}",
                encoding="utf-8",
            )

        def validate_generated_staging(self, root, chapter_files, bibliography_mode):
            return (
                bibliography_mode == "none"
                and chapter_files == ["body/chapter_a.tex"]
                and (root / self.entrypoint).is_file()
            )

    adapter = Adapter()
    metadata = MetadataResolution(
        {"title": "合成标题", "candidate": PLACEHOLDER}, (), ("candidate",),
    )
    content_directory = _prepare_template(
        tmp_path, SimpleNamespace(adapter=adapter), metadata,
    )
    assert content_directory == "body"
    assert "CANDIDATE=【待填写】" in (tmp_path / "thesis.tex").read_text(encoding="utf-8")
    assert _example_content_absent(
        tmp_path, adapter, ["body/chapter_a.tex"], "none",
    ) is True
