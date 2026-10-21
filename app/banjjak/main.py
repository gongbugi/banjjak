from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from prometheus_client import REGISTRY, make_asgi_app
from prometheus_client.core import GaugeMetricFamily
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


FRESH_FOR = timedelta(hours=2)   # 수집은 매시간. 한 번 빠져도 버티고, 두 번 연달아 빠지면 넘는다


def _now():
    return datetime.now(timezone.utc)


def _data_age():
    """가장 최근 기록이 얼마나 묵었나. DB를 못 읽거나 기록이 없으면 None."""
    try:
        with db.engine().connect() as conn:
            latest = latest_capture(conn)
    except Exception:
        return None
    return None if latest is None else _now() - latest


class _DataAge:
    # 긁을 때마다 DB에서 잰다. 못 재면 지표를 아예 내지 않는다 (옛 값이나 0을 내면 신선해 보인다)
    NAME, HELP = "banjjak_data_age_seconds", "now minus the latest star snapshot"

    def describe(self):
        yield GaugeMetricFamily(self.NAME, self.HELP)

    def collect(self):
        age = _data_age()
        if age is not None:
            yield GaugeMetricFamily(self.NAME, self.HELP, value=age.total_seconds())


REGISTRY.register(_DataAge())


@app.get("/freshness")
def freshness():
    # VM 밖(Better Stack)에서 보는 신호. 수집이 0건으로 "성공"해도 묵으면 여기서 잡힌다
    age = _data_age()
    if age is None:
        return JSONResponse({"status": "unknown"}, status_code=503)
    body = {"status": "fresh" if age <= FRESH_FOR else "stale", "age_seconds": int(age.total_seconds())}
    return body if age <= FRESH_FOR else JSONResponse(body, status_code=503)


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
