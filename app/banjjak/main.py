import asyncio
import json
import os
import random
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from prometheus_client import REGISTRY, Histogram
from prometheus_client.core import GaugeMetricFamily
from sqlalchemy import text

from banjjak import db
from banjjak.cards import KST, attach_cards, success_ratio, weekly_tally
from banjjak.probe import USER_AGENT as PROBE_AGENT
from banjjak.trending import latest_capture, trending

# 지표는 이 앱에 없다. banjjak.serve가 9100 포트로 따로 낸다 (공개 입구로 새지 않게)
app = FastAPI(title="banjjak")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
# README 인용은 원문 그대로 저장하고, 화면에서만 마크다운 링크 · 강조 · 코드 표시를 글자로 보인다
templates.env.filters["plain"] = lambda s: _MD_LINK.sub(r"\1", s).replace("**", "").replace("`", "")

# 0.5초 칸: p95 500ms 규칙이 경계에서 어림이 아니라 정확해진다
# source: 운영 VM 합성 요청(probe)과 실제(user)를 나눠 본다. SLI 규칙은 둘을 합친다
# ponytail: User-Agent는 누구나 흉내 낼 수 있다. 지표 라벨일 뿐이라 막지 않는다
REQUEST_SECONDS = Histogram(
    "banjjak_http_request_duration_seconds", "web request latency", ["route", "method", "status", "source"],
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
        source = "probe" if request.headers.get("user-agent") == PROBE_AGENT else "user"
        REQUEST_SECONDS.labels(route.path if route else "unmatched", request.method, str(status), source).observe(
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


async def _fault():
    """장애 주입 스위치 (#10). 화면 요청(/ · /api/trending)에만 건다. 검사 주소는 건드리지 않는다.
    두 곳에서 읽는다: 주입기가 바꾸는 ConfigMap 파일(FAULT_FILE, 운영 AI는 ConfigMap을 못 읽는다)과
    "나쁜 배포"로 웹 배포 설정에 들어간 FAULT 환경 변수(롤백하면 사라진다). 라우팅 뒤에 걸어야 경보 식이 그 주소로 센다."""
    fault = {}
    for raw in (_read(os.environ.get("FAULT_FILE", "/etc/fault/fault.json")), os.environ.get("FAULT")):
        try:
            fault.update(json.loads(raw) if raw else {})
        except ValueError:
            pass   # 깨진 스위치는 꺼진 것으로 본다
    if fault.get("delay"):
        await asyncio.sleep(float(fault["delay"]))
    if random.random() < float(fault.get("error_rate", 0)):
        raise HTTPException(status_code=500)   # 사용자에게는 평범한 서버 오류로 보인다


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def _snapshot():
    with db.engine().connect() as conn:
        updated = latest_capture(conn)
        items = attach_cards(conn, trending(conn))
        return (updated.isoformat() if updated else None), items, weekly_tally(conn, _now())


@app.get("/api/trending", dependencies=[Depends(_fault)])
def api_trending():
    updated_at, items, week = _snapshot()
    return {"updated_at": updated_at, "items": items, "week": week}


@app.get("/", dependencies=[Depends(_fault)])
def index(request: Request):
    updated_at, items, week = _snapshot()
    basis = datetime.fromisoformat(updated_at).astimezone(KST) if updated_at else None
    basis_text = f"{basis.year}년 {basis.month}월 {basis.day}일 {basis:%H:%M} 기준 (한국 시간)" if basis else None
    # 반짝임 크기: 하루 별 증가를 1위 대비 제곱근으로 (작은 차이도 보이게)
    top = max((i["stars_per_day"] for i in items), default=0)
    for i in items:
        i["glow"] = round(0.35 + 0.65 * (i["stars_per_day"] / top) ** 0.5, 2) if top > 0 else 0.35
    return templates.TemplateResponse(request, "index.html", {"basis": basis_text, "items": items, "week": week})
