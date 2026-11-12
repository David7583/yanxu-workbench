import asyncio
from io import BytesIO
import json
from pathlib import Path

from docx import Document
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from backend import core, manuscript_routes as manuscript, integration_routes as integration, providers, task_routes


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "DATA_DIR", tmp_path / "data")
    core.init_db()
    manuscript.init_schema()
    integration.init_schema()
    task_routes.init_schema()
    monkeypatch.setattr(providers, "settings", lambda: {"model": "review-model", "id": "test-provider"})
    monkeypatch.setattr(integration.keyring, "get_password", lambda *args: None)
    app = FastAPI()
    app.include_router(manuscript.router)
    app.include_router(integration.router)
    app.include_router(task_routes.router)
    client = TestClient(app)
    with core.conn() as db:
        project = db.execute("SELECT id FROM projects LIMIT 1").fetchone()[0]
    return client, project, tmp_path


def upload(client, project, content="Evidence from 11 interviews", title="Research question"):
    doc = Document()
    doc.add_heading(title, 1)
    doc.add_paragraph(content)
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "Sample"
    table.cell(1, 0).text = "11"
    stream = BytesIO()
    doc.save(stream)
    raw = stream.getvalue()
    result = client.post(f"/api/projects/{project}/manuscripts", files={"file": ("thesis.docx", raw)})
    assert result.status_code == 201, result.text
    return result.json(), raw


def test_word_extracts_order_locations_and_immutable_original(env):
    client, project, _ = env
    item, raw = upload(client, project)
    assert [b["kind"] for b in item["blocks"]] == ["heading", "paragraph", "table"]
    assert item["blocks"][1]["element"] == 2
    assert "page" not in item["blocks"][1]
    assert "source_path" not in item
    assert client.get(item["source_url"]).content == raw
    assert core.get_project(project)["draft_en"] == ""
    assert client.get(f"/api/projects/{project}/manuscripts").json()["items"][0]["characters"] > 0


def test_duplicate_upload_has_one_source(env):
    client, project, _ = env
    first, raw = upload(client, project)
    again = client.post(f"/api/projects/{project}/manuscripts", files={"file": ("copy.docx", raw)}).json()
    assert again["duplicate"]
    assert again["id"] == first["id"]
    assert len(list((core.DATA_DIR / "manuscripts").glob("*.docx"))) == 1


def test_invalid_file_cleaned(env):
    client, project, _ = env
    bad = client.post(f"/api/projects/{project}/manuscripts", files={"file": ("bad.docx", b"not a Word file")})
    assert bad.status_code == 422
    assert not list((core.DATA_DIR / "manuscripts").glob("*"))
    assert client.post(f"/api/projects/{project}/manuscripts", files={"file": ("run.py", b"print(1)")}).status_code == 415


def test_pdf_page_locations_and_ocr_warning(env):
    from reportlab.pdfgen.canvas import Canvas
    client, project, _ = env
    stream = BytesIO()
    canvas = Canvas(stream)
    canvas.drawString(40, 700, "Study with 18 documents")
    canvas.showPage()
    canvas.showPage()
    canvas.save()
    response = client.post(f"/api/projects/{project}/manuscripts", files={"file": ("thesis.pdf", stream.getvalue())})
    assert response.status_code == 201, response.text
    assert response.json()["blocks"][0]["page"] == 1
    assert any("1 页" in w for w in response.json()["warnings"])


def test_diagnosis_scope_and_provenance(env, monkeypatch):
    client, project, _ = env
    item, _ = upload(client, project)
    captured = []
    async def complete(messages, **kwargs):
        captured.extend(messages)
        return "## Evidence gap\n[M2] More perspectives are needed."
    monkeypatch.setattr(providers, "complete", complete)
    result = client.post(f'/api/manuscripts/{item["id"]}/diagnose', json={"block_ids": ["M2"], "target_journal": "A target"})
    assert result.status_code == 200, result.text
    context = json.loads(captured[1]["content"])
    assert [b["id"] for b in context["source"]["blocks"]] == ["M2"]
    assert result.json()["metadata"]["model"] == "review-model"
    assert client.get(f'/api/manuscripts/{item["id"]}').json()["diagnosis"]
    assert core.get_project(project)["revision"] == 0


def test_long_source_all_text_processed_without_truncation(env, monkeypatch):
    client, project, _ = env
    item, _ = upload(client, project, "text " * 25000)
    fragments = []
    async def complete(messages, **kwargs):
        payload = json.loads(messages[-1]["content"])
        if "blocks" in payload:
            fragments.extend(payload["blocks"])
        return "原文完整分段分析，事实需要核验。"
    monkeypatch.setattr(providers, "complete", complete)
    result = client.post(f'/api/manuscripts/{item["id"]}/diagnose', json={})
    assert result.status_code == 200, result.text
    assert result.json()["metadata"]["coverage"]["coverage_percent"] == 100
    assert result.json()["metadata"]["model_calls"] > 1
    for block in item["blocks"]:
        restored = "".join(part["text"] for part in sorted((part for part in fragments if part["id"] == block["id"]), key=lambda part: part["character_range"][0]))
        assert restored == block["text"]
    bad = client.post(f'/api/manuscripts/{item["id"]}/diagnose', json={"block_ids": ["other"]})
    assert bad.status_code == 422


def test_background_result_persists_and_restart_never_replays(env, monkeypatch):
    import threading
    import time
    from backend import research_routes
    client, project, _ = env
    release = threading.Event()
    calls = []
    async def fake_generate(project_id, body):
        calls.append(project_id)
        while not release.is_set():
            await asyncio.sleep(0.01)
        return {"notes": "Saved background result", "base_revision": 0}
    monkeypatch.setattr(research_routes, "generate", fake_generate)
    with client:
        response = client.post('/api/tasks', json={"kind": "research_generate", "project_id": project, "payload": {"instruction": "Review"}})
        assert response.status_code == 202, response.text
        identifier = response.json()["id"]
        assert client.get('/api/tasks/' + identifier).json()["status"] in {"queued", "running"}
        assert "result" not in client.get('/api/tasks').json()["items"][0]
        release.set()
        for _ in range(100):
            result = client.get('/api/tasks/' + identifier).json()
            if result["status"] == "completed":
                break
            time.sleep(0.01)
        assert result["result"]["notes"] == "Saved background result"
        assert client.post('/api/tasks/' + identifier + '/acknowledge', json={"disposition": "dismissed"}).json()["reviewed"] == "dismissed"
    with core.conn() as db:
        db.execute("INSERT INTO work_tasks(id,kind,project_id,status,payload,created_at,updated_at) VALUES('orphan','research_generate',?,'running','{}',?,?)", (project, core.now(), core.now()))
    task_routes.init_schema()
    assert task_routes.get_task("orphan")["status"] == "interrupted"
    assert task_routes.get_task(identifier)["result"]["notes"] == "Saved background result"
    assert calls == [project]


def test_proposal_only_applies_after_review_and_preserves_snapshot(env, monkeypatch):
    client, project, _ = env
    item, raw = upload(client, project)
    core.update_project(project, {"draft_en": "Original", "expected_revision": 0})
    async def complete(*args, **kwargs):
        return json.dumps({"notes": "Review required", "draft_en": "11 interviews"})
    monkeypatch.setattr(providers, "complete", complete)
    result = client.post(f'/api/manuscripts/{item["id"]}/proposal', json={"language": "en"})
    assert result.status_code == 200, result.text
    proposal = result.json()
    assert core.get_project(project)["draft_en"] == "Original"
    result = client.post(f'/api/manuscript-proposals/{proposal["id"]}/apply', json={"expected_revision": 1})
    assert result.status_code == 200, result.text
    assert core.get_project(project)["draft_en"] == "11 interviews"
    assert client.get(item["source_url"]).content == raw
    with core.conn() as db:
        snapshots = [json.loads(r["content"]) for r in db.execute("SELECT content FROM revisions WHERE project_id=?", (project,))]
    assert any(s["draft_en"] == "Original" for s in snapshots)
    assert client.post(f'/api/manuscript-proposals/{proposal["id"]}/apply', json={"expected_revision": 1}).status_code == 409


def test_stale_proposal_and_fabricated_citations_rejected(env, monkeypatch):
    client, project, _ = env
    item, _ = upload(client, project)
    async def invalid(*args, **kwargs):
        return json.dumps({"notes": "Reason", "draft_zh": "结论 [@nonexistent]"})
    monkeypatch.setattr(providers, "complete", invalid)
    assert client.post(f'/api/manuscripts/{item["id"]}/proposal', json={"language": "zh"}).status_code == 502
    async def valid(*args, **kwargs):
        return json.dumps({"notes": "Reason", "draft_zh": "研究建议"})
    monkeypatch.setattr(providers, "complete", valid)
    proposal = client.post(f'/api/manuscripts/{item["id"]}/proposal', json={"language": "zh"}).json()
    core.update_project(project, {"notes": "A newer human edit", "expected_revision": 0})
    for revision in [0, 1]:
        assert client.post(f'/api/manuscript-proposals/{proposal["id"]}/apply', json={"expected_revision": revision}).status_code == 409
    assert core.get_project(project)["notes"] == "A newer human edit"


def configure_vault(client, tmp):
    vault = tmp / "vault"
    vault.mkdir()
    result = client.put("/api/integrations", json={"vault_path": str(vault), "export_subdir": "Research"})
    assert result.status_code == 200
    return vault


def test_obsidian_scoped_scan_and_read(env):
    client, _, tmp = env
    vault = configure_vault(client, tmp)
    (vault / "研究.md").write_text("# Evidence\nA personal idea", encoding="utf-8")
    (vault / ".obsidian").mkdir()
    (vault / ".obsidian" / "private.md").write_text("Settings")
    listed = client.get("/api/integrations/obsidian/notes").json()
    assert [i["path"] for i in listed["items"]] == ["研究.md"]
    read = client.get("/api/integrations/obsidian/note", params={"path": "研究.md"}).json()
    assert read["content"].startswith("# Evidence")
    assert read["evidence_level"] == "personal_note_unverified"
    for bad in ["../outside.md", ".obsidian/private.md", str(tmp / "outside.md")]:
        assert client.get("/api/integrations/obsidian/note", params={"path": bad}).status_code == 422


def test_obsidian_import_is_idempotent_and_checks_revision(env):
    client, project, tmp = env
    vault = configure_vault(client, tmp)
    (vault / "note.md").write_text("A real note", encoding="utf-8")
    response = client.post(f"/api/projects/{project}/obsidian/import", json={"path": "note.md", "expected_revision": 0})
    assert response.status_code == 200, response.text
    assert "A real note" in response.json()["project"]["notes"]
    assert client.post(f"/api/projects/{project}/obsidian/import", json={"path": "note.md"}).json()["duplicate"]
    (vault / "note.md").write_text("Updated note", encoding="utf-8")
    assert client.post(f"/api/projects/{project}/obsidian/import", json={"path": "note.md", "expected_revision": 0}).status_code == 409
    assert "Updated note" not in core.get_project(project)["notes"]


def test_obsidian_export_collision_requires_matching_hash(env):
    client, project, tmp = env
    vault = configure_vault(client, tmp)
    core.update_project(project, {"notes": "My research", "expected_revision": 0})
    response = client.post(f"/api/projects/{project}/obsidian/export", json={"filename": "card"})
    assert response.status_code == 200, response.text
    first_hash = response.json()["sha256"]
    target = vault / "Research" / "card.md"
    assert "My research" in target.read_text("utf-8")
    target.write_text("Edited in Obsidian", encoding="utf-8")
    assert client.post(f"/api/projects/{project}/obsidian/export", json={"filename": "card", "expected_hash": first_hash}).status_code == 409
    assert target.read_text("utf-8") == "Edited in Obsidian"
    note = client.get("/api/integrations/obsidian/note", params={"path": "Research/card.md"}).json()
    response = client.post(f"/api/projects/{project}/obsidian/export", json={"filename": "card", "expected_hash": note["sha256"]})
    assert response.status_code == 200
    assert client.post(f"/api/projects/{project}/obsidian/export", json={"filename": "../other"}).status_code == 422


def test_integration_credentials_bound_to_endpoint_and_not_stored(env, monkeypatch):
    client, _, _ = env
    secrets = {}
    monkeypatch.setattr(integration.keyring, "get_password", lambda a, b: secrets.get((a, b)))
    monkeypatch.setattr(integration.keyring, "set_password", lambda a, b, c: secrets.__setitem__((a, b), c))
    response = client.put("/api/integrations", json={"zotero_api_key": "private-token"})
    assert response.json()["zotero_has_key"]
    assert "private-token" not in response.text
    response = client.put("/api/integrations", json={"zotero_url": "https://api.zotero.org"})
    assert not response.json()["zotero_has_key"]
    with core.conn() as db:
        assert "private-token" not in str([tuple(r) for r in db.execute("SELECT * FROM integration_settings")])
    assert client.put("/api/integrations", json={"zotero_url": "https://unrelated.example"}).status_code == 422


def test_zotero_import_links_stable_id_and_never_claims_verified(env, monkeypatch):
    client, project, _ = env
    async def get(path, params=None, config=None):
        return {"key": "ABCD1234", "version": 2, "data": {"itemType": "journalArticle", "title": "Digital participation", "DOI": "https://doi.org/10.1234/Example", "date": "2025-08", "creators": [{"creatorType": "author", "lastName": "Yang", "firstName": "R"}], "abstractNote": "Abstract only"}}, {}
    monkeypatch.setattr(integration, "_zotero_get", get)
    first = client.post(f"/api/projects/{project}/zotero/import", json={"keys": ["ABCD1234"]})
    assert first.status_code == 200, first.text
    second = client.post(f"/api/projects/{project}/zotero/import", json={"keys": ["ABCD1234"]})
    assert first.json()["items"][0]["reference_id"] == second.json()["items"][0]["reference_id"]
    with core.conn() as db:
        refs = db.execute('SELECT * FROM "references"').fetchall()
    assert len(refs) == 1
    assert refs[0]["verified"] == 0
    assert refs[0]["doi"] == "10.1234/example"
    assert json.loads(refs[0]["metadata"])["claim_verified"] is False


def test_zotero_failed_selection_is_atomic(env, monkeypatch):
    from fastapi import HTTPException
    client, project, _ = env
    async def get(path, params=None, config=None):
        if path.endswith("ZZZZ9999"):
            raise HTTPException(404, "missing")
        return {"key": "ABCD1234", "data": {"itemType": "journalArticle", "title": "One"}}, {}
    monkeypatch.setattr(integration, "_zotero_get", get)
    response = client.post(f"/api/projects/{project}/zotero/import", json={"keys": ["ABCD1234", "ZZZZ9999"]})
    assert response.status_code == 404
    with core.conn() as db:
        assert db.execute('SELECT count(*) FROM "references"').fetchone()[0] == 0
