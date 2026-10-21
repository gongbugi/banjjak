from datetime import datetime, timezone

from fastapi.testclient import TestClient

from banjjak import db
from banjjak.collect import collect_once
from banjjak.main import app

client = TestClient(app)


def test_healthz():
    assert client.get("/healthz").json() == {"status": "ok"}


def test_readyz_checks_database():
    assert client.get("/readyz").status_code == 200


def test_metrics_exposed():
    r = client.get("/metrics/")
    assert r.status_code == 200
    assert "python_info" in r.text


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


def test_no_write_routes():
    writes = [r.path for r in app.routes if set(getattr(r, "methods", ())) & {"POST", "PUT", "PATCH", "DELETE"}]
    assert writes == []
