# -*- coding: utf-8 -*-
"""读取项目内置模板的轻量格式画像，并验证官方规范来源。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


##### 异常与哈希板块 #####


class TemplateProfileError(RuntimeError):
    """模板画像、LaTeX模板或官方Word规范不满足内置模板契约。"""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


##### 画像模型板块 #####


@dataclass(frozen=True, slots=True)
class TemplateFormatProfile:
    """只暴露运行时需要的模板约束，不在论文生成时重新解释Word规范。"""

    template_id: str
    profile_path: Path
    template_root: Path
    official_spec_path: Path
    locked_rules: dict[str, Any]
    conditional_rules: dict[str, dict[str, Any]]
    capabilities: dict[str, dict[str, Any]]

    def requires_english_caption(self, kind: str) -> bool:
        rule = self.conditional_rules.get(f"{kind}.english_caption", {})
        return bool(rule.get("required", False))

    def requires_english_toc(self) -> bool:
        rule = self.conditional_rules.get("toc.english_headings", {})
        return bool(rule.get("required", False))

    def capability(self, name: str) -> dict[str, Any]:
        return dict(self.capabilities.get(name, {}))

    def public_summary(self) -> dict[str, Any]:
        """报告只公开相对来源和规则摘要，不泄露服务端绝对路径。"""
        return {
            "template_id": self.template_id,
            "official_spec": self.official_spec_path.name,
            "locked_rule_count": len(self.locked_rules),
            "conditional_rule_count": len(self.conditional_rules),
            "capabilities": sorted(self.capabilities),
        }


##### 加载与验证板块 #####


def load_template_format_profile(path: str | Path) -> TemplateFormatProfile:
    profile_path = Path(path).resolve()
    try:
        document = json.loads(profile_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TemplateProfileError("模板格式画像不可读。") from exc
    contract = document.get("format_contract")
    if not isinstance(contract, dict):
        raise TemplateProfileError("模板画像缺少 format_contract。")

    template_root = (profile_path.parent / contract.get("template_root", "")).resolve()
    spec = contract.get("official_spec") or {}
    spec_path = (template_root / str(spec.get("path", ""))).resolve()
    if template_root not in spec_path.parents or not spec_path.is_file():
        raise TemplateProfileError("内置模板缺少官方 Word 规范。")
    expected_hash = str(spec.get("sha256", "")).lower()
    if len(expected_hash) != 64 or _sha256(spec_path) != expected_hash:
        raise TemplateProfileError("官方 Word 规范哈希与模板画像不一致。")
    for required in contract.get("required_latex_files", []):
        candidate = (template_root / str(required)).resolve()
        if template_root not in candidate.parents or not candidate.is_file():
            raise TemplateProfileError(f"内置 LaTeX 模板文件缺失：{required}")

    locked = contract.get("locked_rules") or {}
    conditional = contract.get("conditional_rules") or {}
    capabilities = contract.get("capabilities") or {}
    if not all(isinstance(item, dict) for item in (locked, conditional, capabilities)):
        raise TemplateProfileError("模板格式画像字段类型错误。")
    return TemplateFormatProfile(
        template_id=str(document["template_id"]),
        profile_path=profile_path,
        template_root=template_root,
        official_spec_path=spec_path,
        locked_rules=dict(locked),
        conditional_rules={str(key): dict(value) for key, value in conditional.items()},
        capabilities={str(key): dict(value) for key, value in capabilities.items()},
    )
