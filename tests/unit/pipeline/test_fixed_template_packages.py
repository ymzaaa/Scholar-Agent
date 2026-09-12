# -*- coding: utf-8 -*-
"""两个当前模板的固定目录、分发包与不变性契约。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
from zipfile import ZipFile

from template_registry.registry import load_builtin_template_registry
from tests.support.paths import (
    BACHELOR_TEMPLATE, GRADUATE_TEMPLATE, REPO_ROOT, TEMPLATE_ROOT,
)


##### 固定目录板块 #####


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_only_two_current_template_containers_are_exposed() -> None:
    containers = sorted(path.name for path in TEMPLATE_ROOT.iterdir() if path.is_dir())
    assert containers == ["OUC本科生", "OUC研究生"]
    assert GRADUATE_TEMPLATE.is_dir() and BACHELOR_TEMPLATE.is_dir()


def test_container_manifests_point_only_to_current_sources() -> None:
    registry = load_builtin_template_registry(REPO_ROOT)
    for template_id, container, source in (
        ("ouc-graduate", TEMPLATE_ROOT / "OUC研究生", GRADUATE_TEMPLATE),
        ("ouc-bachelor", TEMPLATE_ROOT / "OUC本科生", BACHELOR_TEMPLATE),
    ):
        frozen = json.loads(
            (container / "TEMPLATE-MANIFEST.json").read_text(encoding="utf-8")
        )
        registered = registry.get(template_id)
        assert registered.source_path.endswith(f"{container.name}/source")
        assert source.is_dir()
        assert "version" not in frozen
        assert "source_fingerprint" not in frozen


def test_runtime_sources_are_separated_from_downloadable_examples() -> None:
    graduate = TEMPLATE_ROOT / "OUC研究生"
    bachelor = TEMPLATE_ROOT / "OUC本科生"

    assert not list((graduate / "source" / "contents").glob("section_*.tex"))
    assert len(list((graduate / "example" / "contents").glob("section_*.tex"))) == 6
    assert not list((bachelor / "source" / "includes").glob("section_*.tex"))
    assert len(list((bachelor / "example" / "includes").glob("section_*.tex"))) == 2

    for relative in ("oucthesis.cls", "oucauthoryear.bst"):
        assert _sha256(graduate / "source" / relative) == _sha256(
            graduate / "example" / relative
        )
    assert _sha256(bachelor / "source" / "oucart.cls") == _sha256(
        bachelor / "example" / "oucart.cls"
    )


##### 研究生分发包板块 #####


def test_graduate_distribution_zip_is_hash_locked_and_contains_safe_examples() -> None:
    container = TEMPLATE_ROOT / "OUC研究生"
    archive = container / "OUC研究生固定模板.zip"
    assert archive.is_file() and archive.stat().st_size > 0

    with ZipFile(archive) as bundle:
        names = {PurePosixPath(name).as_posix() for name in bundle.namelist()}
    assert {"main.tex", "oucthesis.cls", "data/cover.tex"}.issubset(names)
    assert {
        "reference-contents/chapter-example.tex",
        "reference-contents/figure-table-example.tex",
        "reference-contents/formula-citation-example.tex",
    }.issubset(names)
    assert not any(".." in PurePosixPath(name).parts for name in names)
    assert not any(name.endswith((".aux", ".log", ".pdf", ".toc")) for name in names)
