# -*- coding: utf-8 -*-
"""模板目录与项目创建 HTTP 契约集成测试。"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from infrastructure import stage1_adapter
from main import app
from persistence.store import ProjectStore, get_store


REPO_ROOT = Path(__file__).resolve().parents[3]
W01 = REPO_ROOT / "tests" / "fixtures" / "word" / "W01_minimal_body.docx"


##### 测试夹具板块 #####


@pytest.fixture()
def template_client(tmp_path: Path):
    os.environ["SCHOLAR_WORKSPACE_ROOT"] = str(tmp_path / "workspace")
    stage1_adapter._get_workspace_manager.cache_clear()
    store = ProjectStore(tmp_path / "state.db")
    app.dependency_overrides[get_store] = lambda: store
    try:
        yield TestClient(app), store
    finally:
        app.dependency_overrides.clear()
        stage1_adapter._get_workspace_manager.cache_clear()


##### 模板接口板块 #####


def test_template_catalog_returns_two_selectable_templates(template_client) -> None:
    client, _ = template_client
    response = client.get("/api/templates")
    assert response.status_code == 200, response.text
    templates = response.json()["templates"]
    assert {item["template_id"] for item in templates} == {
        "ouc-graduate", "ouc-bachelor",
    }
    assert all("source_fingerprint" not in item for item in templates)
    assert all("version" not in item for item in templates)


@pytest.mark.parametrize("template_id", ["ouc-graduate", "ouc-bachelor"])
def test_project_creation_records_selected_template(
    template_client, template_id: str,
) -> None:
    client, store = template_client
    response = client.post(
        "/api/projects",
        files={"docx": ("w01.docx", W01.read_bytes())},
        data={"template_id": template_id, "reference_source": "word_list"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["template_id"] == template_id
    assert "template_version" not in payload
    project = store.get(payload["project_id"])
    assert project is not None
    assert project.template_id == template_id


def test_unknown_template_and_unsupported_bibtex_are_rejected(
    template_client,
) -> None:
    client, _ = template_client
    unknown = client.post(
        "/api/projects",
        files={"docx": ("w01.docx", W01.read_bytes())},
        data={"template_id": "unknown-school"},
    )
    assert unknown.status_code == 422
    assert unknown.json()["error"] == "template_unavailable"

    unsupported = client.post(
        "/api/projects",
        files={
            "docx": ("w01.docx", W01.read_bytes()),
            "bib": ("references.bib", b"@article{x,title={X}}"),
        },
        data={"template_id": "ouc-bachelor", "reference_source": "bib"},
    )
    assert unsupported.status_code == 422
    assert unsupported.json()["error"] == "template_capability_mismatch"
