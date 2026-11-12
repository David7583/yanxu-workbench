"""Durable background AI jobs. Client navigation never owns a running job."""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
import json
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from .core import conn, get_project, json_load, now, uid

router = APIRouter(prefix="/api")
_workers: dict[str, asyncio.Task] = {}
_current_job: ContextVar[str | None] = ContextVar("workbench_job", default=None)
_slots = asyncio.Semaphore(2)


def init_schema():
    with conn() as db:
        db.executescript('''CREATE TABLE IF NOT EXISTS work_tasks (
          id TEXT PRIMARY KEY,kind TEXT NOT NULL,project_id TEXT NOT NULL REFERENCES projects(id),
          manuscript_id TEXT,status TEXT NOT NULL,progress INTEGER NOT NULL DEFAULT 0,
          phase TEXT NOT NULL DEFAULT '',payload TEXT NOT NULL,result TEXT,error TEXT NOT NULL DEFAULT '',
          reviewed TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
          CREATE INDEX IF NOT EXISTS work_tasks_project ON work_tasks(project_id,created_at);''')
        db.execute("UPDATE work_tasks SET status='interrupted',phase='服务重启，任务已中断',error='服务已重启。任务不会自动重新调用 AI；已有稿件与完成结果仍然保留。',updated_at=? WHERE status IN ('queued','running')", (now(),))


def task_row(row, full=True):
    value = dict(row)
    value["payload"] = json_load(value["payload"], {})
    if full:
        value["result"] = json_load(value["result"], None)
    else:
        value.pop("result", None)
        value["payload"] = {key: value["payload"][key] for key in ("target", "session_id", "provider_id", "model", "language") if key in value["payload"]}
    return value


def get_task(identifier, full=True):
    with conn() as db:
        row = db.execute("SELECT * FROM work_tasks WHERE id=?", (identifier,)).fetchone()
    if not row:
        raise HTTPException(404, "后台任务不存在。")
    get_project(row["project_id"])
    return task_row(row, full)


def report_progress(progress: int, phase: str):
    identifier = _current_job.get()
    if identifier:
        with conn() as db:
            db.execute("UPDATE work_tasks SET progress=?,phase=?,updated_at=? WHERE id=? AND status='running'", (max(0, min(99, progress)), phase, now(), identifier))


async def _run(identifier):
    token = _current_job.set(identifier)
    try:
        async with _slots:
            task = get_task(identifier)
            if task["status"] != "queued":
                return
            with conn() as db:
                db.execute("UPDATE work_tasks SET status='running',progress=3,phase='正在准备研究材料',updated_at=? WHERE id=?", (now(), identifier))
            if task["kind"] == "research_generate":
                from .research_routes import generate, GenerateInput
                report_progress(12, "正在生成研究整理与稿件；可以离开此页面")
                result = await generate(task["project_id"], GenerateInput(**task["payload"]))
            else:
                from .manuscript_routes import diagnose_manuscript, create_manuscript_proposal, ReviewInput, ProposalInput
                if task["kind"] == "manuscript_diagnose":
                    result = await diagnose_manuscript(task["manuscript_id"], ReviewInput(**task["payload"]))
                else:
                    result = await create_manuscript_proposal(task["manuscript_id"], ProposalInput(**task["payload"]))
            with conn() as db:
                db.execute("UPDATE work_tasks SET status='completed',progress=100,phase='结果已保存，等待查看',result=?,updated_at=? WHERE id=? AND status='running'", (json.dumps(result, ensure_ascii=False), now(), identifier))
    except asyncio.CancelledError:
        with conn() as db:
            db.execute("UPDATE work_tasks SET status='interrupted',phase='执行服务已中断，未自动重试',updated_at=? WHERE id=? AND status IN ('queued','running')", (now(), identifier))
    except Exception as exc:
        # Provider exceptions may contain URLs or credentials; never echo raw bodies.
        message = exc.detail if isinstance(exc, HTTPException) else "任务未完成。请检查服务配置或连接后手动重试；当前稿件未被自动覆盖。"
        if not isinstance(message, str):
            message = json.dumps(message, ensure_ascii=False)
        with conn() as db:
            db.execute("UPDATE work_tasks SET status='failed',phase='任务未完成',error=?,updated_at=? WHERE id=? AND status IN ('queued','running')", (message, now(), identifier))
    finally:
        _current_job.reset(token)
        _workers.pop(identifier, None)


class TaskCreate(BaseModel):
    kind: Literal["manuscript_diagnose", "manuscript_proposal", "research_generate"]
    project_id: str
    manuscript_id: str | None = None
    payload: dict = Field(default_factory=dict)


@router.post("/tasks", status_code=202)
async def create_task(body: TaskCreate):
    from . import providers
    from .manuscript_routes import get_manuscript, ReviewInput, ProposalInput, source_context
    from .research_routes import GenerateInput
    project = get_project(body.project_id)
    model = GenerateInput if body.kind == "research_generate" else ReviewInput if body.kind == "manuscript_diagnose" else ProposalInput
    try:
        validated = model(**body.payload)
    except ValueError as exc:
        raise HTTPException(422, "任务参数无效，请检查选择的范围和生成要求。") from exc
    if body.kind != "research_generate":
        source = get_manuscript(body.manuscript_id or "")
        if source["project_id"] != body.project_id:
            raise HTTPException(422, "原稿不属于当前项目。")
        source_context(source, validated)
    payload = validated.model_dump(exclude_none=True)
    config = providers.get_profile(payload["provider_id"], payload.get("model")) if payload.get("provider_id") else providers.settings()
    if config.get("provider_id"):
        payload["provider_id"] = config["provider_id"]
    if not payload.get("model") and config.get("model"):
        payload["model"] = config["model"]
    if body.kind in {"research_generate", "manuscript_proposal"}:
        payload.setdefault("expected_revision", project["revision"])
    identifier, stamp = uid(), now()
    with conn() as db:
        db.execute("BEGIN IMMEDIATE")
        current = db.execute("SELECT deleted_at FROM projects WHERE id=?", (body.project_id,)).fetchone()
        if not current or current["deleted_at"]:
            raise HTTPException(409, "项目已移入回收站，请先恢复。")
        old = db.execute("SELECT id FROM work_tasks WHERE project_id=? AND kind=? AND coalesce(manuscript_id,'')=? AND status IN ('queued','running')", (body.project_id, body.kind, body.manuscript_id or "")).fetchone()
        if old:
            raise HTTPException(409, {"message": "已有相同类型任务正在执行，可以从任务中心查看。", "task_id": old["id"]})
        db.execute("INSERT INTO work_tasks(id,kind,project_id,manuscript_id,status,phase,payload,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)", (identifier, body.kind, body.project_id, body.manuscript_id, "queued", "已排队，可以离开当前页面", json.dumps(payload, ensure_ascii=False), stamp, stamp))
    _workers[identifier] = asyncio.create_task(_run(identifier))
    return get_task(identifier)


@router.get("/tasks")
def list_tasks(project_id: str = "", status: str = "", limit: int = Query(50, ge=1, le=200)):
    clauses, values = ["coalesce(p.deleted_at,'')=''"], []
    if project_id:
        get_project(project_id)
        clauses.append("t.project_id=?")
        values.append(project_id)
    if status:
        clauses.append("t.status=?")
        values.append(status)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with conn() as db:
        rows = db.execute("SELECT t.* FROM work_tasks t JOIN projects p ON p.id=t.project_id" + where + " ORDER BY CASE WHEN t.status IN ('queued','running') THEN 0 ELSE 1 END,t.created_at DESC LIMIT ?", (*values, limit)).fetchall()
    return {"items": [task_row(row, False) for row in rows]}


@router.get("/tasks/{identifier}")
def read_task(identifier: str):
    return get_task(identifier)


@router.post("/tasks/{identifier}/cancel")
async def cancel_task(identifier: str):
    task = get_task(identifier)
    if task["status"] not in {"queued", "running"}:
        return task
    with conn() as db:
        db.execute("UPDATE work_tasks SET status='cancelled',phase='已请求停止；已发生的调用用量不会撤回',updated_at=? WHERE id=?", (now(), identifier))
    worker = _workers.get(identifier)
    if worker:
        worker.cancel()
    return get_task(identifier)


class Acknowledge(BaseModel):
    disposition: Literal["applied", "dismissed"]


@router.post("/tasks/{identifier}/acknowledge")
def acknowledge_task(identifier: str, body: Acknowledge):
    task = get_task(identifier)
    if task["status"] != "completed":
        raise HTTPException(409, "任务尚未完成。")
    with conn() as db:
        db.execute("UPDATE work_tasks SET reviewed=?,updated_at=? WHERE id=?", (body.disposition, now(), identifier))
    return get_task(identifier)
