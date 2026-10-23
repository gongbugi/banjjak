import time
from datetime import datetime, timezone

from fastapi.testclient import TestClient
from prometheus_client import REGISTRY, generate_latest

from banjjak import db
from banjjak.collect import collect_once
from banjjak.main import app
from banjjak.probe import USER_AGENT as PROBE_AGENT

client = TestClient(app)


def test_healthz():
    assert client.get("/healthz").json() == {"status": "ok"}


def test_readyz_checks_database():
    assert client.get("/readyz").status_code == 200


def test_metrics_not_on_public_app():
    # 지표는 따로 띄운 포트(9100)로만 낸다. 공개 입구(Ingress)는 앱 포트만 알아서 /metrics가 밖으로 안 샌다
    assert client.get("/metrics/").status_code == 404
    assert "python_info" in generate_latest(REGISTRY).decode()


def _latency_bucket(route, status, le, source="user"):
    return REGISTRY.get_sample_value("banjjak_http_request_duration_seconds_bucket",
                                     {"route": route, "method": "GET", "status": status, "source": source, "le": le})


def test_probe_requests_are_counted_apart_from_users():
    # 운영 VM의 합성 요청과 실제 사용자를 지표에서 나눠 본다 (SLI 규칙은 둘을 합쳐 본다)
    probe0 = _latency_bucket("/api/trending", "200", "+Inf", "probe") or 0
    user0 = _latency_bucket("/api/trending", "200", "+Inf", "user") or 0
    client.get("/api/trending", headers={"User-Agent": PROBE_AGENT})
    client.get("/api/trending", headers={"User-Agent": "Mozilla/5.0"})
    assert _latency_bucket("/api/trending", "200", "+Inf", "probe") == probe0 + 1
    assert _latency_bucket("/api/trending", "200", "+Inf", "user") == user0 + 1


def test_request_latency_has_half_second_bucket():
    before = _latency_bucket("/api/trending", "200", "0.5") or 0
    client.get("/api/trending")
    assert _latency_bucket("/api/trending", "200", "0.5") == before + 1   # p95 500ms 규칙이 경계에서 정확하려면 0.5초 칸이 있어야 한다


def test_unknown_paths_share_one_label():
    # 스캐너가 아무 주소나 두드려도 지표 종류가 늘지 않는다
    client.get("/wp-login.php")
    assert _latency_bucket("unmatched", "404", "+Inf") >= 1


def _seed():
    item = {"id": 7, "full_name": "x/rocket", "html_url": "https://github.com/x/rocket",
            "description": "goes up", "language": "Rust", "stargazers_count": 420,
            "created_at": "2026-10-17T12:00:00Z"}
    with db.engine().begin() as conn:
        collect_once(lambda q, p: {"items": [item] if p == 1 else []}, conn,
                     datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc))


def test_api_trending_lists_ranked_repos():
    _seed()
    body = client.get("/api/trending").json()
    assert body["updated_at"] == "2026-10-21T12:00:00+00:00"
    assert body["items"][0]["full_name"] == "x/rocket"
    assert body["items"][0]["stars_per_day"] == 105.0   # 4일 동안 420개 (추정)


def test_index_page_shows_ranked_repos():
    _seed()
    r = client.get("/")
    assert r.status_code == 200
    assert "x/rocket" in r.text
    assert "최근 30일 안에 생긴 레포" in r.text
    assert "2026년 10월 21일 21:00 기준 (한국 시간)" in r.text   # 수집 시각 12:00 UTC


def test_no_write_routes():
    writes = [r.path for r in app.routes if set(getattr(r, "methods", ())) & {"POST", "PUT", "PATCH", "DELETE"}]
    assert writes == []


# --- 장애 스위치 (#10): 주입기가 켜면 화면 요청(/ · /api/trending)만 느려지거나 5xx. 검사 주소는 그대로 ---

def test_fault_switch_breaks_only_the_user_pages_and_the_sli_sees_it(tmp_path, monkeypatch):
    switch = tmp_path / "fault.json"
    monkeypatch.setenv("FAULT_FILE", str(switch))
    errors0 = _latency_bucket("/api/trending", "500", "+Inf") or 0
    switch.write_text('{"error_rate": 1.0}')
    assert client.get("/api/trending").status_code == 500
    assert client.get("/healthz").status_code == 200   # 검사 주소는 건드리지 않는다
    assert _latency_bucket("/api/trending", "500", "+Inf") == errors0 + 1   # 경보 식이 세는 그 칸
    switch.write_text("{}")
    assert client.get("/api/trending").status_code == 200   # 끄면 바로 정상


def test_fault_switch_delays_the_user_pages(tmp_path, monkeypatch):
    switch = tmp_path / "fault.json"
    monkeypatch.setenv("FAULT_FILE", str(switch))
    switch.write_text('{"delay": 0.3}')
    start = time.perf_counter()
    assert client.get("/api/trending").status_code == 200
    assert time.perf_counter() - start >= 0.3
    start = time.perf_counter()
    client.get("/healthz")
    assert time.perf_counter() - start < 0.3


def test_bad_deploy_fault_comes_from_the_deployment_env(monkeypatch, tmp_path):
    # "나쁜 배포": 주입기가 웹 배포 설정에 FAULT를 넣는다 → 롤백하면 사라진다
    monkeypatch.setenv("FAULT_FILE", str(tmp_path / "none.json"))
    monkeypatch.setenv("FAULT", '{"error_rate": 1.0}')
    assert client.get("/").status_code == 500
