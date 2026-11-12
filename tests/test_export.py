import json
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from backend import core
from backend.export_routes import router, render_citations, tex_escape, filename_stem


@pytest.fixture
def export_context(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "DATA_DIR", tmp_path / "data")
    core.init_db()
    with core.conn() as db:
        project = dict(db.execute("SELECT * FROM projects LIMIT 1").fetchone())
        db.execute("INSERT INTO settings VALUES (?,?)", ("export_dir", json.dumps(str(tmp_path / "outputs"))))
        ref = {"id": "ref_a1", "project_id": project["id"], "title": "Evidence for Research Writing",
               "authors": json.dumps([{"family": "Smith", "given": "Jane"}]), "year": 2024,
               "doi": "10.1234/example", "url": "https://doi.org/10.1234/example", "abstract": "",
               "publisher": "Example Press", "type": "journal-article", "verified": 1,
               "metadata": json.dumps({"container-title": ["Research Journal"], "volume": "3", "issue": "2", "page": "10-20"})}
        db.execute('INSERT INTO "references" (' + ",".join(ref) + ') VALUES (' + ",".join("?" for _ in ref) + ')', tuple(ref.values()))
    core.update_project(project["id"], {"title": "研究与证据", "draft_zh": "# 研究与证据\n\n中文引用 [@ref_a1]。\n\n## 方法\n\n- 有来源的陈述\n- 待验证的假设\n", "draft_en": "# Research and evidence\n\nEvidence supports writing [@ref_a1].\n\n## Methods\n\nWe separate ideas from evidence.", "expected_revision": 0})
    app = FastAPI()
    app.include_router(router)
    return TestClient(app), project["id"], ref, tmp_path


def test_real_csl_styles_render_differently(export_context):
    _, _, ref, _ = export_context
    apa, _ = render_citations("Claim [@ref_a1].", [ref], "apa", "en")
    ieee, _ = render_citations("Claim [@ref_a1].", [ref], "ieee", "en")
    gbt, _ = render_citations("观点 [@ref_a1]。", [ref], "gbt", "zh")
    assert "(Smith, 2024)" in apa
    assert "[1]" in ieee
    assert "[1]" in gbt
    assert "参考文献" in gbt
    assert "Research Journal" in apa
    assert "[@" not in apa + ieee + gbt
    assert apa != ieee != gbt


def test_citation_group_and_unknown_reference(export_context):
    _, _, ref, _ = export_context
    other = dict(ref, id="ref_b2", title="Second evidence", year=2025)
    output, _ = render_citations("Two [@ref_a1; @ref_b2].", [ref, other], "ieee", "en")
    assert "Second evidence" in output
    with pytest.raises(HTTPException) as error:
        render_citations("Invalid [@other_project].", [ref], "apa", "en")
    assert error.value.status_code == 422
    with pytest.raises(HTTPException):
        render_citations("Invalid [@not closed", [ref], "apa", "en")


def test_multiformat_snapshot_and_safe_download(export_context):
    client, project_id, _, tmp = export_context
    response = client.post(f"/api/projects/{project_id}/export", json={"languages": ["zh", "en"], "formats": ["md", "docx", "pdf", "tex", "bib"], "style": "apa"})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["snapshot_revision"] == 1
    assert len(data["files"]) == 9
    assert len({item["name"] for item in data["files"]}) == 9
    for item in data["files"]:
        path = Path(item["path"])
        assert path.is_relative_to(tmp / "outputs") and path.stat().st_size > 0
        download = client.get(item["url"])
        assert download.status_code == 200
        assert download.content == path.read_bytes()
        if path.suffix == ".pdf":
            assert download.content.startswith(b"%PDF-")
        if path.suffix == ".docx":
            from docx import Document
            texts = "\n".join(p.text for p in Document(path).paragraphs)
            assert "Smith, 2024" in texts
        if path.suffix == ".bib":
            assert "@article{ref_a1," in path.read_text(encoding="utf-8")
    assert client.get(f"/api/exports/{data['token']}/unknown.txt").status_code == 404
    assert client.get("/api/exports/invalid_token/any.txt").status_code == 404
    assert client.get(f"/api/exports/{data['token']}/..%5Csecret.txt").status_code == 404


def test_empty_language_fails_before_writing(export_context):
    client, project_id, _, tmp = export_context
    core.update_project(project_id, {"draft_en": "", "expected_revision": 1})
    result = client.post(f"/api/projects/{project_id}/export", json={"languages": ["zh", "en"], "formats": ["md"]})
    assert result.status_code == 422
    assert not (tmp / "outputs").exists()


def test_no_references_no_fake_bibliography(export_context):
    client, project_id, _, _ = export_context
    core.update_project(project_id, {"draft_zh": "尚未插入引用。", "expected_revision": 1})
    result = client.post(f"/api/projects/{project_id}/export", json={"languages": ["zh"], "formats": ["md"]})
    assert result.status_code == 200
    data = result.json()
    assert any("未插入" in notice for notice in data["warnings"])
    assert "参考文献" not in Path(data["files"][0]["path"]).read_text(encoding="utf-8")


def test_filename_and_tex_escaping():
    assert "/" not in filename_stem("../escape\\title:bad")
    assert filename_stem("CON") == "paper_CON"
    assert tex_escape("a_b & 10% {$x} \\input{secret}") == r"a\_b \& 10\% \{\$x\} \textbackslash{}input\{secret\}"
