# -*- coding: utf-8 -*-
"""生成 W01 在两个已注册模板中的 P2 验收产物。"""

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


ARTIFACT_ROOT = REPOSITORY_ROOT / "tests" / "artifacts" / "P2" / "W01"
WORD_PATH = REPOSITORY_ROOT / "tests" / "fixtures" / "word" / "W01_minimal_body.docx"


def _safe_clean(path: Path) -> None:
    resolved = path.resolve()
    root = ARTIFACT_ROOT.resolve()
    if root not in resolved.parents or resolved == root:
        raise ValueError(f"拒绝清理 P2 根目录之外的路径：{resolved}")
    if resolved.exists():
        shutil.rmtree(resolved)


def main() -> int:
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
            translation_client=FakeLLMClient(),
        )
        summaries.append({
            "template_id": template_id,
            "published": report["published"],
            "quality_status": report["quality_status"],
            "output": output.relative_to(REPOSITORY_ROOT).as_posix(),
        })
    print(json.dumps(summaries, ensure_ascii=False, indent=2))
    return 0 if all(item["published"] for item in summaries) else 1


if __name__ == "__main__":
    raise SystemExit(main())
