# -*- coding: utf-8 -*-
"""双 OUC 当前模板的隔离编译验收。"""

from __future__ import annotations

from pathlib import Path
from zipfile import ZipFile

import pytest

from pipeline.compiler import compile_project
from template_registry.baseline import run_template_baseline
from template_registry.registry import load_builtin_template_registry
from tests.support.paths import REPO_ROOT, TEMPLATE_ROOT
from tests.support.template_source import source_snapshot


@pytest.mark.parametrize("template_id", ["ouc-graduate", "ouc-bachelor"])
def test_current_template_baseline_compiles_in_isolation(
    tmp_path: Path, template_id: str,
) -> None:
    registry = load_builtin_template_registry(REPO_ROOT)
    manifest = registry.get(template_id)
    source = registry.source_root(manifest)
    before = source_snapshot(source)
    report = run_template_baseline(
        manifest, REPO_ROOT, tmp_path / template_id / "current",
        timeout_seconds=120,
    )
    assert report["passed"] is True
    assert report["compile"]["success"] is True
    assert source_snapshot(source) == before


def test_current_templates_have_distinct_education_levels() -> None:
    registry = load_builtin_template_registry(REPO_ROOT)
    assert registry.get("ouc-graduate").education_level == "graduate"
    assert registry.get("ouc-bachelor").education_level == "undergraduate"


def test_graduate_distribution_zip_compiles_in_isolation(tmp_path: Path) -> None:
    registry = load_builtin_template_registry(REPO_ROOT)
    archive = TEMPLATE_ROOT / "OUC研究生" / "OUC研究生固定模板.zip"
    destination = tmp_path / "graduate-zip"
    destination.mkdir()
    with ZipFile(archive) as bundle:
        assert all(".." not in Path(name).parts for name in bundle.namelist())
        bundle.extractall(destination)
    result = compile_project(
        destination, registry.get("ouc-graduate").compile_recipe,
        timeout_seconds=120,
    )
    assert result.success is True
    assert (destination / "main.pdf").stat().st_size > 0
