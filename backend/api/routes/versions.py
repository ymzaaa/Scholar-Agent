# -*- coding: utf-8 -*-
"""项目版本列表与用户接受、拒绝、直接父版本回退接口。"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from api.schemas import (
    GenerationResponse, VersionListResponse, VersionReviewResponse,
)
from persistence.store import ProjectStore, get_store
from generation.versions import (
    VersionContractError, generation_response_payload, validate_trusted_generation,
)


##### 版本查询板块 #####


router = APIRouter(tags=["versions"])


@router.get(
    "/api/projects/{project_id}/versions", response_model=VersionListResponse,
)
def list_versions(project_id: str, store: ProjectStore = Depends(get_store)):
    project = store.get(project_id)
    if project is None:
        return JSONResponse(
            status_code=404,
            content={"error": "project_not_found", "detail": "项目不存在。"},
        )
    versions = [
        GenerationResponse(**generation_response_payload(
            item,
            accepted_generation_id=project.accepted_generation_id,
        ))
        for item in store.list_generations(project_id)
        if item.trusted and item.status in {"success", "degraded"}
        and item.review_status in {"pending", "accepted", "superseded", "rejected"}
    ]
    return VersionListResponse(
        project_id=project_id,
        accepted_version_id=project.accepted_generation_id if project else None,
        versions=versions,
    )


##### 用户评审板块 #####


@router.post(
    "/api/versions/{version_id}/accept", response_model=VersionReviewResponse,
)
def accept_version(version_id: str, store: ProjectStore = Depends(get_store)):
    try:
        version = store.get_generation(version_id)
        if version is None:
            raise KeyError(version_id)
        project = store.get(version.project_id)
        if project is None:
            raise VersionContractError("版本所属项目不存在。")
        validate_trusted_generation(version, project)
        accepted, previous = store.accept_generation(version_id)
    except KeyError:
        return JSONResponse(
            status_code=404,
            content={"error": "version_not_found", "detail": "版本不存在。"},
        )
    except (ValueError, VersionContractError) as exc:
        return JSONResponse(
            status_code=409,
            content={"error": "version_review_conflict", "detail": str(exc)},
        )
    return VersionReviewResponse(
        project_id=accepted.project_id, version_id=version_id,
        previous_accepted_version_id=previous,
        accepted_version_id=version_id, review_status=accepted.review_status,
    )


@router.post(
    "/api/versions/{version_id}/reject", response_model=VersionReviewResponse,
)
def reject_version(version_id: str, store: ProjectStore = Depends(get_store)):
    try:
        rejected, previous = store.reject_generation(version_id)
    except KeyError:
        return JSONResponse(
            status_code=404,
            content={"error": "version_not_found", "detail": "版本不存在。"},
        )
    except (ValueError, VersionContractError) as exc:
        return JSONResponse(
            status_code=409,
            content={"error": "version_review_conflict", "detail": str(exc)},
        )
    return VersionReviewResponse(
        project_id=rejected.project_id, version_id=version_id,
        accepted_version_id=previous,
        previous_accepted_version_id=previous, review_status=rejected.review_status,
    )


@router.post(
    "/api/versions/{version_id}/rollback", response_model=VersionReviewResponse,
)
def rollback_version(version_id: str, store: ProjectStore = Depends(get_store)):
    """只允许把当前接受版本回退到其直接可信父版本。"""
    try:
        current = store.get_generation(version_id)
        if current is None:
            raise KeyError(version_id)
        parent = store.get_generation(current.parent_generation_id) if current.parent_generation_id else None
        project = store.get(current.project_id)
        if parent is None or project is None:
            raise ValueError("当前版本没有可回退的直接可信父版本。")
        validate_trusted_generation(parent, project)
        rolled_back, accepted_parent = store.rollback_accepted_generation(version_id)
    except KeyError:
        return JSONResponse(
            status_code=404,
            content={"error": "version_not_found", "detail": "版本不存在。"},
        )
    except (ValueError, VersionContractError) as exc:
        return JSONResponse(
            status_code=409,
            content={"error": "version_rollback_conflict", "detail": str(exc)},
        )
    return VersionReviewResponse(
        project_id=rolled_back.project_id,
        version_id=rolled_back.generation_id,
        previous_accepted_version_id=rolled_back.generation_id,
        accepted_version_id=accepted_parent.generation_id,
        review_status=rolled_back.review_status,
    )
