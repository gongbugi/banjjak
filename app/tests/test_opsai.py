import json

from fastapi.testclient import TestClient

from banjjak.opsai import app

client = TestClient(app)

# Alertmanager webhook 본문 (필요한 칸만)
PAYLOAD = {
    "version": "4", "status": "firing", "receiver": "ops-ai", "groupKey": "{}:{alertname=\"DataStale\"}",
    "alerts": [
        {"status": "firing", "labels": {"alertname": "DataStale"},
         "annotations": {"summary": "마지막 수집 기록이 2시간보다 오래됐다"},
         "startsAt": "2026-10-21T12:16:41Z", "fingerprint": "a1"},
        {"status": "resolved", "labels": {"alertname": "WebLatencyP95High"},
         "annotations": {"summary": "웹 p95가 5분째 500ms를 넘는다"},
         "startsAt": "2026-10-21T11:00:00Z", "fingerprint": "b2"},
    ],
}


def test_records_each_alert_as_one_line(capsys):
    r = client.post("/alerts", json=PAYLOAD)
    assert r.status_code == 200
    assert r.json() == {"received": 2}
    lines = [json.loads(l) for l in capsys.readouterr().out.splitlines()]
    assert [(l["alertname"], l["status"]) for l in lines] == [("DataStale", "firing"), ("WebLatencyP95High", "resolved")]
    assert lines[0]["summary"] == "마지막 수집 기록이 2시간보다 오래됐다"


def test_healthz():
    assert client.get("/healthz").json() == {"status": "ok"}
