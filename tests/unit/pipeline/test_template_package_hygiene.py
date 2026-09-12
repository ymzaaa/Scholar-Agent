# -*- coding: utf-8 -*-
"""OUC 模板前导区的宏包唯一性与加载顺序测试。"""

from __future__ import annotations

import re

from tests.support.paths import GRADUATE_TEMPLATE


##### 模板宏包约束板块 #####


def _template_source() -> str:
    return (GRADUATE_TEMPLATE / "oucthesis.cls").read_text(encoding="utf-8")


def _required_packages(source: str) -> list[str]:
    return re.findall(r"\\RequirePackage(?:\[[^]]*\])?\{([^}]+)\}", source)


def test_ouc_template_has_no_redundant_class_packages() -> None:
    source = _template_source()
    packages = _required_packages(source)

    assert "ctex" not in packages, "ctexbook 文档类已经提供 ctex，不应再次加载 ctex 包"
    assert packages.count("geometry") == 1, "geometry 必须只加载一次"
    assert source.count(r"\PassOptionsToClass{scheme=chinese}{ctexbook}") == 1


def test_math_packages_follow_supported_loading_order() -> None:
    source = _template_source()
    packages = _required_packages(source)

    assert packages.index("amsmath") < packages.index("mathtools")
    assert packages.index("mathtools") < packages.index("unicode-math")
