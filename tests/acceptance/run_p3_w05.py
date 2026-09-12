# -*- coding: utf-8 -*-
"""生成 W05 数字引用在两个固定模板中的 P3 验收产物。"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

try:
    from ._bootstrap import REPOSITORY_ROOT, ensure_stage1_imports
except ImportError:  # 兼容直接执行当前文件
    from _bootstrap import REPOSITORY_ROOT, ensure_stage1_imports

ensure_stage1_imports()

from pipeline.minimal_registered_generation import run_minimal_registered_generation
from template_registry.registry import resolve_template
from template_registry.registry import load_builtin_template_registry
from pipeline_api import PipelineRunner


##### 固定路径板块 #####


ARTIFACT_ROOT = REPOSITORY_ROOT / "tests" / "artifacts" / "P3" / "W05"
WORD_PATH = REPOSITORY_ROOT / "tests" / "fixtures" / "word" / "W05_references_complete.docx"


class _AcceptanceTranslationClient:
    """验收脚本使用的离线翻译桩，只满足固定案例的英文目录门禁。"""

    configured = True
    endpoint_host = "offline.acceptance"
    s = SimpleNamespace(llm_provider="offline", llm_model="acceptance-stub")

    def complete_text(self, _system: str, user: str, **_kwargs: object) -> str:
        ids = [
            line.split(" ", 1)[0].split("=", 1)[1]
            for line in user.splitlines() if line.startswith("ID=")
        ]
        return "\n".join(f"ID={item}: Citation Test" for item in ids)


def _safe_clean(path: Path) -> None:
    resolved = path.resolve()
    root = ARTIFACT_ROOT.resolve()
    if root not in resolved.parents or resolved == root:
        raise ValueError(f"拒绝清理 P3/W05 根目录之外的路径：{resolved}")
    if resolved.exists():
        shutil.rmtree(resolved)


##### 验收执行板块 #####


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
            case_id="W05",
            translation_client=_AcceptanceTranslationClient(),
            report_filename="p3-report.json",
            reference_source="word",
        )
        summaries.append({
            "template_id": template_id,
            "accepted": report["published"],
            "transaction_status": report["references"]["transaction_status"],
            "body_citations": report["references"]["citation_review"]["counts"]["body_citation"],
            "reference_entries": len(report["references"]["number_to_key"]),
            "output": output.relative_to(REPOSITORY_ROOT).as_posix(),
        })
    ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
    (ARTIFACT_ROOT / "acceptance-summary.json").write_text(
        json.dumps(summaries, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summaries, ensure_ascii=False, indent=2))
    return 0 if all(item["accepted"] for item in summaries) else 1


if __name__ == "__main__":
    raise SystemExit(main())
