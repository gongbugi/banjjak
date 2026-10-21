from datetime import datetime, timedelta, timezone
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


KST = timezone(timedelta(hours=9))   # 한국은 서머타임이 없어 고정 오프셋으로 충분하다 (tzdata 불필요)


@app.get("/")
def index(request: Request):
    updated_at, items = _snapshot()
    basis = datetime.fromisoformat(updated_at).astimezone(KST) if updated_at else None
    basis_text = f"{basis.year}년 {basis.month}월 {basis.day}일 {basis:%H:%M} 기준 (한국 시간)" if basis else None
    return templates.TemplateResponse(request, "index.html", {"basis": basis_text, "items": items})
