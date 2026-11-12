"""Local, transactional persistence shared by the workbench services."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import uuid

from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[1]
def configured_data_dir():
    fallback = ROOT / "data"
    try:
        config = json.loads((ROOT / ".state" / "storage.json").read_text("utf-8"))
        fallback = Path(config["data_dir"])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return Path(os.environ.get("PAPER_WORKBENCH_DATA", str(fallback))).resolve()


DATA_DIR = configured_data_dir()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def uid() -> str:
    return uuid.uuid4().hex


def json_load(value, default=None):
    if value is None:
        return default
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return default


@contextmanager
def conn():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DATA_DIR / "workbench.sqlite3", timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=30000")
    try:
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def init_db():
    with conn() as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript('''
        CREATE TABLE IF NOT EXISTS projects (
          id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
          discipline TEXT NOT NULL DEFAULT '通用研究', created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '',
          draft_zh TEXT NOT NULL DEFAULT '', draft_en TEXT NOT NULL DEFAULT '',
          revision INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS chats (
          id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
          role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL,
          sources TEXT NOT NULL DEFAULT '[]'
        );
        CREATE INDEX IF NOT EXISTS chats_project ON chats(project_id,created_at);
        CREATE TABLE IF NOT EXISTS sources (
          id TEXT PRIMARY KEY, title TEXT NOT NULL, provider TEXT NOT NULL,
          kind TEXT NOT NULL, created_at TEXT NOT NULL, message_count INTEGER NOT NULL DEFAULT 0,
          source_file TEXT NOT NULL, metadata TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS source_messages (
          id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(id),
          role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT,
          parent_id TEXT, branch TEXT NOT NULL DEFAULT 'main', ordinal INTEGER NOT NULL,
          fingerprint TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS source_messages_source ON source_messages(source_id,ordinal);
        CREATE INDEX IF NOT EXISTS source_messages_fingerprint ON source_messages(fingerprint);
        CREATE TABLE IF NOT EXISTS imports (
          id TEXT PRIMARY KEY, filename TEXT NOT NULL, status TEXT NOT NULL,
          progress REAL NOT NULL DEFAULT 0, total INTEGER NOT NULL DEFAULT 0,
          imported INTEGER NOT NULL DEFAULT 0, duplicates INTEGER NOT NULL DEFAULT 0,
          error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          file_hash TEXT NOT NULL DEFAULT '', bytes_total INTEGER NOT NULL DEFAULT 0,
          warnings TEXT NOT NULL DEFAULT '[]'
        );
        CREATE INDEX IF NOT EXISTS imports_hash ON imports(file_hash,status);
        CREATE TABLE IF NOT EXISTS "references" (
          id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
          title TEXT NOT NULL, authors TEXT NOT NULL DEFAULT '[]', year INTEGER,
          doi TEXT NOT NULL DEFAULT '', url TEXT NOT NULL DEFAULT '',
          abstract TEXT NOT NULL DEFAULT '', publisher TEXT NOT NULL DEFAULT '',
          type TEXT NOT NULL DEFAULT 'article-journal', verified INTEGER NOT NULL DEFAULT 0,
          metadata TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS revisions (
          id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
          kind TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS revisions_project ON revisions(project_id,created_at);
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        ''')
        project_columns={row[1] for row in db.execute("PRAGMA table_info(projects)")}
        if "deleted_at" not in project_columns:
            db.execute("ALTER TABLE projects ADD COLUMN deleted_at TEXT")
        import_columns={row[1] for row in db.execute("PRAGMA table_info(imports)")}
        for name,definition in {"source_path":"TEXT","scan_snapshot":"TEXT","checkpoint":"INTEGER NOT NULL DEFAULT 0"}.items():
            if name not in import_columns:db.execute(f"ALTER TABLE imports ADD COLUMN {name} {definition}")
        # Additive migrations retain original projects and archives.
        existing_columns = {row[1] for row in db.execute("PRAGMA table_info(source_messages)")}
        for name, definition in {"model": "TEXT", "author_name": "TEXT", "original_time": "TEXT", "edited_at": "TEXT", "metadata": "TEXT NOT NULL DEFAULT '{}'"}.items():
            if name not in existing_columns:
                db.execute(f"ALTER TABLE source_messages ADD COLUMN {name} {definition}")
        db.execute("CREATE TABLE IF NOT EXISTS source_message_versions (id TEXT PRIMARY KEY,message_id TEXT NOT NULL,content TEXT NOT NULL,role TEXT NOT NULL,model TEXT,created_at TEXT,edited_at TEXT,imported_at TEXT NOT NULL,origin TEXT NOT NULL,metadata TEXT NOT NULL)")
        try:
            db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(message_id UNINDEXED, title, content)")
        except sqlite3.OperationalError:
            pass
        db.execute("UPDATE imports SET status='interrupted', error='应用重启，任务已中断；可点击继续，已完成内容会保留并自动去重。',updated_at=? WHERE status IN ('queued','processing','cancelling')", (now(),))
        if not db.execute("SELECT 1 FROM projects LIMIT 1").fetchone():
            stamp = now()
            db.execute("INSERT INTO projects(id,title,created_at,updated_at) VALUES (?,?,?,?)", (uid(), "我的第一篇研究", stamp, stamp))


def get_project(project_id: str) -> dict:
    with conn() as db:
        row = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "项目不存在")
        if row["deleted_at"]:raise HTTPException(404,"项目已移入回收站，请先恢复")
        return dict(row)


PROJECT_FIELDS = {"title", "description", "discipline", "notes", "draft_zh", "draft_en"}


def update_project(project_id: str, changes: dict) -> dict:
    """Compare-and-swap project saves; a snapshot is kept before every actual change."""
    expected = changes.get("expected_revision")
    clean = {k: v for k, v in changes.items() if k in PROJECT_FIELDS}
    if any(not isinstance(v, str) for v in clean.values()):
        raise HTTPException(422, "项目字段必须是文本")
    if "title" in clean:
        clean["title"] = clean["title"].strip()
        if not clean["title"] or len(clean["title"]) > 240:
            raise HTTPException(422, "项目名称需要 1–240 个字符")
    if any(len(v) > 4_000_000 for v in clean.values()):
        raise HTTPException(422, "单个字段过大，请拆分稿件")
    with conn() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "项目不存在")
        current = dict(row)
        if current.get("deleted_at"):raise HTTPException(409,"项目已移入回收站，请先恢复")
        if expected is not None and expected != current["revision"]:
            raise HTTPException(409, {"message": "内容已有新版本，当前修改尚未覆盖，请重新读取后合并。", "current_revision": current["revision"]})
        clean = {k: v for k, v in clean.items() if v != current[k]}
        if clean:
            db.execute("INSERT INTO revisions VALUES (?,?,?,?,?)", (uid(), project_id, "snapshot", json.dumps(current, ensure_ascii=False), now()))
            columns = ",".join(f"{key}=?" for key in clean)
            db.execute(f"UPDATE projects SET {columns},updated_at=?,revision=revision+1 WHERE id=?", (*clean.values(), now(), project_id))
        return dict(db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone())


def search_messages(query: str, limit: int = 20) -> list[dict]:
    query = query.strip()[:500]
    if not query:
        return []
    limit = max(1, min(limit, 100))
    result = []
    with conn() as db:
        # Quoted tokens avoid interpreting user input as FTS operators.
        terms = [word for word in query.split() if word][:12]
        match = " OR ".join('"' + word.replace('"', '""') + '"' for word in terms)
        try:
            rows = db.execute('''SELECT m.*,s.title,s.provider,s.source_file
              FROM messages_fts f JOIN source_messages m ON m.id=f.message_id
              JOIN sources s ON s.id=m.source_id WHERE messages_fts MATCH ?
              ORDER BY bm25(messages_fts) LIMIT ?''', (match, limit)).fetchall()
            result = [dict(row) for row in rows]
        except sqlite3.OperationalError:
            pass
        # FTS unicode61 does not segment Chinese. Literal substring retrieval is the
        # compatibility path; it returns bounded results without slicing messages.
        if len(result) < limit:
            seen = {item["id"] for item in result}
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            rows = db.execute('''SELECT m.*,s.title,s.provider,s.source_file FROM source_messages m
              JOIN sources s ON s.id=m.source_id
              WHERE m.content LIKE ? ESCAPE '\\' OR s.title LIKE ? ESCAPE '\\'
              ORDER BY m.created_at DESC,m.ordinal LIMIT ?''', (f"%{escaped}%", f"%{escaped}%", limit)).fetchall()
            result += [dict(row) for row in rows if row["id"] not in seen]
    return result[:limit]
