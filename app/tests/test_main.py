from fastapi.testclient import TestClient

from banjjak.main import app

client = TestClient(app)


def test_healthz():
    assert client.get("/healthz").json() == {"status": "ok"}


def test_metrics_exposed():
    r = client.get("/metrics/")
    assert r.status_code == 200
    assert "python_info" in r.text
