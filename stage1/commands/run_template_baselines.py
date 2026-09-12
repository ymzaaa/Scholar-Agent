# -*- coding: utf-8 -*-
"""为两个当前内置模板生成隔离基线报告。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

try:
    from ._bootstrap import REPOSITORY_ROOT, ensure_stage1_imports
except ImportError:  # 兼容直接执行当前文件
    from _bootstrap import REPOSITORY_ROOT, ensure_stage1_imports

ensure_stage1_imports()

from template_registry.baseline import run_template_baseline
from template_registry.models import TemplateManifestError
from template_registry.registry import load_builtin_template_registry


##### 路径与参数板块 #####


BASELINE_ROOT = REPOSITORY_ROOT / "tests" / "baselines" / "templates"


def _artifact_directory(template_id: str) -> Path:
    target = (BASELINE_ROOT / template_id / "current").resolve()
    if BASELINE_ROOT not in target.parents:
        raise TemplateManifestError("模板基线目录必须位于 tests/baselines/templates。")
    return target


##### 执行板块 #####


def run_all(*, timeout_seconds: int = 120) -> list[dict]:
    registry = load_builtin_template_registry(REPOSITORY_ROOT)
    reports = []
    for manifest in registry.templates():
        artifacts = _artifact_directory(manifest.template_id)
        report = run_template_baseline(
            manifest, REPOSITORY_ROOT, artifacts,
            timeout_seconds=timeout_seconds,
        )
        reports.append(report)
    return reports


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=int, default=120, help="每个编译进程超时秒数。")
    arguments = parser.parse_args()
    reports = run_all(timeout_seconds=arguments.timeout)
    summary = [
        {
            "template": report["template_id"],
            "compile_success": report["compile"]["success"],
            "passed": report["passed"],
        }
        for report in reports
    ]
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if all(item["passed"] for item in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
