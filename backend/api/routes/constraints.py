# -*- coding: utf-8 -*-
"""论文级约束候选的查询与人工确认接口。"""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from api.schemas import ConfirmConstraintRequest, ConstraintListResponse
from constraints.service import constraint_payload
from persistence.store import ProjectStore, get_store


router = APIRouter(tags=["paper-constraints"])


@router.get("/api/projects/{project_id}/constraints", response_model=ConstraintListResponse)
def list_constraints(project_id: str, store: ProjectStore = Depends(get_store)):
    if store.get(project_id) is None:
        return JSONResponse(status_code=404, content={"detail": "项目不存在。"})
    return ConstraintListResponse(
        project_id=project_id,
        constraints=[constraint_payload(item) for item in store.list_constraints(project_id)],
    )


@router.post("/api/constraints/{constraint_id}/confirm")
def confirm_constraint(
    constraint_id: str, body: ConfirmConstraintRequest,
    store: ProjectStore = Depends(get_store),
):
    try:
        item = store.confirm_constraint(constraint_id, supersedes_id=body.supersedes_id)
    except ValueError as exc:
        return JSONResponse(status_code=409, content={"detail": str(exc)})
    return constraint_payload(item)
