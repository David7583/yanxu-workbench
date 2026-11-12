from fastapi.testclient import TestClient
from backend import core, providers
from backend.app import app


def test_local_api_rejects_other_websites_and_invalid_hosts(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "DATA_DIR", tmp_path)
    monkeypatch.setattr(providers.keyring, "get_password", lambda *args: None)
    with TestClient(app) as client:
        response = client.post("/api/projects", json={"title": "Blocked"}, headers={"Origin": "https://untrusted.example"})
        assert response.status_code == 403
        assert client.get("/api/health", headers={"Host": "untrusted.example"}).status_code == 400
        response = client.post("/api/projects", json={"title": "Local"}, headers={"Origin": "http://127.0.0.1:8765"})
        assert response.status_code == 201
        assert response.headers["cache-control"] == "no-store"
        assert client.get("/api/unknown").status_code == 404
        assert client.get("/api/exports/invalid/file.txt").status_code == 404


def test_blank_export_directory_restores_default(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "DATA_DIR", tmp_path)
    monkeypatch.setattr(providers.keyring, "get_password", lambda *args: None)
    with TestClient(app) as client:
        response = client.put("/api/settings", json={"export_dir": ""})
        assert response.status_code == 200
        assert response.json()["export_dir"] == providers.DEFAULTS["export_dir"]
