"""운영 AI 자리. 지금은 Alertmanager가 보낸 알림을 한 줄씩 기록만 한다 (#7부터 분류 · 진단을 채운다).

Slack 알림은 Alertmanager가 따로 보낸다 → 이 프로세스가 죽어도 사람에게 가는 알림은 끊기지 않는다.
실행: uvicorn banjjak.opsai:app --port 8080
"""
import json

from fastapi import FastAPI, Request

app = FastAPI(title="banjjak-ops-ai")


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/alerts")
async def alerts(request: Request):
    body = await request.json()
    received = body.get("alerts", [])
    for a in received:
        # ponytail: 기록은 일단 표준 출력 한 줄(JSON). 승인 대기처럼 이어받아야 할 상태가 생기면 DB로 (#9)
        print(json.dumps({
            "status": a.get("status"), "alertname": a.get("labels", {}).get("alertname"),
            "summary": a.get("annotations", {}).get("summary"),
            "startsAt": a.get("startsAt"), "fingerprint": a.get("fingerprint"),
        }, ensure_ascii=False), flush=True)
    return {"received": len(received)}
