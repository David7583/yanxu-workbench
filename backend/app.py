from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .core import init_db
from .core_routes import router as core_router
from .research_routes import router as research_router
from .export_routes import router as export_router
from .preview_routes import router as preview_router
from . import provider_routes, group_routes, archive_routes, setup_routes, skills_routes, manuscript_routes, integration_routes, task_routes, activity_routes


@asynccontextmanager
async def lifespan(app):
    init_db()
    provider_routes.init_schema()
    skills_routes.init_schema()
    group_routes.init_schema()
    archive_routes.init_schema()
    setup_routes.init_schema()
    manuscript_routes.init_schema()
    integration_routes.init_schema()
    task_routes.init_schema()
    yield


app = FastAPI(title="研序 · 本地研究工作台", lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])


@app.middleware("http")
async def local_boundary(request: Request, call_next):
    origin = request.headers.get("origin")
    if request.url.path.startswith("/api") and origin:
        parsed = urlparse(origin)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"} or parsed.port not in {8765, 5173}:
            return JSONResponse({"detail": "此本地服务不接受其他网站发起的请求。"}, status_code=403)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    if request.url.path.startswith("/api"):
        response.headers["Cache-Control"] = "no-store"
    return response


app.include_router(core_router)
app.include_router(research_router)
app.include_router(export_router)
app.include_router(preview_router)
for module in (provider_routes, group_routes, archive_routes, setup_routes, skills_routes, manuscript_routes, integration_routes, task_routes, activity_routes):
    app.include_router(module.router)


@app.get("/api/health")
def health():
    return {"status": "ok", "name": "研序", "version": "0.3.0", "storage": "local"}


DIST = Path(__file__).resolve().parents[1] / "frontend" / "dist"
if (DIST / "assets").exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")


@app.get("/{path:path}")
def frontend(path: str):
    if path.startswith("api/"):
        return JSONResponse({"detail": "接口不存在。"}, status_code=404)
    candidate = (DIST / path).resolve()
    if candidate.is_relative_to(DIST.resolve()) and candidate.is_file():
        return FileResponse(candidate)
    if (DIST / "index.html").exists():
        return FileResponse(DIST / "index.html")
    return JSONResponse({"detail": "请先执行 npm run build，或使用前端开发服务器。"}, status_code=503)
