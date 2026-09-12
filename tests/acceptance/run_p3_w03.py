# -*- coding: utf-8 -*-
"""生成 W03 集中结构确认在两个固定模板中的 P3 验收产物。"""

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
from tests.support.template_source import source_snapshot
from template_registry.registry import load_builtin_template_registry
from pipeline_api import PipelineRunner


##### 固定路径板块 #####


ARTIFACT_ROOT = REPOSITORY_ROOT / "tests" / "artifacts" / "P3" / "W03"
WORD_PATH = REPOSITORY_ROOT / "tests/fixtures/word/W03_ambiguous_structure.docx"
EXPECTED_PATH = REPOSITORY_ROOT / (
    "tests/baselines/synthetic/W03_ambiguous_structure.expected.json"
)


class _AcceptanceTranslationClient:
    """离线补齐研究生模板强制英文目录，不参与结构识别。"""

    configured = True
    endpoint_host = "offline.acceptance"
    s = SimpleNamespace(llm_provider="offline", llm_model="acceptance-stub")

    def complete_text(self, _system: str, user: str, **_kwargs: object) -> str:
        ids = [
            line.split(" ", 1)[0].split("=", 1)[1]
            for line in user.splitlines() if line.startswith("ID=")
        ]
        return "\n".join(f"ID={item}: Structure Review" for item in ids)


def _safe_clean(path: Path) -> None:
    resolved = path.resolve()
    root = ARTIFACT_ROOT.resolve()
    if root not in resolved.parents or resolved == root:
        raise ValueError(f"拒绝清理 P3/W03 根目录之外的路径：{resolved}")
    if resolved.exists():
        shutil.rmtree(resolved)


##### 验收执行板块 #####


def main() -> int:
    expected = json.loads(EXPECTED_PATH.read_text(encoding="utf-8"))
    expected_review = expected["target_expectations"]["recognition"][
        "expected_review_texts"
    ]
    runner = PipelineRunner()
    extracted = runner.extract(str(WORD_PATH))
    recognized = runner.recognize(extracted)
    review = [
        item for item in recognized.review["heading_candidates"]
        if item["requires_review"]
    ]
    if [item["text"] for item in review] != expected_review:
        raise RuntimeError("W03 待确认标题候选与冻结基准不一致。")
    decisions = {item["unit_id"]: "section" for item in review}

    registry = load_builtin_template_registry(REPOSITORY_ROOT)
    summaries = []
    for template_id in ("ouc-graduate", "ouc-bachelor"):
        output = ARTIFACT_ROOT / template_id
        _safe_clean(output)
        resolved = resolve_template(registry, template_id)
        source = registry.source_root(resolved.manifest)
        source_before = source_snapshot(source)
        report = run_minimal_registered_generation(
            runner=PipelineRunner(), resolved_template=resolved,
            repository_root=REPOSITORY_ROOT, docx_path=WORD_PATH,
            output_directory=output, case_id="W03",
            translation_client=_AcceptanceTranslationClient(),
            report_filename="p3-report.json", heading_decisions=decisions,
        )
        tex = (
            output / resolved.adapter.content_directory / "section_01.tex"
        ).read_text(encoding="utf-8")
        accepted = bool(
            report["published"]
            and report["content"]["role_counts"].get("content_table") == 1
            and r"\tablecaption" not in tex
            and tex.index("研究方法") < tex.index("温度") < tex.index("实验结果")
            and source_before == source_snapshot(source)
        )
        summaries.append({
            "template_id": template_id, "accepted": accepted,
            "confirmed_review_count": len(decisions),
            "content_table_count": report["content"]["role_counts"].get(
                "content_table", 0
            ),
            "fixed_source_unchanged": True,
            "output": output.relative_to(REPOSITORY_ROOT).as_posix(),
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
