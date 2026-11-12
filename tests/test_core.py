import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import core
from backend.core_routes import router


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "DATA_DIR", tmp_path)
    core.init_db()
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        yield client


def test_conflicting_edit_does_not_overwrite_and_restore_is_scoped(client):
    first = client.get("/api/projects").json()["items"][0]
    project = first["id"]
    result = client.patch(f"/api/projects/{project}", json={"expected_revision": 0, "draft_zh": "人工修改的内容"})
    assert result.status_code == 200
    assert result.json()["revision"] == 1
    conflict = client.patch(f"/api/projects/{project}", json={"expected_revision": 0, "draft_zh": "过期 AI 输出"})
    assert conflict.status_code == 409
    assert client.get(f"/api/projects/{project}").json()["draft_zh"] == "人工修改的内容"
    revision = client.get(f"/api/projects/{project}/revisions").json()["items"][0]
    other = client.post("/api/projects", json={"title": "另一个项目"}).json()
    assert client.post(f"/api/projects/{other['id']}/restore/{revision['id']}").status_code == 404
    restored = client.post(f"/api/projects/{project}/restore/{revision['id']}", json={"expected_revision": 1})
    assert restored.status_code == 200
    assert restored.json()["draft_zh"] == ""
    assert len(client.get(f"/api/projects/{project}/revisions").json()["items"]) == 2


def test_project_patch_requires_revision(client):
    project = client.get("/api/projects").json()["items"][0]["id"]
    assert client.patch(f"/api/projects/{project}", json={"draft_zh": "未注明版本"}).status_code == 422


def test_invalid_upload_rejected_before_scheduling(client):
    assert client.post("/api/import", files={"file": ("empty.json", b"", "application/json")}).status_code == 422
    assert client.post("/api/import", files={"file": ("run.exe", b"bytes", "application/octet-stream")}).status_code == 415


def test_upload_search_and_exact_file_dedup(client, monkeypatch):
    from backend import core_routes
    from backend.importer import process_import
    monkeypatch.setattr(core_routes, "start_import", process_import)
    payload = '{"id":"upload-1","messages":[{"role":"user","content":"教育公平研究"}]}'.encode("utf-8")
    first = client.post("/api/import", files={"file": ("archive.json", payload, "application/json")})
    assert first.status_code == 202
    job = client.get("/api/imports/" + first.json()["job_id"]).json()
    assert job["status"] == "done"
    assert job["imported"] == 1
    assert client.get("/api/search", params={"q": "教育公平"}).json()["items"][0]["role"] == "user"
    second = client.post("/api/import", files={"file": ("renamed.json", payload, "application/json")})
    assert second.json()["duplicate"] is True
    assert client.get("/api/sources").json()["total"] == 1
