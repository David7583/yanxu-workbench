import json

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from backend import core, providers, research_routes as research


@pytest.fixture
def research_env(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "DATA_DIR", tmp_path / "data")
    core.init_db()
    vault = {}
    monkeypatch.setattr(providers.keyring, "get_password", lambda service, user: vault.get((service, user)))
    monkeypatch.setattr(providers.keyring, "set_password", lambda service, user, value: vault.__setitem__((service, user), value))
    monkeypatch.setattr(providers.keyring, "delete_password", lambda service, user: vault.pop((service, user), None))
    research._literature_cache.clear()
    research._project_locks.clear()
    app = FastAPI()
    app.include_router(research.router)
    with core.conn() as db:
        project_id = db.execute("SELECT id FROM projects").fetchone()[0]
    with TestClient(app) as client:
        yield client, project_id, vault


def configure(client):
    response = client.put("/api/settings", json={"model": "test-model", "api_key": "private-secret", "base_url": "https://models.example/v1"})
    assert response.status_code == 200


def test_credentials_are_keyring_only_and_bound_to_endpoint(research_env):
    client, project_id, vault = research_env
    configure(client)
    assert list(vault.values()) == ["private-secret"]
    response = client.get("/api/settings")
    assert response.json()["has_key"] is True
    assert "private-secret" not in response.text
    with core.conn() as db:
        contents = str([tuple(row) for row in db.execute("SELECT * FROM settings")])
    assert "private-secret" not in contents
    response = client.put("/api/settings", json={"base_url": "https://another.example/v1"})
    assert response.json()["has_key"] is False
    response = client.put("/api/settings", json={"base_url": "http://remote.example/v1"})
    assert response.status_code == 422


def test_keyring_failure_does_not_save_plaintext(research_env, monkeypatch):
    client, _, _ = research_env
    def fail(*args):
        raise RuntimeError("private-secret")
    monkeypatch.setattr(providers.keyring, "set_password", fail)
    response = client.put("/api/settings", json={"model": "test-model", "api_key": "private-secret"})
    assert response.status_code == 503
    assert "private-secret" not in response.text
    assert client.get("/api/settings").json()["model"] == ""


def test_no_key_chat_fails_without_fake_messages(research_env):
    client, project_id, _ = research_env
    response = client.post(f"/api/projects/{project_id}/chat", json={"message": "请继续讨论"})
    assert response.status_code == 409
    with core.conn() as db:
        assert db.execute("SELECT count(*) FROM chats").fetchone()[0] == 0


def test_chat_persists_real_provider_result_and_history_sources(research_env, monkeypatch):
    client, project_id, _ = research_env
    configure(client)
    with core.conn() as db:
        db.execute("INSERT INTO sources(id,title,provider,kind,created_at,source_file) VALUES(?,?,?,?,?,?)",
                   ("source1", "教育公平讨论", "GPT", "json", core.now(), "old.json"))
        db.execute("INSERT INTO source_messages(id,source_id,role,content,ordinal,fingerprint) VALUES(?,?,?,?,?,?)",
                   ("m1", "source1", "user", "教育公平应考虑设备条件。", 0, "hash1"))
    captured = []
    async def fake_complete(messages, **kwargs):
        captured.extend(messages)
        return "设备条件是一个需要检验的因素 [H1]。"
    monkeypatch.setattr(providers, "complete", fake_complete)
    response = client.post(f"/api/projects/{project_id}/chat", json={"message": "教育公平", "agent": "critical"})
    assert response.status_code == 200, response.text
    assert response.json()["sources"][0]["source_id"] == "source1"
    assert "设备条件" in str(captured)
    with core.conn() as db:
        assert db.execute("SELECT count(*) FROM chats").fetchone()[0] == 2


def test_generation_is_only_suggestion_and_checks_revision(research_env, monkeypatch):
    client, project_id, _ = research_env
    configure(client)
    core.update_project(project_id, {"notes": "人工编辑的笔记", "expected_revision": 0})
    async def fake_complete(messages, **kwargs):
        return json.dumps({"notes": "建议笔记", "draft_zh": "中文稿[待补证据]", "draft_en": "Draft [evidence needed]"})
    monkeypatch.setattr(providers, "complete", fake_complete)
    response = client.post(f"/api/projects/{project_id}/generate", json={"target": "both", "expected_revision": 1})
    assert response.status_code == 200, response.text
    assert response.json()["base_revision"] == 1
    assert response.json()["notes"] == "建议笔记"
    assert core.get_project(project_id)["notes"] == "人工编辑的笔记"
    response = client.post(f"/api/projects/{project_id}/generate", json={"target": "both", "expected_revision": 0})
    assert response.status_code == 409


def test_unrecognized_citations_are_rejected():
    with pytest.raises(HTTPException) as error:
        research.parse_generated('{"draft_zh":"A claim [@ref_fake]."}', ["draft_zh"], {"ref_real"})
    assert error.value.status_code == 502


def test_reference_verification_cannot_be_claimed_by_client(research_env):
    client, project_id, _ = research_env
    response = client.post(f"/api/projects/{project_id}/references", json={
        "title": "Unverified claim", "doi": "10.1234/not-verified", "verified": True,
        "metadata": {"source": "Crossref"}})
    assert response.status_code == 200, response.text
    reference = response.json()
    assert reference["verified"] is False
    core.update_project(project_id, {"draft_zh": "引用 [@" + reference["id"] + "]", "expected_revision": 0})
    assert client.delete(f"/api/projects/{project_id}/references/{reference['id']}").status_code == 409


def test_crossref_normalization_does_not_claim_fulltext():
    record = research.crossref_record({"DOI": "10.1234/TEST", "title": ["<i>Evidence</i>"],
        "author": [{"given": "Ming", "family": "Li"}], "issued": {"date-parts": [[2024, 1]]}})
    assert record["doi"] == "10.1234/test"
    assert record["title"] == "Evidence"
    assert record["abstract"] == ""
    assert record["metadata"]["evidence_level"] == "metadata"


def test_provider_error_does_not_echo_credentials(research_env, monkeypatch):
    client, _, _ = research_env
    configure(client)
    original_client = providers.httpx.AsyncClient
    def make_client(**kwargs):
        async def handler(request):
            assert request.headers["authorization"] == "Bearer private-secret"
            return providers.httpx.Response(401, json={"error": "echo: private-secret"})
        return original_client(transport=providers.httpx.MockTransport(handler), **kwargs)
    monkeypatch.setattr(providers.httpx, "AsyncClient", make_client)
    response = client.post("/api/settings/test")
    assert response.status_code == 502
    assert "private-secret" not in response.text


def test_literature_failure_is_visible_without_fabricated_sources(research_env, monkeypatch):
    client, project_id, _ = research_env
    configure(client)
    async def fail_search(*args, **kwargs):
        raise HTTPException(502, "检索失败")
    async def fake_complete(messages, **kwargs):
        assert "外部文献检索失败" in messages[0]["content"]
        return "文献检索暂时不可用，可以先明确研究问题。"
    monkeypatch.setattr(research, "search_literature", fail_search)
    monkeypatch.setattr(providers, "complete", fake_complete)
    response = client.post(f"/api/projects/{project_id}/chat", json={"message": "一个问题", "search_literature": True})
    assert response.status_code == 200
    assert response.json()["warnings"] == ["检索失败"]
    assert response.json()["sources"] == []


def test_long_draft_is_not_silently_truncated_for_generation(research_env, monkeypatch):
    client, project_id, _ = research_env
    configure(client)
    original = "研究内容" * 8000
    core.update_project(project_id, {"draft_zh": original, "expected_revision": 0})
    response = client.post(f"/api/projects/{project_id}/generate", json={"target": "both", "expected_revision": 1})
    assert response.status_code == 422
    assert core.get_project(project_id)["draft_zh"] == original


def test_grouped_citations_are_checked_individually():
    result = research.parse_generated('{"draft_zh":"A claim [@ref_one; @ref_two]."}', ["draft_zh"], {"ref_one", "ref_two"})
    assert "@ref_two" in result["draft_zh"]
    with pytest.raises(HTTPException):
        research.parse_generated('{"draft_zh":"A claim [@ref_one; @ref_fake]."}', ["draft_zh"], {"ref_one"})


@pytest.mark.parametrize("query", [
    "10.1038/nature14539", "doi: 10.1038/nature14539", "https://doi.org/10.1038/nature14539",
    "https://dx.doi.org/10.1038%2Fnature14539",
])
def test_doi_search_uses_exact_single_work_lookup(research_env, monkeypatch, query):
    client, _, _ = research_env
    original_client = research.httpx.AsyncClient
    requests = []
    def make_client(**kwargs):
        async def handler(request):
            requests.append(request)
            assert request.url.raw_path == b"/works/10.1038%2Fnature14539"
            assert not request.url.query
            return research.httpx.Response(200, json={"message": {
                "DOI": "10.1038/nature14539", "title": ["Deep learning"],
                "issued": {"date-parts": [[2015]]}, "type": "journal-article",
            }})
        return original_client(transport=research.httpx.MockTransport(handler), **kwargs)
    monkeypatch.setattr(research.httpx, "AsyncClient", make_client)
    response = client.post("/api/literature/search", json={"query": query, "limit": 15})
    assert response.status_code == 200, response.text
    assert len(requests) == 1
    assert len(response.json()["items"]) == 1
    assert response.json()["items"][0]["title"] == "Deep learning"
    assert response.json()["items"][0]["doi"] == "10.1038/nature14539"


def test_doi_not_found_is_explicit_without_keyword_fallback(research_env, monkeypatch):
    client, _, _ = research_env
    original_client = research.httpx.AsyncClient
    def make_client(**kwargs):
        async def handler(request):
            assert request.url.raw_path == b"/works/10.1234%2Fmissing"
            return research.httpx.Response(404, text="Resource not found.")
        return original_client(transport=research.httpx.MockTransport(handler), **kwargs)
    monkeypatch.setattr(research.httpx, "AsyncClient", make_client)
    response = client.post("/api/literature/search", json={"query": "doi:10.1234/missing"})
    assert response.status_code == 404
    assert "未找到这条 DOI" in response.json()["detail"]
