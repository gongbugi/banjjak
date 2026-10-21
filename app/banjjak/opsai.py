"""운영 AI 자리. 지금은 Alertmanager가 보낸 알림을 한 줄씩 기록만 한다 (#7부터 분류 · 진단을 채운다).

Slack 알림은 Alertmanager가 따로 보낸다 → 이 프로세스가 죽어도 사람에게 가는 알림은 끊기지 않는다.
실행: uvicorn banjjak.opsai:app --port 8080
"""
import asyncio
import json
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from banjjak.heartbeat import ping

BEAT_EVERY = 60   # 초. Better Stack에는 더 긴 간격으로 걸어 한두 번 빠져도 버틴다


async def _beat():
    # ponytail: 지금은 "프로세스가 살아 있다"만 알린다. #7에서 알림 처리 고리가 생기면 그 고리에서 보낸다
    while True:
        await asyncio.to_thread(ping, os.environ.get("HEARTBEAT_URL"))
        await asyncio.sleep(BEAT_EVERY)


@asynccontextmanager
async def lifespan(app):
    task = asyncio.create_task(_beat()) if os.environ.get("HEARTBEAT_URL") else None
    yield
    if task:
        task.cancel()


app = FastAPI(title="banjjak-ops-ai", lifespan=lifespan)


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
