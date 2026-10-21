from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from banjjak import db, main
from banjjak.collect import collect_once

client = TestClient(main.app)
CAPTURED = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def seeded():
    item = {"id": 7, "full_name": "x/rocket", "html_url": "https://github.com/x/rocket",
            "description": None, "language": None, "stargazers_count": 420,
            "created_at": "2026-10-17T12:00:00Z"}
    with db.engine().begin() as conn:
        collect_once(lambda q, p: {"items": [item] if p == 1 else []}, conn, CAPTURED)


def _at(monkeypatch, elapsed):
    monkeypatch.setattr(main, "_now", lambda: CAPTURED + elapsed)


def test_fresh_up_to_two_hours(seeded, monkeypatch):
    _at(monkeypatch, timedelta(hours=2))
    r = client.get("/freshness")
    assert r.status_code == 200
    assert r.json()["age_seconds"] == 7200


def test_stale_after_two_hours(seeded, monkeypatch):
    _at(monkeypatch, timedelta(hours=2, seconds=1))
    assert client.get("/freshness").status_code == 503


def test_no_data_is_not_fresh():
    assert client.get("/freshness").status_code == 503


def test_data_age_metric(seeded, monkeypatch):
    _at(monkeypatch, timedelta(minutes=30))
    assert "banjjak_data_age_seconds 1800.0" in client.get("/metrics/").text


class _DownEngine:
    def connect(self):
        raise OperationalError("connect", None, Exception("db down"))


def test_db_down_means_503_and_no_metric(seeded, monkeypatch):
    monkeypatch.setattr(db, "engine", lambda: _DownEngine())
    assert client.get("/freshness").status_code == 503
    # 옛 값이나 0을 내면 신선해 보인다 → 지표 자체를 내지 않는다
    assert "banjjak_data_age_seconds " not in client.get("/metrics/").text
