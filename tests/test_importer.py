import json
from pathlib import Path

import pytest

from backend import core
from backend.importer import iter_json, iter_markdown, iter_html, normalize_conversation, process_import, store_conversation


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "DATA_DIR", tmp_path)
    core.init_db()
    return tmp_path


def conversation():
    return {"id": "conv-1", "title": "教育公平", "current_node": "b", "mapping": {
        "root": {"parent": None, "message": None},
        "u": {"parent": "root", "message": {"id": "u", "author": {"role": "user"}, "create_time": 1, "content": {"parts": ["技术能否改善教育公平？"]}}},
        "a": {"parent": "u", "message": {"id": "a", "author": {"role": "assistant"}, "create_time": 2, "content": {"parts": ["最初的解释"]}}},
        "b": {"parent": "u", "message": {"id": "b", "author": {"role": "assistant"}, "create_time": 3, "content": {"parts": ["修订后的解释"]}}},
    }}


def test_preserve_all_branches_and_selected_path():
    value = normalize_conversation(conversation(), "export.json")
    assert len(value["messages"]) == 3
    assert [(m["external_id"], m["branch"]) for m in value["messages"]] == [("u", "main"), ("a", "alternative"), ("b", "main")]
    assert value["messages"][2]["parent_id"] == "u"


def test_incremental_import_and_changed_selection(database):
    raw = conversation()
    assert store_conversation(normalize_conversation(raw, "a.json"), "a.json", "a.json")
    assert not store_conversation(normalize_conversation(raw, "b.json"), "b.json", "b.json")
    raw["current_node"] = "a"
    assert store_conversation(normalize_conversation(raw, "c.json"), "c.json", "c.json")
    raw["mapping"]["new"] = {"parent": "a", "message": {"id": "new", "author": {"role": "user"}, "create_time": 4, "content": {"parts": ["我不同意这个结论"]}}}
    raw["current_node"] = "new"
    assert store_conversation(normalize_conversation(raw, "d.json"), "d.json", "d.json")
    with core.conn() as db:
        assert db.execute("SELECT count(*) FROM sources").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM source_messages").fetchone()[0] == 4
        assert db.execute("SELECT branch FROM source_messages WHERE content='最初的解释'").fetchone()[0] == "main"
        assert db.execute("SELECT branch FROM source_messages WHERE content='修订后的解释'").fetchone()[0] == "alternative"
        reply = db.execute("SELECT parent_id FROM source_messages WHERE content='最初的解释'").fetchone()
        assert db.execute("SELECT role FROM source_messages WHERE id=?", (reply["parent_id"],)).fetchone()[0] == "user"
    assert core.search_messages("教育公平")[0]["role"] == "user"


def test_repeated_identical_turns_are_not_deleted(database):
    raw = {"id": "repeat", "messages": [{"role": "user", "content": "继续"}, {"role": "assistant", "content": "论证一"}, {"role": "user", "content": "继续"}]}
    record = normalize_conversation(raw, "repeat.json")
    store_conversation(record, "repeat.json", "repeat.json")
    with core.conn() as db:
        assert db.execute("SELECT count(*) FROM source_messages WHERE content='继续'").fetchone()[0] == 2


def test_cross_format_duplicate_preserves_provider_metadata(database):
    original = normalize_conversation({"id": "same", "provider": "ChatGPT", "messages": [{"role": "user", "content": "同一内容"}]}, "a.json")
    store_conversation(original, "a.json", "a.json")
    markdown = normalize_conversation({"provider": "Markdown", "messages": [{"role": "user", "content": "同一内容"}]}, "b.md")
    assert not store_conversation(markdown, "b.md", "b.md")
    with core.conn() as db:
        row = db.execute("SELECT * FROM sources").fetchone()
        assert row["provider"] == "ChatGPT"
        metadata = json.loads(row["metadata"])
        assert metadata["external_id"] == "same"
        assert len(metadata["origins"]) == 2


@pytest.mark.parametrize("wrapped", [False, True])
def test_json_streaming_items(database, wrapped):
    path = database / "archive.json"
    data = [conversation(), {"uuid": "claude-1", "name": "方法", "chat_messages": [{"uuid": "m1", "sender": "human", "text": "资料"}]}]
    path.write_text(json.dumps({"conversations": data} if wrapped else data), encoding="utf-8")
    result = list(iter_json(path, "archive.json"))
    assert [item[0]["provider"] for item in result] == ["ChatGPT", "Claude"]


def test_invalid_json_reports_error_and_preserves_partial_import(database):
    path = database / "broken.json"
    path.write_text("[" + json.dumps(conversation()) + ", invalid]", encoding="utf-8")
    with core.conn() as db:
        db.execute("INSERT INTO imports(id,filename,status,created_at,updated_at) VALUES ('broken','broken.json','queued',?,?)", (core.now(), core.now()))
    process_import("broken", path, "broken.json")
    with core.conn() as db:
        job = db.execute("SELECT * FROM imports WHERE id='broken'").fetchone()
        assert job["status"] == "error"
        assert job["error"]


def test_markdown_roles_and_unstructured_document(database):
    path = database / "history.md"
    path.write_text("# 研究\n\n## User\n我的问题\n\n## Assistant\nAI 的建议", encoding="utf-8")
    parsed = list(iter_markdown(path, path.name))[0][0]
    assert [message["role"] for message in parsed["messages"]] == ["user", "assistant"]
    path.write_text("没有标记的笔记，不能猜测作者。", encoding="utf-8")
    parsed = list(iter_markdown(path, path.name))[0][0]
    assert parsed["kind"] == "document"
    assert parsed["messages"][0]["role"] == "document"


def test_html_scripts_never_become_text(database):
    path = database / "history.html"
    path.write_text('<script>alert("hidden")</script><h1>笔记</h1><p>可见内容</p>', encoding="utf-8")
    parsed = list(iter_html(path, path.name))[0][0]
    assert "可见内容" in parsed["messages"][0]["content"]
    assert "hidden" not in parsed["messages"][0]["content"]
