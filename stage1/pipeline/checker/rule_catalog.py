# -*- coding: utf-8 -*-
"""加载通用规则目录和模板适配器声明的格式画像。"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path

from pipeline.checker.rule_models import RuleDefinition
from pipeline.template_profile import load_template_format_profile


##### JSON规则板块 #####


def _rule_from_mapping(raw: dict, *, defaults: dict | None = None) -> RuleDefinition:
    merged = {**(defaults or {}), **raw}
    return RuleDefinition(
        rule_id=merged["rule_id"],
        category=merged.get("category", ""),
        item=merged.get("item", ""),
        layer=merged["layer"],
        phase=merged.get("phase", "source"),
        disposition=merged.get("disposition", "block"),
        detector=merged.get("detector"),
        source=merged.get("source", ""),
        config=merged.get("config", {}),
    )


def load_common_rules(path: str | Path) -> list[RuleDefinition]:
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    return [_rule_from_mapping(item) for item in document["rules"]]


##### 模板规范板块 #####


_HEADING_RE = re.compile(r"^##\s+(\d+(?:\.\d+)*)\.??\s*(.+?)\s*$")
_ROW_RE = re.compile(r"^\|\s*(\d+(?:\.\d+)+)\s*\|\s*([^|]+?)\s*\|")


def _rules_from_markdown(profile_path: Path, profile: dict) -> list[dict]:
    """读取静态规范表，供显式离线模板基线回归使用。"""
    source_path = (profile_path.parent / profile["spec_source"]).resolve()
    lines = source_path.read_text(encoding="utf-8").splitlines()
    category = ""
    rules = []
    for line in lines:
        heading = _HEADING_RE.match(line)
        if heading:
            category = heading.group(2)
            continue
        row = _ROW_RE.match(line)
        if not row:
            continue
        spec_id, item = row.groups()
        rules.append(
            {
                "rule_id": f"{profile['rule_prefix']}-{spec_id}",
                "category": category,
                "item": item.strip(),
                "source": f"{source_path.name}#{spec_id}",
            }
        )
    if not rules:
        raise ValueError(f"模板规范未解析到任何规则：{source_path}")
    return rules


def load_template_profile(
    path: str | Path,
    *, baseline: bool = False,
) -> tuple[dict, list[RuleDefinition], dict[str, dict]]:
    profile_path = Path(path).resolve()
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    # 完整静态规范只进入离线基线；论文运行仅加载声明的动态检查。
    if profile.get("profile_status") == "common_rules_only":
        raw_rules = list(profile.get("rules", []))
    elif profile.get("spec_source") and baseline:
        raw_rules = [
            *_rules_from_markdown(profile_path, profile),
            *profile.get("rules", []),
        ]
    else:
        raw_rules = [
            *profile.get("rules", []),
            *({"rule_id": rule_id, **value} for rule_id, value in profile.get("overrides", {}).items()
              if value.get("detector") in {"table_alignment", "pdf_page_size"}),
        ]
    raw_rules = [*raw_rules, *profile.get("extra_rules", [])]
    overrides = dict(profile.get("overrides", {}))
    for group in profile.get("assertion_groups", []):
        shared = {key: value for key, value in group.items() if key != "rule_ids"}
        for rule_id in group["rule_ids"]:
            overrides[rule_id] = {**shared, **overrides.get(rule_id, {})}
    defaults = {
        "layer": "template",
        "phase": "source",
        "disposition": profile.get("default_disposition", "degrade"),
    }
    definitions = []
    for raw in raw_rules:
        override = overrides.get(raw["rule_id"], {})
        definitions.append(_rule_from_mapping({**raw, **override}, defaults=defaults))
    metadata = {
        "template_id": profile["template_id"],
        "profile_path": str(profile_path),
        "rule_count": len(definitions),
    }
    if profile.get("format_contract"):
        metadata["format_contract"] = load_template_format_profile(
            profile_path
        ).public_summary()
    raw_common_overrides = profile.get("common_overrides", {})
    if not isinstance(raw_common_overrides, dict) or not all(
        isinstance(rule_id, str) and isinstance(value, dict)
        for rule_id, value in raw_common_overrides.items()
    ):
        raise ValueError("common_overrides 必须是规则 ID 到覆盖对象的映射。")
    allowed_fields = {"disposition", "config"}
    for rule_id, value in raw_common_overrides.items():
        unknown = set(value) - allowed_fields
        if unknown:
            raise ValueError(f"通用规则覆盖包含非法字段：{rule_id}={sorted(unknown)}")
        if "config" in value and not isinstance(value["config"], dict):
            raise ValueError(f"通用规则覆盖 config 必须是对象：{rule_id}")
    metadata["common_override_count"] = len(raw_common_overrides)
    return metadata, definitions, {
        str(rule_id): dict(value) for rule_id, value in raw_common_overrides.items()
    }


def _apply_common_overrides(
    rules: list[RuleDefinition], overrides: dict[str, dict],
) -> list[RuleDefinition]:
    by_id = {rule.rule_id: rule for rule in rules}
    unknown = sorted(set(overrides) - set(by_id))
    if unknown:
        raise ValueError(f"模板试图覆盖未知通用规则：{unknown}")
    result = []
    for rule in rules:
        override = overrides.get(rule.rule_id)
        if not override:
            result.append(rule)
            continue
        result.append(replace(
            rule,
            disposition=override.get("disposition", rule.disposition),
            config={**rule.config, **override.get("config", {})},
        ))
    return result


##### 活动规则板块 #####


def build_active_rules(
    common_path: str | Path, template_profile_path: str | Path, *, baseline: bool = False,
) -> tuple[dict, list[RuleDefinition]]:
    metadata, template_rules, common_overrides = load_template_profile(
        template_profile_path, baseline=baseline,
    )
    common_rules = _apply_common_overrides(
        load_common_rules(common_path), common_overrides,
    )
    rules = common_rules + template_rules
    if not baseline:
        dynamic_detectors = {"log_pattern", "pdf_page_size", "table_alignment", "bilingual_caption", "english_headings"}
        placeholder_rules = {"COMMON-PLACEHOLDER", "COMMON-COVER-METADATA-PENDING", "OUC-TEMPLATE-PLACEHOLDER"}
        rules = [rule for rule in rules if (
            rule.detector in dynamic_detectors or rule.rule_id in placeholder_rules
        ) and rule.rule_id != "COMMON-REF-UNRESOLVED"]
        metadata["manual_review"] = "精细版式仍需人工结合 PDF 复核。"
    ids = [rule.rule_id for rule in rules]
    if len(ids) != len(set(ids)):
        raise ValueError("活动规则集包含重复 rule_id。")
    metadata["common_rule_count"] = sum(rule.layer == "common" for rule in rules)
    metadata["active_rule_count"] = len(rules)
    return metadata, rules
