"""Small, content-free snapshots for the global background task centre."""
from fastapi import APIRouter
from .core import conn
from .core_routes import import_record

router = APIRouter(prefix='/api')


@router.get('/work-queue')
def work_queue():
    entries = []
    with conn() as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for r in db.execute("SELECT * FROM imports ORDER BY (status IN ('queued','processing','running','cancelling')) DESC,updated_at DESC LIMIT 60"):
            record = import_record(r)
            v = {k: record.get(k) for k in ('id','status','progress','error','created_at','updated_at','can_resume','resume_reason')}
            entries.append({**v, 'kind': 'import', 'title': r['filename'], 'phase': f"已解析 {r['total']} · 新增 {r['imported']} · 重复 {r['duplicates']}"})
        if 'session_runs' in tables:
            for r in db.execute("SELECT r.*,s.title,s.project_id FROM session_runs r JOIN research_sessions s ON s.id=r.session_id JOIN projects p ON p.id=s.project_id WHERE p.deleted_at IS NULL ORDER BY (r.status IN ('queued','running')) DESC,r.updated_at DESC LIMIT 60"):
                v = dict(r)
                entries.append({**v, 'kind': 'discussion', 'progress': round(100*v['completed']/max(1,v['total'])), 'phase': f"已完成 {v['completed']} / {v['total']} 次回应"})
    # Task results and prompts are deliberately excluded from this global feed.
    return {'items': sorted(entries, key=lambda v: v['updated_at'], reverse=True)}
