"""Projects, source archives, search and background import API."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import threading

from fastapi import APIRouter, Body, File, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field

from . import core, importer
from .core import conn, get_project, json_load, now, search_messages, uid, update_project
from .importer import CANCELLED, start_import

router = APIRouter(prefix="/api")


class ProjectCreate(BaseModel):
    title: str = Field(default="新的研究", min_length=1, max_length=240)
    description: str = Field(default="", max_length=20000)
    discipline: str = Field(default="通用研究", max_length=100)


class ProjectPatch(BaseModel):
    expected_revision: int = Field(ge=0)
    title: str | None = Field(default=None, min_length=1, max_length=240)
    description: str | None = Field(default=None, max_length=20000)
    discipline: str | None = Field(default=None, max_length=100)
    notes: str | None = Field(default=None, max_length=4_000_000)
    draft_zh: str | None = Field(default=None, max_length=4_000_000)
    draft_en: str | None = Field(default=None, max_length=4_000_000)


@router.get("/projects")
def projects(deleted: bool = False):
    with conn() as db:
        rows = db.execute("SELECT * FROM projects WHERE deleted_at IS "+("NOT NULL" if deleted else "NULL")+" ORDER BY updated_at DESC").fetchall()
    return {"items": [dict(row) for row in rows]}


@router.post("/projects", status_code=201)
def create_project(body: ProjectCreate):
    title = body.title.strip()
    if not title:
        raise HTTPException(422, "请输入项目名称")
    project_id, created = uid(), now()
    with conn() as db:
        db.execute("INSERT INTO projects(id,title,description,discipline,created_at,updated_at) VALUES (?,?,?,?,?,?)", (project_id, title, body.description, body.discipline, created, created))
    return get_project(project_id)


@router.get("/projects/{project_id}")
def project(project_id: str):
    return get_project(project_id)


@router.patch("/projects/{project_id}")
def patch_project(project_id: str, body: ProjectPatch):
    return update_project(project_id, body.model_dump(exclude_none=True))


@router.get("/projects/{project_id}/chats")
def project_chats(project_id: str, limit: int = Query(200, ge=1, le=1000)):
    get_project(project_id)
    with conn() as db:
        rows = db.execute("SELECT * FROM (SELECT * FROM chats WHERE project_id=? ORDER BY created_at DESC,rowid DESC LIMIT ?) ORDER BY created_at ASC", (project_id, limit)).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["sources"] = json_load(item["sources"], [])
        result.append(item)
    return {"items": result}


@router.get("/projects/{project_id}/revisions")
def project_revisions(project_id: str, limit: int = Query(50, ge=1, le=200)):
    get_project(project_id)
    with conn() as db:
        rows = db.execute("SELECT * FROM revisions WHERE project_id=? ORDER BY created_at DESC LIMIT ?", (project_id, limit)).fetchall()
    return {"items": [{**dict(row), "content": json_load(row["content"], row["content"])} for row in rows]}


@router.post("/projects/{project_id}/restore/{revision_id}")
def restore_revision(project_id: str, revision_id: str, body: dict = Body(default={})):
    current = get_project(project_id)
    with conn() as db:
        row = db.execute("SELECT * FROM revisions WHERE id=? AND project_id=?", (revision_id, project_id)).fetchone()
    if not row:
        raise HTTPException(404, "该项目中不存在此版本")
    content = json_load(row["content"], None)
    if not isinstance(content, dict):
        raise HTTPException(422, "这个历史版本不是完整稿件快照")
    changes = {field: content[field] for field in core.PROJECT_FIELDS if field in content}
    changes["expected_revision"] = body.get("expected_revision", current["revision"])
    return update_project(project_id, changes)


@router.get("/sources")
def sources(q: str = "", limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    with conn() as db:
        if q.strip():
            escaped = q.strip()[:500].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            where = "WHERE title LIKE ? ESCAPE '\\' OR provider LIKE ? ESCAPE '\\'"
            params = (f"%{escaped}%", f"%{escaped}%")
        else:
            where, params = "", ()
        total = db.execute(f"SELECT count(*) FROM sources {where}", params).fetchone()[0]
        rows = db.execute(f"SELECT * FROM sources {where} ORDER BY created_at DESC LIMIT ? OFFSET ?", (*params, limit, offset)).fetchall()
    return {"items": [{**dict(row), "metadata": json_load(row["metadata"], {})} for row in rows], "total": total}


@router.get("/sources/{source_id}")
def source(source_id: str, limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)):
    with conn() as db:
        row = db.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "资料不存在")
        messages = db.execute("SELECT * FROM source_messages WHERE source_id=? ORDER BY ordinal,id LIMIT ? OFFSET ?", (source_id, limit, offset)).fetchall()
    return {"source": {**dict(row), "metadata": json_load(row["metadata"], {})}, "messages": [{**dict(message), "metadata": json_load(message["metadata"], {})} for message in messages], "total": row["message_count"]}


@router.get("/search")
def search(q: str = "", limit: int = Query(20, ge=1, le=100)):
    return {"items": search_messages(q, limit)}


@router.get("/stats")
def statistics():
    with conn() as db:
        result = {key: db.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0] for key, table in (("projects", "projects"), ("sources", "sources"), ("messages", "source_messages"), ("references", "references"))}
        result["projects"] = db.execute("SELECT COUNT(*) FROM projects WHERE deleted_at IS NULL").fetchone()[0]
    return result


@router.post("/import", status_code=202)
async def upload_import(file: UploadFile = File(...)):
    original_name = Path((file.filename or "upload").replace("\\", "/")).name
    suffix = Path(original_name).suffix.lower()
    if suffix not in (".json", ".html", ".htm", ".md", ".markdown", ".txt", ".zip"):
        raise HTTPException(415, "请选择 JSON、ZIP、HTML、Markdown 或 TXT 文件")
    job_id = uid()
    directory = core.DATA_DIR / "imports"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (job_id + suffix)
    hasher = hashlib.sha256()
    size = 0
    try:
        with path.open("xb") as target:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > 1024 * 1024 * 1024:
                    raise HTTPException(413, "单文件上限为 1 GB，请拆分导出")
                target.write(chunk)
                hasher.update(chunk)
        if size == 0:
            raise HTTPException(422, "不能导入空文件")
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    finally:
        await file.close()
    file_hash = hasher.hexdigest()
    stamp = now()
    with conn() as db:
        db.execute("BEGIN IMMEDIATE")
        active=db.execute("SELECT id,status FROM imports WHERE file_hash=? AND status IN ('queued','processing','cancelling') LIMIT 1",(file_hash,)).fetchone()
        if active:
            path.unlink()
            return {"job_id":active["id"],"status":active["status"],"duplicate":True,"already_running":True}
        previous = db.execute("SELECT * FROM imports WHERE file_hash=? AND status='done' ORDER BY created_at DESC LIMIT 1", (file_hash,)).fetchone()
        status = "done" if previous else "queued"
        duplicates = previous["total"] if previous else 0
        snapshot=json.dumps({"bytes":size,"mtime_ns":path.stat().st_mtime_ns})
        db.execute("INSERT INTO imports(id,filename,status,progress,total,imported,duplicates,error,created_at,updated_at,file_hash,bytes_total,source_path,scan_snapshot) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (job_id, original_name, status, 100 if previous else 0, duplicates, 0, duplicates, "", stamp, stamp, file_hash, size,str(path),snapshot))
    if previous:
        # This newly received file is byte-identical to an existing retained raw
        # import, so the redundant copy is safe to discard.
        path.unlink()
    else:
        start_import(job_id, path, original_name)
    return {"job_id": job_id, "status": status, "duplicate": bool(previous)}


def import_record(row):
    value = dict(row)
    value["warnings"] = json_load(value["warnings"], [])
    value.pop("file_hash", None)
    from .archive_routes import resume_capability
    value["can_resume"],value["resume_reason"]=resume_capability(row)
    value["can_pause"]=value["status"] in ("queued","processing")
    return value


@router.get("/imports")
def imports():
    with conn() as db:
        rows = db.execute("SELECT * FROM imports ORDER BY created_at DESC LIMIT 100").fetchall()
    return {"items": [import_record(row) for row in rows]}


@router.get("/imports/{job_id}")
def import_status(job_id: str):
    with conn() as db:
        row = db.execute("SELECT * FROM imports WHERE id=?", (job_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "导入任务不存在")
    return import_record(row)


@router.post("/imports/{job_id}/cancel")
def cancel_import(job_id: str):
    with importer.JOB_LOCK:
        with conn() as db:
            db.execute("BEGIN IMMEDIATE")
            row=db.execute("SELECT * FROM imports WHERE id=?",(job_id,)).fetchone()
            if not row:raise HTTPException(404,"导入任务不存在")
            if row["status"] in ("queued","processing"):
                CANCELLED.setdefault(job_id,threading.Event()).set()
                status="cancelling" if job_id in importer.WORKERS else "cancelled"
                db.execute("UPDATE imports SET status=?,updated_at=? WHERE id=?",(status,now(),job_id))
    return import_status(job_id)


@router.get("/sources/{source_id}/messages/{message_id}/versions")
def message_versions(source_id: str, message_id: str):
    with conn() as db:
        if not db.execute("SELECT id FROM source_messages WHERE id=? AND source_id=?",(message_id,source_id)).fetchone():
            raise HTTPException(404,"消息不存在")
        return {"items":[{**dict(r),"metadata":json_load(r["metadata"],{})} for r in db.execute("SELECT * FROM source_message_versions WHERE message_id=? ORDER BY imported_at DESC",(message_id,))]}

@router.delete("/projects/{project_id}")
def delete_project(project_id:str):
    with conn() as db:
        db.execute("BEGIN IMMEDIATE")
        row=db.execute("SELECT id,deleted_at FROM projects WHERE id=?",(project_id,)).fetchone()
        if not row:raise HTTPException(404,"项目不存在")
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "session_runs" in tables and db.execute("SELECT 1 FROM session_runs r JOIN research_sessions s ON s.id=r.session_id WHERE s.project_id=? AND r.status IN ('queued','running') LIMIT 1",(project_id,)).fetchone():
            raise HTTPException(409,"项目仍有讨论正在运行，请先停止讨论再移入回收站")
        if "work_tasks" in tables and db.execute("SELECT 1 FROM work_tasks WHERE project_id=? AND status IN ('queued','running') LIMIT 1",(project_id,)).fetchone():
            raise HTTPException(409,"项目仍有后台任务正在运行，请先停止任务再移入回收站")
        deleted_at=row['deleted_at'] or now()
        if not row['deleted_at']:db.execute("UPDATE projects SET deleted_at=?,updated_at=?,revision=revision+1 WHERE id=?",(deleted_at,deleted_at,project_id))
    return {"id":project_id,"deleted_at":deleted_at,"message":"项目已移入回收站，原始资料和版本记录保留"}

@router.post("/projects/{project_id}/restore")
def restore_deleted_project(project_id:str):
    with conn() as db:
        cursor=db.execute("UPDATE projects SET deleted_at=NULL,updated_at=?,revision=revision+1 WHERE id=? AND deleted_at IS NOT NULL",(now(),project_id))
        if not cursor.rowcount and not db.execute("SELECT id FROM projects WHERE id=?",(project_id,)).fetchone():raise HTTPException(404,"项目不存在")
    return get_project(project_id)

@router.get("/dashboard")
def dashboard():
    from datetime import datetime,timedelta,timezone
    today=datetime.now(timezone.utc).date();days=[(today-timedelta(days=n)).isoformat() for n in range(6,-1,-1)];start=days[0]
    with conn() as db:
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        scalar=lambda sql,args=():db.execute(sql,args).fetchone()[0]
        counts={"projects":scalar("SELECT COUNT(*) FROM projects WHERE deleted_at IS NULL"),"deleted_projects":scalar("SELECT COUNT(*) FROM projects WHERE deleted_at IS NOT NULL"),"sources":scalar("SELECT COUNT(*) FROM sources"),"messages":scalar("SELECT COUNT(*) FROM source_messages"),"references":scalar('SELECT COUNT(*) FROM "references" r JOIN projects p ON p.id=r.project_id WHERE p.deleted_at IS NULL'),"skills":scalar('SELECT COUNT(*) FROM skills') if 'skills' in tables else 0,"imports_active":scalar("SELECT COUNT(*) FROM imports WHERE status IN ('queued','processing','cancelling')"),"sessions":scalar('SELECT COUNT(*) FROM research_sessions s JOIN projects p ON p.id=s.project_id WHERE p.deleted_at IS NULL') if 'research_sessions' in tables else 0}
        distribution=[dict(r) for r in db.execute("SELECT provider,COUNT(*) AS sources,COALESCE(SUM(message_count),0) AS messages FROM sources GROUP BY provider ORDER BY messages DESC,provider")]
        activity={day:{"date":day,"discussions":0,"revisions":0,"imports":0,"total":0} for day in days}
        queries=[('revisions',"SELECT date(r.created_at) AS day,COUNT(*) AS n FROM revisions r JOIN projects p ON p.id=r.project_id WHERE p.deleted_at IS NULL AND date(r.created_at)>=? GROUP BY day"),('imports',"SELECT date(updated_at) AS day,COUNT(*) AS n FROM imports WHERE status='done' AND date(updated_at)>=? GROUP BY day")]
        if 'session_messages' in tables:
            queries.append(('discussions',"SELECT date(m.created_at) AS day,COUNT(*) AS n FROM session_messages m JOIN research_sessions s ON s.id=m.session_id JOIN projects p ON p.id=s.project_id WHERE p.deleted_at IS NULL AND m.status='completed' AND date(m.created_at)>=? GROUP BY day"))
        legacy_exclude=" AND NOT EXISTS(SELECT 1 FROM session_messages m WHERE m.id='legacy_'||c.id)" if 'session_messages' in tables else ''
        queries.append(('discussions',"SELECT date(c.created_at) AS day,COUNT(*) AS n FROM chats c JOIN projects p ON p.id=c.project_id WHERE p.deleted_at IS NULL AND date(c.created_at)>=?"+legacy_exclude+" GROUP BY day"))
        for kind,query in queries:
            for r in db.execute(query,(start,)):
                if r['day'] in activity:activity[r['day']][kind]+=r['n']
        for day in activity.values():day['total']=day['discussions']+day['revisions']+day['imports']
        sessions_sql="(SELECT COUNT(*) FROM research_sessions s WHERE s.project_id=p.id)" if 'research_sessions' in tables else '0'
        message_sql="(SELECT COUNT(*) FROM session_messages m JOIN research_sessions s ON s.id=m.session_id WHERE s.project_id=p.id AND m.status='completed')" if 'session_messages' in tables else '0'
        legacy_count="(SELECT COUNT(*) FROM chats c WHERE c.project_id=p.id"+legacy_exclude+")"
        project_rows=db.execute(f'''SELECT p.id,p.title,p.updated_at,length(trim(p.notes)) AS notes_chars,length(trim(p.draft_zh)) AS zh_chars,length(trim(p.draft_en)) AS en_chars,(SELECT COUNT(*) FROM "references" r WHERE r.project_id=p.id) AS "references",{sessions_sql} AS sessions,{message_sql}+{legacy_count} AS discussion_messages FROM projects p WHERE p.deleted_at IS NULL ORDER BY p.updated_at DESC LIMIT 100''').fetchall()
        project_progress=[]
        for r in project_rows:
            item=dict(r);stages={"discussion":bool(item['discussion_messages']),"notes":bool(item['notes_chars']),"draft":bool(item['zh_chars'] or item['en_chars']),"references":bool(item['references'])}
            item.update(stages=stages,progress=sum(stages.values())*25);project_progress.append(item)
    return {"counts":counts,"activity":list(activity.values()),"source_distribution":distribution,"projects":project_progress,"timezone":"UTC","progress_description":"讨论、笔记、稿件、文献四类内容的已有情况；不代表论文质量或可发表程度"}
