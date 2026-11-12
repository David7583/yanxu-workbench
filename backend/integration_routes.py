"""Explicitly selected Zotero/Obsidian sources; no direct third-party DB writes."""
from __future__ import annotations

import hashlib
import html
import json
import os
from pathlib import Path
import re
import threading
from urllib.parse import quote, urlsplit

import httpx
import keyring
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from . import core
from .core import conn, get_project, json_load, now, uid, update_project

router = APIRouter(prefix="/api")
DEFAULTS = {"zotero_url": "http://127.0.0.1:23119/api", "zotero_library": "users/0", "vault_path": "", "export_subdir": "研序"}
_file_lock = threading.Lock()


def init_schema():
    with conn() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS integration_settings (key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS integration_links (
          id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
          service TEXT NOT NULL, external_id TEXT NOT NULL, local_id TEXT NOT NULL,
          version TEXT NOT NULL, metadata TEXT NOT NULL, updated_at TEXT NOT NULL,
          UNIQUE(project_id,service,external_id)
        );
        ''')


def settings():
    config = DEFAULTS.copy()
    with conn() as db:
        for row in db.execute("SELECT key,value FROM integration_settings"):
            if row["key"] in DEFAULTS:
                config[row["key"]] = json_load(row["value"], DEFAULTS[row["key"]])
    return config


def credential_scope(config):
    return "workbench-integrations-" + hashlib.sha256(str(core.ROOT).encode()).hexdigest()[:12], hashlib.sha256(config["zotero_url"].encode()).hexdigest()


def zotero_key(config):
    try:
        return keyring.get_password(*credential_scope(config))
    except Exception:
        return None


def public_settings():
    config = settings()
    return {**config, "zotero_has_key": bool(zotero_key(config))}


def canonical_zotero_url(value):
    value = value.strip().rstrip("/")
    parsed = urlsplit(value)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise HTTPException(422, "Zotero 地址不能包含密码、查询参数或片段。")
    allowed = parsed.hostname in {"localhost", "127.0.0.1", "::1"} and parsed.scheme in {"http", "https"}
    allowed = allowed or (parsed.hostname == "api.zotero.org" and parsed.scheme == "https")
    if not allowed:
        raise HTTPException(422, "请选择本机 Zotero API，或官方 https://api.zotero.org。")
    return value


class IntegrationSettings(BaseModel):
    zotero_url: str | None = Field(default=None, max_length=1000)
    zotero_library: str | None = Field(default=None, max_length=100)
    zotero_api_key: str | None = Field(default=None, max_length=2000)
    delete_zotero_key: bool = False
    vault_path: str | None = Field(default=None, max_length=4000)
    export_subdir: str | None = Field(default=None, max_length=300)


@router.get("/integrations")
def get_settings():
    return public_settings()


@router.put("/integrations")
def set_settings(body: IntegrationSettings):
    config = settings()
    changes = {k: v for k, v in body.model_dump(exclude_none=True).items() if k in DEFAULTS}
    if "zotero_url" in changes:
        changes["zotero_url"] = canonical_zotero_url(changes["zotero_url"])
    if "zotero_library" in changes and not re.fullmatch(r"(?:users|groups)/\d+", changes["zotero_library"]):
        raise HTTPException(422, "文献库格式应为 users/0 或 groups/群组编号。")
    if "vault_path" in changes and changes["vault_path"].strip():
        path = Path(changes["vault_path"]).expanduser()
        if not path.is_absolute() or not path.is_dir():
            raise HTTPException(422, "请选择已存在的 Obsidian 库目录。")
        changes["vault_path"] = str(path.resolve())
    if "export_subdir" in changes:
        subdir = Path(changes["export_subdir"])
        if not str(subdir).strip() or subdir.is_absolute() or any(part in {"..", ".obsidian"} or part.startswith(".") for part in subdir.parts):
            raise HTTPException(422, "导出子目录必须位于笔记库内，且不能指向隐藏配置目录。")
    updated = config | changes
    if body.zotero_api_key and body.delete_zotero_key:
        raise HTTPException(422, "不能同时保存和删除 Zotero 凭据。")
    try:
        if body.zotero_api_key:
            if re.search(r"[\r\n]", body.zotero_api_key):
                raise HTTPException(422, "Zotero 凭据格式无效。")
            keyring.set_password(*credential_scope(updated), body.zotero_api_key.strip())
        elif body.delete_zotero_key and zotero_key(updated):
            keyring.delete_password(*credential_scope(updated))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, "无法保存系统凭据；未将密钥写入普通文件。") from exc
    with conn() as db:
        for key, value in changes.items():
            db.execute("INSERT INTO integration_settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value, ensure_ascii=False)))
    return public_settings()


async def _zotero_get(path, params=None, config=None):
    config = config or settings()
    headers = {"Zotero-API-Version": "3", "Accept": "application/json"}
    secret = zotero_key(config)
    if secret:
        headers["Zotero-API-Key"] = secret
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            response = await client.get(config["zotero_url"] + "/" + path.lstrip("/"), params=params, headers=headers)
        if response.status_code in {401, 403}:
            raise HTTPException(502, "Zotero 拒绝访问。请启用本机通信设置；使用云端库时请检查 API key 和库权限。")
        if response.status_code == 404:
            raise HTTPException(404, "Zotero 中不存在该资源，请检查文献库或条目是否已移动。")
        if response.status_code >= 300:
            raise HTTPException(502, f"Zotero 返回 HTTP {response.status_code}；未更改文献库。")
        return response.json(), dict(response.headers)
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(502, "无法连接 Zotero。请打开 Zotero 并启用“允许此计算机上的其他应用程序与 Zotero 通信”。") from exc


@router.get("/integrations/zotero/status")
async def zotero_status():
    config = settings()
    _, headers = await _zotero_get(config["zotero_library"] + "/items", {"limit": 1, "format": "json"})
    return {"connected": True, "library": config["zotero_library"], "api_version": headers.get("zotero-api-version", "3"), "server_id": headers.get("zotero-server-id"), "mode": "local" if urlsplit(config["zotero_url"]).hostname != "api.zotero.org" else "cloud", "access": "read_only", "message": "已连接 Zotero；工作台仅通过接口读取所选资料。"}


@router.get("/integrations/zotero/collections")
async def zotero_collections(start: int = Query(0, ge=0)):
    data, headers = await _zotero_get(settings()["zotero_library"] + "/collections", {"format": "json", "limit": 100, "start": start})
    if not isinstance(data, list):
        raise HTTPException(502, "Zotero 集合响应格式不正确。")
    return {"items": [{"key": x.get("key"), "name": x.get("data", {}).get("name", "未命名集合"), "parent": x.get("data", {}).get("parentCollection")} for x in data], "total": int(headers.get("total-results", len(data))), "start": start}


def zotero_item(item, config=None):
    data = item.get("data", {})
    authors = []
    for creator in data.get("creators", []):
        if creator.get("creatorType", "author") not in {"author", "editor", "bookAuthor"}:
            continue
        authors.append({"literal": creator["name"]} if creator.get("name") else {"family": creator.get("lastName", ""), "given": creator.get("firstName", "")})
    year = re.search(r"\b(?:18|19|20|21)\d{2}\b", data.get("date", ""))
    doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", data.get("DOI", "").strip(), flags=re.I).lower()
    library = (config or settings())["zotero_library"]
    uri = "zotero://select/" + ("library" if library.startswith("users/") else library) + "/items/" + quote(item.get("key", ""))
    return {"key": item.get("key"), "version": item.get("version", 0), "title": data.get("title") or "未命名条目", "authors": authors,
            "year": year[0] if year else "", "doi": doi, "url": data.get("url", ""), "abstract": data.get("abstractNote", ""),
            "publisher": data.get("publicationTitle") or data.get("publisher", ""), "type": {"journalArticle": "article-journal", "conferencePaper": "paper-conference", "bookSection": "chapter"}.get(data.get("itemType"), data.get("itemType", "article-journal")),
            "tags": [t.get("tag", "") for t in data.get("tags", [])], "zotero_uri": uri,
            "evidence_level": "abstract" if data.get("abstractNote") else "metadata", "claim_verified": False}


@router.get("/integrations/zotero/items")
async def zotero_items(collection: str = "", q: str = "", start: int = Query(0, ge=0)):
    if collection and not re.fullmatch(r"[A-Z0-9]{8}", collection):
        raise HTTPException(422, "Zotero 集合标识无效。")
    config = settings()
    path = config["zotero_library"] + (f"/collections/{collection}/items/top" if collection else "/items/top")
    data, headers = await _zotero_get(path, {"format": "json", "limit": 100, "start": start, "q": q[:300], "itemType": "-attachment"}, config=config)
    if not isinstance(data, list):
        raise HTTPException(502, "Zotero 条目响应格式不正确。")
    return {"items": [zotero_item(x, config) for x in data if x.get("data", {}).get("itemType") not in {"attachment", "note", "annotation"}], "total": int(headers.get("total-results", len(data))), "start": start}


@router.get("/integrations/zotero/items/{key}/children")
async def zotero_children(key: str):
    if not re.fullmatch(r"[A-Z0-9]{8}", key):
        raise HTTPException(422, "Zotero 条目标识无效。")
    data, _ = await _zotero_get(settings()["zotero_library"] + f"/items/{key}/children", {"format": "json", "limit": 100})
    result = []
    for item in data:
        child = item.get("data", {})
        result.append({"key": item.get("key"), "type": child.get("itemType"), "title": child.get("title", ""), "content_type": child.get("contentType", ""),
                       "note": html.unescape(re.sub(r"<[^>]*>", "", child.get("note", ""))), "annotation_text": child.get("annotationText", ""),
                       "annotation_comment": child.get("annotationComment", ""), "page_label": child.get("annotationPageLabel", ""), "filename": child.get("filename", ""), "evidence_level": "user_annotation" if child.get("itemType") == "annotation" else "attachment_metadata"})
    return {"items": result, "claim_verified": False}


class ZoteroImport(BaseModel):
    keys: list[str] = Field(min_length=1, max_length=100)


@router.post("/projects/{project_id}/zotero/import")
async def import_zotero(project_id: str, body: ZoteroImport):
    get_project(project_id)
    if any(not re.fullmatch(r"[A-Z0-9]{8}", key) for key in body.keys):
        raise HTTPException(422, "Zotero 条目标识无效。")
    config = settings()
    records = []
    server_ids = set()
    for key in dict.fromkeys(body.keys):
        raw, headers = await _zotero_get(config["zotero_library"] + "/items/" + key, {"format": "json"}, config=config)
        server_ids.add(headers.get("zotero-server-id", ""))
        if len(server_ids) > 1:
            raise HTTPException(409, "Zotero 文献库在读取期间切换，请重新选择条目。")
        if raw.get("data", {}).get("itemType") in {"attachment", "note", "annotation"}:
            raise HTTPException(422, "请选择父级文献条目；附件和笔记不单独作为参考文献。")
        records.append(zotero_item(raw, config))
    results = []
    # All remote reads finish before writing, so a failed fetch cannot leave a partial selection.
    with conn() as db:
        for record in records:
            server_id = next(iter(server_ids), "")
            external = config["zotero_url"] + "/" + config["zotero_library"] + "/items/" + record["key"] + ("#server=" + server_id if server_id else "")
            link = db.execute("SELECT * FROM integration_links WHERE project_id=? AND service='zotero' AND external_id=?", (project_id, external)).fetchone()
            old = db.execute('SELECT * FROM "references" WHERE id=? AND project_id=?', (link["local_id"], project_id)).fetchone() if link else None
            if not old and record["doi"]:
                old = db.execute('SELECT * FROM "references" WHERE project_id=? AND doi=?', (project_id, record["doi"])).fetchone()
            identifier = old["id"] if old else "ref_" + uid()
            metadata = json_load(old["metadata"], {}) if old else {}
            metadata["zotero"] = {"key": record["key"], "version": record["version"], "library": config["zotero_library"], "server_id": server_id, "url": config["zotero_url"], "uri": record["zotero_uri"], "tags": record["tags"]}
            if not old:
                metadata.update(source="zotero", evidence_level=record["evidence_level"], claim_verified=False)
                db.execute('INSERT INTO "references" (id,project_id,title,authors,year,doi,url,abstract,publisher,type,verified,metadata) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                           (identifier, project_id, record["title"], json.dumps(record["authors"], ensure_ascii=False), record["year"], record["doi"], record["url"], record["abstract"], record["publisher"], record["type"], 0, json.dumps(metadata, ensure_ascii=False)))
            else:
                # Never silently overwrite manually curated or Crossref-verified metadata.
                db.execute('UPDATE "references" SET metadata=? WHERE id=?', (json.dumps(metadata, ensure_ascii=False), identifier))
            db.execute("INSERT INTO integration_links VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(project_id,service,external_id) DO UPDATE SET local_id=excluded.local_id,version=excluded.version,metadata=excluded.metadata,updated_at=excluded.updated_at", (uid(), project_id, "zotero", external, identifier, str(record["version"]), json.dumps(record, ensure_ascii=False), now()))
            results.append({"reference_id": identifier, "key": record["key"], "title": record["title"], "existing": bool(old), "evidence_level": metadata.get("evidence_level", "metadata")})
    return {"items": results, "message": "已关联所选文献；现有文献内容保留。元数据与摘要不代表全文或论点已经核验。"}


def vault_root():
    value = settings()["vault_path"]
    if not value:
        raise HTTPException(409, "请先选择 Obsidian 库目录。")
    root = Path(value).resolve()
    if not root.is_dir():
        raise HTTPException(404, "笔记库目录已移动或不可访问。")
    return root


def confined_path(root, relative):
    relative = relative.replace("\\", "/")
    path = Path(relative)
    if path.is_absolute() or any(part.startswith(".") for part in path.parts) or not path.parts:
        raise HTTPException(422, "请选择库内非隐藏目录中的笔记。")
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root) or resolved.suffix.lower() != ".md":
        raise HTTPException(422, "笔记路径必须位于所选库内，且为 Markdown 文件。")
    return resolved


@router.get("/integrations/obsidian/notes")
def obsidian_notes(q: str = "", limit: int = Query(300, ge=1, le=2000)):
    root = vault_root()
    items, visited, truncated = [], 0, False
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and not (Path(directory) / d).is_symlink())
        for name in sorted(files):
            if not name.lower().endswith(".md") or name.startswith("."):
                continue
            visited += 1
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            if q and q.casefold() not in relative.casefold():
                continue
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            items.append({"path": relative, "title": path.stem, "bytes": stat.st_size, "modified_at": stat.st_mtime, "uri": "obsidian://open?path=" + quote(str(path))})
            if len(items) >= limit:
                truncated = True
                break
        if truncated or visited >= 20000:
            truncated = True
            break
    return {"items": items, "truncated": truncated, "scanned": visited, "vault": root.name}


@router.get("/integrations/obsidian/note")
def obsidian_note(path: str):
    root = vault_root()
    resolved = confined_path(root, path)
    if not resolved.is_file():
        raise HTTPException(404, "笔记不存在。")
    if resolved.stat().st_size > 2 * 1024 * 1024:
        raise HTTPException(413, "单篇笔记超过 2 MB，请选择较小的研究笔记。")
    try:
        raw = resolved.read_bytes()
        text = raw.decode("utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise HTTPException(422, "无法按 UTF-8 读取笔记。") from exc
    return {"path": resolved.relative_to(root).as_posix(), "title": resolved.stem, "content": text, "sha256": hashlib.sha256(raw).hexdigest(), "uri": "obsidian://open?path=" + quote(str(resolved)), "evidence_level": "personal_note_unverified"}


class NoteImport(BaseModel):
    path: str = Field(max_length=4000)
    expected_revision: int | None = Field(default=None, ge=0)


@router.post("/projects/{project_id}/obsidian/import")
def import_obsidian(project_id: str, body: NoteImport):
    project = get_project(project_id)
    note = obsidian_note(body.path)
    external = str(confined_path(vault_root(), body.path))
    with conn() as db:
        existing = db.execute("SELECT * FROM integration_links WHERE project_id=? AND service='obsidian' AND external_id=?", (project_id, external)).fetchone()
    if existing and existing["version"] == note["sha256"]:
        return {"project": project, "duplicate": True, "source": note["path"]}
    notes = project["notes"] + f'\n\n## 来自 Obsidian：{note["title"]}\n\n来源：[{note["path"]}]({note["uri"]}) · 个人笔记，尚未核验\n\n' + note["content"]
    updated = update_project(project_id, {"notes": notes.strip(), "expected_revision": project["revision"] if body.expected_revision is None else body.expected_revision})
    with conn() as db:
        db.execute("INSERT INTO integration_links VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(project_id,service,external_id) DO UPDATE SET version=excluded.version,metadata=excluded.metadata,updated_at=excluded.updated_at", (uid(), project_id, "obsidian", external, project_id, note["sha256"], json.dumps({"path": note["path"], "uri": note["uri"]}, ensure_ascii=False), now()))
    return {"project": updated, "duplicate": False, "source": note["path"], "message": "已作为有来源的个人研究笔记追加；原笔记保持不变。"}


class NoteExport(BaseModel):
    kind: str = "notes"
    filename: str = Field(min_length=1, max_length=200)
    expected_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


@router.post("/projects/{project_id}/obsidian/export")
def export_obsidian(project_id: str, body: NoteExport):
    project = get_project(project_id)
    if body.kind not in {"notes", "draft_zh", "draft_en"}:
        raise HTTPException(422, "请选择研究笔记、中文稿或英文稿。")
    if not project[body.kind].strip():
        raise HTTPException(422, "所选内容为空。")
    name = body.filename.strip()
    if any(c in name for c in '/\\<>:"|?*') or name.startswith(".") or name.endswith((" ", ".")):
        raise HTTPException(422, "请输入不含路径或特殊字符的笔记文件名。")
    if not name.lower().endswith(".md"):
        name += ".md"
    if getattr(os.path, "isreserved", lambda value: False)(name):
        raise HTTPException(422, "该文件名是系统保留名称，请换一个名称。")
    root = vault_root()
    subdir = settings()["export_subdir"]
    target = confined_path(root, str(Path(subdir) / name))
    data = ("---\nworkbench_project: " + json.dumps(project_id) + "\nworkbench_revision: " + str(project["revision"]) + "\nworkbench_kind: " + json.dumps(body.kind) + "\n---\n\n" + project[body.kind] + "\n").encode("utf-8")
    with _file_lock:
        exists = target.exists()
        if exists:
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            if body.expected_hash != digest:
                raise HTTPException(409, {"message": "目标笔记已存在或已经修改。请先预览当前内容，再明确选择覆盖；也可以使用新文件名。", "current_hash": digest, "path": target.relative_to(root).as_posix()})
        elif body.expected_hash:
            raise HTTPException(409, "目标笔记已被移动或删除，请重新选择文件名。")
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(target.name + "." + uid() + ".tmp")
        try:
            if exists:
                temp.write_bytes(data)
                os.replace(temp, target)
            else:
                with target.open("xb") as output:
                    output.write(data)
        except FileExistsError as exc:
            raise HTTPException(409, "目标文件刚被其他程序创建，请选择新文件名。") from exc
        except OSError as exc:
            raise HTTPException(422, "无法写入笔记，请检查文件名、目录权限及磁盘空间。") from exc
        finally:
            temp.unlink(missing_ok=True)
    return {"path": target.relative_to(root).as_posix(), "sha256": hashlib.sha256(data).hexdigest(), "uri": "obsidian://open?path=" + quote(str(target)), "bytes": len(data), "revision": project["revision"]}
