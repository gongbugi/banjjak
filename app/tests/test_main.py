from datetime import datetime, timezone

from fastapi.testclient import TestClient
from prometheus_client import REGISTRY, generate_latest

from banjjak import db
from banjjak.collect import collect_once
from banjjak.main import app

client = TestClient(app)


def test_healthz():
    assert client.get("/healthz").json() == {"status": "ok"}


def test_readyz_checks_database():
    assert client.get("/readyz").status_code == 200


def test_metrics_not_on_public_app():
    # 지표는 따로 띄운 포트(9100)로만 낸다. 공개 입구(Ingress)는 앱 포트만 알아서 /metrics가 밖으로 안 샌다
    assert client.get("/metrics/").status_code == 404
    assert "python_info" in generate_latest(REGISTRY).decode()


def _latency_bucket(route, status, le):
    return REGISTRY.get_sample_value("banjjak_http_request_duration_seconds_bucket",
                                     {"route": route, "method": "GET", "status": status, "le": le})


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
