# -*- coding: utf-8 -*-
"""双 OUC 当前模板注册表与复制入口测试。"""

from __future__ import annotations

import json

import pytest

from adapters.ouc import OUCTemplateAdapter
from adapters.ouc_bachelor import OUCBachelorTemplateAdapter
from template_registry.models import CompileRecipe, TemplateManifestError
from template_registry.registry import (
    load_builtin_template_registry,
    resolve_template,
)
from tests.support.paths import REPO_ROOT
from tests.support.template_source import source_snapshot


def test_builtin_registry_only_contains_two_current_templates() -> None:
    registry = load_builtin_template_registry(REPO_ROOT)
    assert [item.template_id for item in registry.templates()] == [
        "ouc-bachelor", "ouc-graduate",
    ]
    assert isinstance(resolve_template(registry, "ouc-graduate").adapter, OUCTemplateAdapter)
    assert isinstance(resolve_template(registry, "ouc-bachelor").adapter, OUCBachelorTemplateAdapter)
    with pytest.raises(TemplateManifestError):
        resolve_template(registry, "ouc")


def test_materialize_copies_current_source_without_modifying_it(tmp_path) -> None:
    registry = load_builtin_template_registry(REPO_ROOT)
    resolved = resolve_template(registry, "ouc-bachelor")
    source = registry.source_root(resolved.manifest)
    before = source_snapshot(source)
    target = resolved.materialize(REPO_ROOT, tmp_path / "staging")
    assert source_snapshot(target) == before
    assert source_snapshot(source) == before


def test_container_manifests_have_no_history_or_fingerprint_fields() -> None:
    registry = load_builtin_template_registry(REPO_ROOT)
    forbidden = {
        "version", "active_version", "registration_status", "source_fingerprint",
        "derivation", "history", "patches",
    }
    for manifest in registry.templates():
        raw = json.loads(manifest.manifest_path.read_text(encoding="utf-8"))
        assert not forbidden.intersection(raw)


def test_compile_recipe_rejects_unsafe_or_conflicting_values() -> None:
    base = {
        "entrypoint": "main.tex", "engine": "xelatex", "engine_runs": 2,
        "bibliography_backend": "none", "bibliography_policy": "disabled",
        "artifact_stem": "main", "auxiliary_directories": ["includes"],
    }
    assert CompileRecipe.from_dict(base).engine == "xelatex"
    with pytest.raises(TemplateManifestError):
        CompileRecipe.from_dict({**base, "engine": "powershell"})
    with pytest.raises(TemplateManifestError):
        CompileRecipe.from_dict({**base, "entrypoint": "../main.tex"})
    with pytest.raises(TemplateManifestError):
        CompileRecipe.from_dict({
            **base,
            "bibliography_backend": "bibtex",
            "bibliography_policy": "disabled",
        })
