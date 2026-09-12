# -*- coding: utf-8 -*-
"""生成 W09 异常公式在两个固定模板前的一致阻断证据。"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

try:
    from ._bootstrap import REPOSITORY_ROOT, ensure_stage1_imports
except ImportError:  # 兼容直接执行当前文件
    from _bootstrap import REPOSITORY_ROOT, ensure_stage1_imports

ensure_stage1_imports()

from template_registry.registry import resolve_template
from tests.support.template_source import source_snapshot
from template_registry.registry import load_builtin_template_registry
from pipeline_api import PipelineRunner


##### 固定路径板块 #####


ARTIFACT_ROOT = REPOSITORY_ROOT / "tests/artifacts/P3/W09"
WORD_PATH = REPOSITORY_ROOT / "tests/fixtures/word/W09_formulas_malformed.docx"
EXPECTED_PATH = REPOSITORY_ROOT / (
    "tests/baselines/synthetic/W09_formulas_malformed.expected.json"
)


def _safe_clean(path: Path) -> None:
    resolved = path.resolve()
    root = ARTIFACT_ROOT.resolve()
    if root not in resolved.parents or resolved == root:
        raise ValueError(f"拒绝清理 P3/W09 根目录之外的路径：{resolved}")
    if resolved.exists():
        shutil.rmtree(resolved)


##### 验收执行板块 #####


def main() -> int:
    expected = json.loads(EXPECTED_PATH.read_text(encoding="utf-8"))
    required = expected["target_expectations"]["formulas"][
        "must_report_candidates"
    ]
    runner = PipelineRunner()
    extracted = runner.extract(str(WORD_PATH))
    recognized = runner.recognize(extracted)
    review = recognized.review["formula_review"]
    raw_candidates = [item["raw_text"] for item in review["candidates"]]
    if raw_candidates != required:
        raise RuntimeError("W09 异常公式候选与冻结基准不一致。")

    registry = load_builtin_template_registry(REPOSITORY_ROOT)
    summaries = []
    for template_id in ("ouc-graduate", "ouc-bachelor"):
        output = ARTIFACT_ROOT / template_id
        _safe_clean(output)
        output.mkdir(parents=True)
        resolved = resolve_template(registry, template_id)
        source = registry.source_root(resolved.manifest)
        source_before = source_snapshot(source)
        report = {
            "schema_version": "1.0.0", "case_id": "W09",
            "template": {
                "template_id": template_id,
            },
            "formula_preflight": review,
            "gates": {
                "formula_preflight": False,
                "render": "not_run", "compile": "not_run", "publish": False,
            },
            "published": False, "quality_status": "blocked",
            "artifacts": {"pdf": False, "latex_source": False},
        }
        report_path = output / "p3-report.json"
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        source_after = source_snapshot(source)
        accepted = bool(
            review["blocking"] and review["malformed_formula_count"] == 3
            and review["valid_formula_count"] == 0
            and not (output / "main.pdf").exists()
            and not (output / "latex-source.zip").exists()
            and source_before == source_after
        )
        summaries.append({
            "template_id": template_id, "accepted": accepted,
            "malformed_formula_count": review["malformed_formula_count"],
            "render_status": "not_run", "compile_status": "not_run",
            "pdf_exists": False,
            "fixed_source_unchanged": source_before == source_after,
            "report": report_path.relative_to(REPOSITORY_ROOT).as_posix(),
        })
    ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
    (ARTIFACT_ROOT / "acceptance-summary.json").write_text(
        json.dumps(summaries, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summaries, ensure_ascii=False, indent=2))
    return 0 if all(item["accepted"] for item in summaries) else 1


if __name__ == "__main__":
    raise SystemExit(main())
