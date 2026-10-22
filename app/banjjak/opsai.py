"""운영 AI: 울린 알림을 분류하고, 점검표로 진단하고, 조치 표로 조치 · 등급을 정해 기록하고 Slack에 보고한다.

①(#7) 분류 · 진단 · 보고 → ②(#8) 조치 · 등급 · 기다림 · 실행 (actions.py)
알림 자체는 Alertmanager가 Slack에 따로 보낸다 → 이 프로세스가 죽어도 사람에게 가는 알림은 끊기지 않는다.
바깥은 앱 클러스터 API(banjjak 읽기 + 허용 조치 쓰기, 쓰기 권한은 클러스터마다 RBAC로)와 운영 Prometheus뿐이다.
운영 클러스터 API 토큰은 아예 없다.
실행: uvicorn banjjak.opsai:app --port 8080
"""
import asyncio
import json
import os
import ssl
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager, closing
from datetime import datetime, timezone

from fastapi import BackgroundTasks, FastAPI, Request

from banjjak import actions, checklist
from banjjak.heartbeat import ping

BEAT_EVERY = 60   # 초. Better Stack에는 더 긴 간격으로 걸어 한두 번 빠져도 버틴다
TICK_EVERY = 15   # 초. 5분 기다린 T1을 찾는 주기
APP_CLUSTER = "/etc/app-cluster"   # Secret app-cluster: url · token · ca.crt (앱 클러스터 ServiceAccount banjjak/ops-ai)

# 분류는 코드다: 종류 = 알림 규칙 이름, 심각도 = 그 규칙이 지키는 SLI (목표값은 스펙 #1)
# critical: 방문자가 지금 못 쓰거나 운영자가 아무것도 못 본다. warning: 쓸 수는 있는데 목표를 벗어났다
KINDS = {
    "WebErrorRateHigh": ("가용성 99%", "critical"),
    "DataAgeMissing": ("데이터 신선도 2시간", "critical"),   # 지표가 아예 없다 = 눈이 멀었다
    "WebLatencyP95High": ("응답 p95 500ms", "warning"),
    "DataStale": ("데이터 신선도 2시간", "warning"),
    "AnalysisSuccessLow": ("분석 성공률 95%", "warning"),
}


def handle(alert, prom, now, conn, hands):
    """울린 알림 1건: 분류(코드) → 진단(점검표) → 조치 · 등급(표) → 기록 → Slack 보고. 실행은 tick이 5분 뒤에 한다.
    hands.cluster: 이 운영 AI가 지키는 곳 (azure · local-k3d). 둘이 같은 Slack 채널에 보내므로 보고마다 밝힌다."""
    kind = alert.get("labels", {}).get("alertname")
    sli, severity = KINDS.get(kind, (None, "unknown"))
    record = {"event": "diagnosis", "cluster": hands.cluster, "at": now.isoformat(), "alertname": kind,
              "startsAt": alert.get("startsAt"), "fingerprint": alert.get("fingerprint"), "sli": sli,
              "severity": severity, **checklist.diagnose(kind, hands.kube, prom, now)}
    print(json.dumps(record, ensure_ascii=False), flush=True)
    row = actions.plan(conn, record, now)   # 판단 기록도 이 행(diagnosis 칸)에 남는다 → #12 채점이 읽는다
    hands.slack(report(record, row, hands.mode))
    return {**record, **{k: row[k] for k in ("id", "action", "tier", "state")}}


def report(r, row, mode):
    lines = [f"🩺 운영 AI 진단 [{r['cluster']}] · {r['alertname']} ({r['sli'] or 'SLI 모름'} · {r['severity']})",
             f"판단: {r['verdict'] or '판단 못 함 → 사람이 봐 주세요'}", "본 것:"]
    lines += [f"• {e['look']}: {e['saw']}" for e in r["evidence"]]
    lines.append(actions.describe(row, mode))
    return "\n".join(lines)


def _app_cluster():
    """앱 클러스터 주소 · 토큰 · TLS. 파일은 매번 읽는다 → Secret을 바꾸면 재시작 없이 따라간다."""
    def read(name):
        with open(os.path.join(APP_CLUSTER, name)) as f:
            return f.read().strip()
    return read("url"), {"Authorization": "Bearer " + read("token")}, \
        ssl.create_default_context(cafile=os.path.join(APP_CLUSTER, "ca.crt"))


def _kube(path):
    """앱 클러스터 API 읽기 (GET)."""
    url, headers, tls = _app_cluster()
    with urllib.request.urlopen(urllib.request.Request(url + path, headers=headers), timeout=10, context=tls) as r:
        body = r.read().decode()
        return json.loads(body) if r.headers.get_content_type() == "application/json" else body


def _patch(path, body, content_type):
    """앱 클러스터 API 쓰기 (PATCH). 할 수 있는 조치 · 이름은 RBAC가 정한다 (Azure는 #8 2단계 전까지 쓰기 권한 없음)."""
    url, headers, tls = _app_cluster()
    req = urllib.request.Request(url + path, data=json.dumps(body).encode(), method="PATCH",
                                 headers={**headers, "Content-Type": content_type})
    with urllib.request.urlopen(req, timeout=10, context=tls) as r:
        return json.load(r)


def _prom(query):
    url = os.environ["PROM_URL"] + "/api/v1/query?" + urllib.parse.urlencode({"query": query})
    with urllib.request.urlopen(url, timeout=10) as r:
        result = json.load(r)["data"]["result"]
    return float(result[0]["value"][1]) if result else None


def _slack(text):
    url = os.environ.get("SLACK_WEBHOOK_URL")   # Secret slack-webhook. 없으면(로컬 · 시험) 기록만 한다
    if not url:
        return
    req = urllib.request.Request(url, data=json.dumps({"text": text}).encode(), headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10).close()
    except Exception as e:   # 보고가 실패해도 기록은 남았다
        print(f"slack failed: {e}", flush=True)


def _hands():
    # 실행 여부는 클러스터마다 (ConfigMap ops-ai-env). 값이 없으면 안전한 쪽(dry-run)
    return actions.Hands(_kube, _patch, _slack, os.environ.get("ACTIONS", "dry-run"),
                         os.environ.get("CLUSTER_NAME", "unknown"))


def _db():
    return closing(actions.connect(os.environ.get("OPSAI_DB", "/data/opsai.db")))   # 운영 VM 디스크(PVC)


def _handle(alert, now):
    with _db() as conn:
        handle(alert, _prom, now, conn, _hands())


def _tick():
    try:
        with _db() as conn:
            actions.tick(conn, datetime.now(timezone.utc), _hands())
    except Exception as e:   # 한 번 실패해도 다음 주기에 다시 본다 (기다리는 조치는 DB에 그대로 있다)
        print(f"tick failed: {type(e).__name__}: {e}", flush=True)


async def _tick_loop():
    while True:
        await asyncio.to_thread(_tick)
        await asyncio.sleep(TICK_EVERY)


async def _beat():
    # ponytail: "프로세스가 살아 있다"만 알린다 (진단 길이 되는지는 모른다).
    # 늘 울리는 Watchdog을 운영 AI에도 보내고 그 처리 끝에서 보내면 진단 길까지 본다
    while True:
        await asyncio.to_thread(ping, os.environ.get("HEARTBEAT_URL"))
        await asyncio.sleep(BEAT_EVERY)


@asynccontextmanager
async def lifespan(app):
    tasks = [asyncio.create_task(_tick_loop())]
    if os.environ.get("HEARTBEAT_URL"):
        tasks.append(asyncio.create_task(_beat()))
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="banjjak-ops-ai", lifespan=lifespan)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/alerts")
async def alerts(request: Request, background: BackgroundTasks):
    body = await request.json()
    received = body.get("alerts", [])
    for a in received:
        # 받은 것은 울림 · 풀림 모두 한 줄씩 (풀린 시각이 복구 시간 채점의 끝이다)
        print(json.dumps({
            "event": "alert", "status": a.get("status"), "alertname": a.get("labels", {}).get("alertname"),
            "summary": a.get("annotations", {}).get("summary"),
            "startsAt": a.get("startsAt"), "fingerprint": a.get("fingerprint"),
        }, ensure_ascii=False), flush=True)
        if a.get("status") == "firing":
            # 진단은 응답한 뒤에 한다: 앱 VM이 멈추면 도구마다 시간 초과를 기다리는데 그동안 Alertmanager를 붙잡지 않는다
            background.add_task(_handle, a, datetime.now(timezone.utc))
    return {"received": len(received)}
