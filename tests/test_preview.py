import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import core
from backend.preview_routes import router


@pytest.fixture
def preview_env(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "DATA_DIR", tmp_path / "data")
    core.init_db()
    with core.conn() as db:
        project_id = db.execute("SELECT id FROM projects").fetchone()[0]
        db.execute('INSERT INTO "references" (id,project_id,title,authors,year,doi,type) VALUES(?,?,?,?,?,?,?)',
                   ("ref_preview", project_id, "Verified metadata example", json.dumps([{"given": "Ming", "family": "Li"}]),
                    2024, "10.1234/example", "article-journal"))
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        yield client, project_id


def test_preview_style_switch_is_read_only(preview_env):
    client, project_id = preview_env
    original = "# 研究\n\n论证 [@ref_preview]。"
    core.update_project(project_id, {"draft_zh": original, "expected_revision": 0})
    apa = client.post(f"/api/projects/{project_id}/preview", json={"language": "zh", "style": "apa"})
    ieee = client.post(f"/api/projects/{project_id}/preview", json={"language": "zh", "style": "ieee"})
    assert apa.status_code == ieee.status_code == 200
    assert apa.json()["revision"] == ieee.json()["revision"] == 1
    assert "Li" in apa.json()["content"] and "2024" in apa.json()["content"]
    assert "[1]" in ieee.json()["content"]
    assert "## 参考文献" in ieee.json()["content"]
    assert "[@" not in apa.json()["content"]
    assert core.get_project(project_id)["draft_zh"] == original
    assert core.get_project(project_id)["revision"] == 1


def test_preview_empty_language_is_explicit(preview_env):
    client, project_id = preview_env
    response = client.post(f"/api/projects/{project_id}/preview", json={"language": "en", "style": "apa"})
    assert response.status_code == 200
    assert response.json()["content"] == ""
    assert response.json()["warnings"]


def test_preview_rejects_foreign_reference_and_invalid_style(preview_env):
    client, project_id = preview_env
    core.update_project(project_id, {"draft_zh": "错误来源 [@ref_foreign]", "expected_revision": 0})
    assert client.post(f"/api/projects/{project_id}/preview", json={"language": "zh", "style": "ieee"}).status_code == 422
    assert client.post(f"/api/projects/{project_id}/preview", json={"language": "zh", "style": "../../secret"}).status_code == 422
    assert client.post("/api/projects/missing/preview", json={}).status_code == 404
