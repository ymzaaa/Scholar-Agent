# -*- coding: utf-8 -*-
"""后端可选模板目录与项目模板冻结契约。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from infrastructure.stage1_adapter import (
    RegisteredTemplateError,
    list_registered_templates,
    resolve_registered_template,
)


##### 数据模型板块 #####


class TemplateSelectionError(ValueError):
    """客户端选择了未注册或不可用的模板。"""


@dataclass(frozen=True, slots=True)
class TemplateBinding:
    """项目创建时保存的当前模板身份。"""

    template_id: str
    display_name: str
    school: str
    education_level: str
    capabilities: dict[str, str]
    bibtex_supported: bool

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> "TemplateBinding":
        return cls(
            template_id=str(record["template_id"]),
            display_name=str(record["display_name"]),
            school=str(record["school"]),
            education_level=str(record["education_level"]),
            capabilities=dict(record["capabilities"]),
            bibtex_supported=bool(record["bibtex_supported"]),
        )

    def public_record(self) -> dict[str, Any]:
        return {
            "template_id": self.template_id,
            "display_name": self.display_name,
            "school": self.school,
            "education_level": self.education_level,
            "capabilities": dict(self.capabilities),
            "bibtex_supported": self.bibtex_supported,
        }


##### 模板查询板块 #####


def available_template_bindings() -> list[TemplateBinding]:
    bindings = [
        TemplateBinding.from_record(record)
        for record in list_registered_templates()
    ]
    return sorted(
        bindings,
        key=lambda item: (item.school, item.display_name),
    )


def resolve_template_binding(template_id: str) -> TemplateBinding:
    try:
        record = resolve_registered_template(template_id)
    except RegisteredTemplateError as exc:
        raise TemplateSelectionError(str(exc)) from exc
    return TemplateBinding.from_record(record)
