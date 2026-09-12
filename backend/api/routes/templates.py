# -*- coding: utf-8 -*-
"""已审核模板目录查询接口。"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from api.schemas import TemplateListResponse, TemplateSummary
from infrastructure.template_catalog import available_template_bindings


##### 路由定义板块 #####


router = APIRouter(prefix="/api/templates", tags=["templates"])


@router.get("", response_model=TemplateListResponse)
def list_templates():
    """只返回当前注册、可用且具有适配器的双 OUC 模板。"""
    try:
        templates = [
            TemplateSummary(**binding.public_record())
            for binding in available_template_bindings()
        ]
    except Exception:  # 注册表损坏属于服务端状态，不能伪装为空目录。
        return JSONResponse(
            status_code=503,
            content={
                "error": "template_catalog_unavailable",
                "detail": "模板目录暂不可用，请检查服务端注册清单。",
            },
        )
    return TemplateListResponse(templates=templates)
