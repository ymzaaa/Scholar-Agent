# -*- coding: utf-8 -*-
"""模板驱动的分层格式规则执行器。"""

from __future__ import annotations

import json
from pathlib import Path

from pipeline.checker.rule_catalog import build_active_rules
from pipeline.checker.rule_detectors import CheckContext, execute_detector
from pipeline.checker.rule_models import RuleResult, summarize_rule_results


##### 默认规则路径板块 #####


STAGE1_DIR = Path(__file__).resolve().parents[2]
DEFAULT_COMMON_RULES = STAGE1_DIR / "rules" / "common-format-rules.json"


##### 规则执行板块 #####


class FormatChecker:
    """按通用目录和当前模板画像构造本次任务的活动规则集。"""

    def __init__(
        self,
        common_rules_path: str | Path,
        tex_dir: str | Path,
        template_profile_path: str | Path,
        *, baseline: bool = False,
    ) -> None:
        self.tex_dir = Path(tex_dir).resolve()
        self.profile, self.rules = build_active_rules(
            common_rules_path, template_profile_path, baseline=baseline,
        )
        self.results: dict[str, dict] = {}

    def run_all(self) -> dict[str, dict]:
        context = CheckContext(self.tex_dir)
        for rule in self.rules:
            outcome = execute_detector(context, rule)
            self.results[rule.rule_id] = RuleResult(
                rule=rule,
                status=outcome.status,
                detail=outcome.detail,
                evidence=outcome.evidence,
                occurrence_count=outcome.occurrence_count,
            ).to_dict()
        return self.results


##### 报告板块 #####


    def coverage_matrix(self) -> dict:
        if not self.results:
            self.run_all()
        summary = summarize_rule_results(self.results)
        return {
            "schema_version": "1.0.0",
            "template": self.profile,
            "quality_status": summary.quality_status,
            "publish_allowed": summary.publish_allowed,
            "all_passed": summary.all_passed,
            "counts": summary.counts,
            "blockers": summary.blockers,
            "degradations": summary.degradations,
            "rules": self.results,
        }

    def report(self) -> str:
        matrix = self.coverage_matrix()
        counts = matrix["counts"]
        lines = [
            "Format Check Report: "
            f"status={matrix['quality_status']}, "
            f"pass={counts['pass']}, fail={counts['fail']}, "
            f"not_applicable={counts['not_applicable']}, "
            f"not_implemented={counts['not_implemented']}"
        ]
        for rule_id, result in self.results.items():
            lines.append(
                f"  [{result['status'].upper()}|{result['disposition'].upper()}] "
                f"{rule_id}: {result['detail'][:140]}"
            )
        return "\n".join(lines)


##### 命令行板块 #####


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="运行模板驱动的格式规则检查")
    parser.add_argument("--tex-dir", required=True)
    parser.add_argument("--template-profile", required=True)
    parser.add_argument("--common-rules", default=str(DEFAULT_COMMON_RULES))
    parser.add_argument("--output", default=None)
    args = parser.parse_args()
    checker = FormatChecker(args.common_rules, args.tex_dir, args.template_profile)
    checker.run_all()
    print(checker.report())
    if args.output:
        Path(args.output).write_text(
            json.dumps(checker.coverage_matrix(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
