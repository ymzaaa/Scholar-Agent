# -*- coding: utf-8 -*-
"""双 OUC 产品模板目录测试。"""

import pytest

from infrastructure.template_catalog import (
    TemplateSelectionError,
    available_template_bindings,
    resolve_template_binding,
)


def test_catalog_only_exposes_two_current_templates() -> None:
    bindings = available_template_bindings()
    assert {item.template_id for item in bindings} == {
        "ouc-graduate", "ouc-bachelor",
    }
    assert all("version" not in item.public_record() for item in bindings)


def test_alias_and_unknown_template_are_rejected() -> None:
    with pytest.raises(TemplateSelectionError, match="未注册模板"):
        resolve_template_binding("ouc")
    with pytest.raises(TemplateSelectionError, match="未注册模板"):
        resolve_template_binding("unknown-school")
