# -*- coding: utf-8 -*-
"""在临时副本中编译双 OUC 当前空白模板。"""

from __future__ import annotations

import json
import tempfile
from dataclasses import asdict
from pathlib import Path

from pipeline.compiler import compile_project

from .materialize import materialize_template
from .models import TemplateManifest


##### 当前基线板块 #####


def run_template_baseline(
    manifest: TemplateManifest, repository_root: str | Path,
    artifact_directory: str | Path, *, timeout_seconds: int = 120,
) -> dict:
    artifacts = Path(artifact_directory).resolve()
    artifacts.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"{manifest.template_id}-") as temporary:
        staging = materialize_template(manifest, repository_root, Path(temporary) / "staging")
        recipe = manifest.compile_recipe
        result = compile_project(
            staging, recipe,
            timeout_seconds=timeout_seconds,
        )
        pdf = staging / f"{recipe.artifact_stem}.pdf"
        log = staging / f"{recipe.artifact_stem}.log"
        if pdf.exists():
            (artifacts / "baseline.pdf").write_bytes(pdf.read_bytes())
        if log.exists():
            (artifacts / "compile.log").write_text(
                log.read_text(encoding="utf-8", errors="replace"), encoding="utf-8",
            )
    report = {
        "schema_version": "1.0.0", "template_id": manifest.template_id,
        "source_path": manifest.source_path, "compile_recipe": asdict(recipe),
        "compile": asdict(result), "passed": result.success,
    }
    (artifacts / "baseline-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    return report
