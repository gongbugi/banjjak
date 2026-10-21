from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from prometheus_client import make_asgi_app
from sqlalchemy import text

from banjjak import db
from banjjak.trending import latest_capture, trending

app = FastAPI(title="banjjak")
app.mount("/metrics", make_asgi_app())
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    # liveness: 프로세스 생존만. DB를 보지 않는다 (DB 장애 때 전 파드 재시작 루프 방지)
    return {"status": "ok"}


@app.get("/readyz")
def readyz():
    try:
        with db.engine().connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:
        return JSONResponse({"status": "db unavailable"}, status_code=503)
    return {"status": "ok"}


def _snapshot():
    with db.engine().connect() as conn:
        updated = latest_capture(conn)
        return (updated.isoformat() if updated else None), trending(conn)


@app.get("/api/trending")
def api_trending():
    updated_at, items = _snapshot()
    return {"updated_at": updated_at, "items": items}


@app.get("/")
def index(request: Request):
    updated_at, items = _snapshot()
    return templates.TemplateResponse(request, "index.html", {"updated_at": updated_at, "items": items})
