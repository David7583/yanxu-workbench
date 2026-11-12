"""Read-only citation preview generated from a single saved document snapshot."""
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import core
from .export_routes import render_citations

router = APIRouter(prefix="/api", tags=["preview"])


class PreviewRequest(BaseModel):
    language: Literal["zh", "en"] = "zh"
    style: Literal["apa", "ieee", "gbt"] = "apa"


@router.post("/projects/{project_id}/preview")
def preview_project(project_id: str, body: PreviewRequest):
    with core.conn() as db:
        db.execute("BEGIN")
        project = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if project is None:
            raise HTTPException(404, "项目不存在。")
        references = [dict(row) for row in db.execute(
            'SELECT * FROM "references" WHERE project_id=? ORDER BY rowid', (project_id,)
        )]
        draft = project["draft_" + body.language]
        revision = project["revision"]
    if not draft.strip():
        return {"content": "", "revision": revision, "warnings": ["当前语言版本尚无正文，请先编辑或生成稿件。"]}
    rendered, warnings = render_citations(draft, references, body.style, body.language)
    return {"content": rendered, "revision": revision, "warnings": warnings}
