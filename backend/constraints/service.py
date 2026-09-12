# -*- coding: utf-8 -*-
"""论文约束的公共响应转换；约束登记由可信候选事务负责。"""

from typing import Any

from constraints.models import PaperConstraint


##### 公共响应板块 #####


def constraint_payload(item: PaperConstraint) -> dict[str, Any]:
    """只返回已经持久化的约束事实。"""
    return item.to_snapshot()
