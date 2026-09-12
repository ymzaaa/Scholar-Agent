# -*- coding: utf-8 -*-
"""生成 W07 混合公式在两个固定模板中的 P3 验收产物。"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

try:
    from ._bootstrap import REPOSITORY_ROOT, ensure_stage1_imports
except ImportError:  # 兼容直接执行当前文件
    from _bootstrap import REPOSITORY_ROOT, ensure_stage1_imports

ensure_stage1_imports()

from pipeline.minimal_registered_generation import run_minimal_registered_generation
from template_registry.registry import resolve_template
from template_registry.registry import load_builtin_template_registry
from pipeline_api import PipelineRunner
from tests.support.fake_llm import FakeLLMClient


##### 固定路径板块 #####


ARTIFACT_ROOT = REPOSITORY_ROOT / "tests" / "artifacts" / "P3" / "W07"
WORD_PATH = (
    REPOSITORY_ROOT / "tests" / "fixtures" / "word"
    / "W07_formulas_mixed_valid.docx"
)
EXPECTED_PATH = (
    REPOSITORY_ROOT / "tests" / "baselines" / "synthetic"
    / "W07_formulas_mixed_valid.expected.json"
)


def _safe_clean(path: Path) -> None:
    resolved = path.resolve()
    root = ARTIFACT_ROOT.resolve()
    if root not in resolved.parents or resolved == root:
        raise ValueError(f"拒绝清理 P3 根目录之外的路径：{resolved}")
    if resolved.exists():
        shutil.rmtree(resolved)


##### 验收执行板块 #####


def main() -> int:
    expected = json.loads(EXPECTED_PATH.read_text(encoding="utf-8"))
    expected_bodies = expected["target_expectations"]["formulas"][
        "expected_latex_bodies"
    ]
    registry = load_builtin_template_registry(REPOSITORY_ROOT)
    summaries = []
    for template_id in ("ouc-graduate", "ouc-bachelor"):
        output = ARTIFACT_ROOT / template_id
        _safe_clean(output)
        report = run_minimal_registered_generation(
            runner=PipelineRunner(),
            resolved_template=resolve_template(registry, template_id),
            repository_root=REPOSITORY_ROOT,
            docx_path=WORD_PATH,
            output_directory=output,
            case_id="W07",
            translation_client=FakeLLMClient(),
            report_filename="p3-report.json",
        )
        actual_bodies = [
            item["latex"] for item in report["formulas"]["inventory"]
        ]
        accepted = bool(
            report["published"] and report["formulas"]["all_passed"]
            and actual_bodies == expected_bodies
        )
        summaries.append({
            "template_id": template_id,
            "accepted": accepted,
            "formula_count": len(actual_bodies),
            "display_formula_count": sum(
                item["display"] for item in report["formulas"]["inventory"]
            ),
            "output": output.relative_to(REPOSITORY_ROOT).as_posix(),
        })
    print(json.dumps(summaries, ensure_ascii=False, indent=2))
    return 0 if all(item["accepted"] for item in summaries) else 1


if __name__ == "__main__":
    raise SystemExit(main())
