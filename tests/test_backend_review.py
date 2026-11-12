"""Regression coverage for provenance issues found during integration review."""
import json
import time

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from backend import core, providers, research_routes as research
from backend.importer import normalize_conversation, store_conversation


@pytest.fixture
def reviewed_env(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "DATA_DIR", tmp_path / "data")
    core.init_db()
    monkeypatch.setattr(providers, "api_key", lambda *args, **kwargs: "local-test-key")
    research._project_locks.clear()
    research._literature_cache.clear()
    app = FastAPI()
    app.include_router(research.router)
    with core.conn() as db:
        project_id = db.execute("SELECT id FROM projects").fetchone()[0]
    with TestClient(app) as client:
        yield client, project_id


def test_verified_lookup_corrects_manual_record_without_breaking_citations(reviewed_env):
    client, project_id = reviewed_env
    first = client.post(f"/api/projects/{project_id}/references", json={"title": "Incorrect manual title", "doi": "10.1234/test", "year": 1999}).json()
    assert first["verified"] is False
    metadata = research.crossref_record({"DOI": "10.1234/test", "title": ["Verified source title"], "issued": {"date-parts": [[2025]]}})
    research._literature_cache["10.1234/test"] = (time.monotonic(), metadata)
    second = client.post(f"/api/projects/{project_id}/references", json={"title": "Ignored client title", "doi": "https://doi.org/10.1234/TEST"})
    assert second.status_code == 200, second.text
    reference = second.json()
    assert reference["id"] == first["id"]
    assert reference["title"] == "Verified source title"
    assert reference["verified"] is True
    assert reference["metadata"]["evidence_level"] == "metadata"
    assert len(client.get(f"/api/projects/{project_id}/references").json()["items"]) == 1


def test_alternative_source_context_follows_ancestors_not_siblings(reviewed_env):
    raw = {"id": "branch-demo", "current_node": "selected", "mapping": {
        "q": {"parent": None, "message": {"id": "q", "author": {"role": "user"}, "content": {"parts": ["最初的问题"]}}},
        "a": {"parent": "q", "message": {"id": "a", "author": {"role": "assistant"}, "content": {"parts": ["回答：匹配词甲"]}}},
        "b": {"parent": "q", "message": {"id": "b", "author": {"role": "assistant"}, "content": {"parts": ["无关的另一个替代回答"]}}},
        "selected": {"parent": "q", "message": {"id": "selected", "author": {"role": "assistant"}, "content": {"parts": ["最终选择的回答"]}}},
    }}
    store_conversation(normalize_conversation(raw, "branch.json"), "branch.json", "branch.json")
    result = research.search_history("匹配词甲", limit=1)[0]
    assert "最初的问题" in result["content"]
    assert "另一个替代回答" not in result["content"]
    assert result["branch"] == "alternative"
    assert "不可视为" in result["branch_notice"]


def test_previous_turn_source_label_does_not_rebind_to_new_result(reviewed_env, monkeypatch):
    client, project_id = reviewed_env
    with core.conn() as db:
        db.execute("INSERT INTO chats VALUES (?,?,?,?,?,?)", ("old-answer", project_id, "assistant", "旧的观点 [H1]", core.now(), json.dumps([{"label": "H1", "title": "旧来源"}], ensure_ascii=False)))
    monkeypatch.setattr(research, "search_history", lambda *args, **kwargs: [{"id": "new-source", "label": "H1", "title": "新来源", "content": "新资料", "kind": "history", "branch": "alternative", "branch_notice": "替代分支；不可视为用户采纳", "evidence_level": "未核验"}])
    async def complete(messages, **kwargs):
        old_turn = next(message["content"] for message in messages if message["role"] == "assistant")
        assert "[H1]" not in old_turn
        assert "旧来源" in old_turn
        assert "替代分支" in messages[1]["content"]
        return "本轮相关材料 [H1]，仍需核验。"
    monkeypatch.setattr(providers, "complete", complete)
    response = client.post(f"/api/projects/{project_id}/chat", json={"message": "继续讨论"})
    assert response.status_code == 200, response.text
    assert response.json()["sources"][0]["title"] == "新来源"


def test_transient_chat_labels_are_not_accepted_as_paper_citations():
    with pytest.raises(HTTPException) as error:
        research.parse_generated('{"draft_zh":"一个论断 [H1]"}', ["draft_zh"], set())
    assert error.value.status_code == 502
