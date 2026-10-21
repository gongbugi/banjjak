import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from prometheus_client import REGISTRY, Histogram
from prometheus_client.core import GaugeMetricFamily
from sqlalchemy import text

from banjjak import db
from banjjak.cards import KST, attach_cards, success_ratio, weekly_tally
from banjjak.trending import latest_capture, trending

# 지표는 이 앱에 없다. banjjak.serve가 9100 포트로 따로 낸다 (공개 입구로 새지 않게)
app = FastAPI(title="banjjak")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

# 0.5초 칸: p95 500ms 규칙이 경계에서 어림이 아니라 정확해진다
REQUEST_SECONDS = Histogram(
    "banjjak_http_request_duration_seconds", "web request latency", ["route", "method", "status"],
    buckets=(0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10))


@app.middleware("http")
async def _observe(request: Request, call_next):
    start, status = time.perf_counter(), 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        # 실제 주소가 아니라 경로 틀로 센다. 모르는 주소는 한 칸으로 (지표 종류가 끝없이 늘지 않게)
        route = request.scope.get("route")
        REQUEST_SECONDS.labels(route.path if route else "unmatched", request.method, str(status)).observe(
            time.perf_counter() - start)


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


def _db_value(measure):
    """DB에서 값 하나를 잰다. DB를 못 읽으면 None."""
    try:
        with db.engine().connect() as conn:
            return measure(conn)
    except Exception:
        return None


def _data_age():
    """가장 최근 기록이 얼마나 묵었나. DB를 못 읽거나 기록이 없으면 None."""
    latest = _db_value(latest_capture)
    return None if latest is None else _now() - latest


class _DbGauge:
    # 긁을 때마다 DB에서 잰다. 못 재면 지표를 아예 내지 않는다 (옛 값이나 0을 내면 정상처럼 보인다)
    def __init__(self, name, help, measure):
        self.name, self.help, self.measure = name, help, measure

    def describe(self):
        yield GaugeMetricFamily(self.name, self.help)

    def collect(self):
        value = self.measure()
        if value is not None:
            yield GaugeMetricFamily(self.name, self.help, value=value)


REGISTRY.register(_DbGauge("banjjak_data_age_seconds", "now minus the latest star snapshot",
                           lambda: None if (age := _data_age()) is None else age.total_seconds()))
# #4 규칙 AnalysisSuccessLow가 이 이름을 본다 (95% 미만이 15분)
REGISTRY.register(_DbGauge("banjjak_analysis_success_ratio", "share of successful analyses in the last 24h",
                           lambda: _db_value(lambda conn: success_ratio(conn, _now()))))


@app.get("/freshness")
def freshness():
    # VM 밖(Better Stack)에서 보는 신호. 수집이 0건으로 "성공"해도 묵으면 여기서 잡힌다
    age = _data_age()
    if age is None:
        return JSONResponse({"status": "unknown"}, status_code=503)
    fresh = age <= FRESH_FOR
    body = {"status": "fresh" if fresh else "stale", "age_seconds": int(age.total_seconds())}
    return JSONResponse(body, status_code=200 if fresh else 503)


def _snapshot():
    with db.engine().connect() as conn:
        updated = latest_capture(conn)
        items = attach_cards(conn, trending(conn))
        return (updated.isoformat() if updated else None), items, weekly_tally(conn, _now())


@app.get("/api/trending")
def api_trending():
    updated_at, items, week = _snapshot()
    return {"updated_at": updated_at, "items": items, "week": week}


@app.get("/")
def index(request: Request):
    updated_at, items, week = _snapshot()
    basis = datetime.fromisoformat(updated_at).astimezone(KST) if updated_at else None
    basis_text = f"{basis.year}년 {basis.month}월 {basis.day}일 {basis:%H:%M} 기준 (한국 시간)" if basis else None
    # 반짝임 크기: 하루 별 증가를 1위 대비 제곱근으로 (작은 차이도 보이게)
    top = max((i["stars_per_day"] for i in items), default=0)
    for i in items:
        i["glow"] = round(0.35 + 0.65 * (i["stars_per_day"] / top) ** 0.5, 2) if top > 0 else 0.35
    return templates.TemplateResponse(request, "index.html", {"basis": basis_text, "items": items, "week": week})
