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
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager, closing
from datetime import datetime, timezone

from fastapi import BackgroundTasks, FastAPI, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from banjjak import actions, appcluster, checklist, llm
from banjjak.heartbeat import ping

BEAT_EVERY = 60   # 초. Better Stack에는 더 긴 간격으로 걸어 한두 번 빠져도 버틴다
TICK_EVERY = 15   # 초. 5분 기다린 T1 실행 · 실행한 조치 확인을 보는 주기

# 분류는 코드다: 종류 = 알림 규칙 이름, 심각도 = 그 규칙이 지키는 SLI (목표값은 스펙 #1)
# critical: 방문자가 지금 못 쓰거나 운영자가 아무것도 못 본다. warning: 쓸 수는 있는데 목표를 벗어났다
KINDS = {
    "WebErrorRateHigh": ("가용성 99%", "critical"),
    "DataAgeMissing": ("데이터 신선도 2시간", "critical"),   # 지표가 아예 없다 = 눈이 멀었다
    "WebLatencyP95High": ("응답 p95 500ms", "warning"),
    "DataStale": ("데이터 신선도 2시간", "warning"),
    "AnalysisSuccessLow": ("분석 성공률 95%", "warning"),
    "EntranceErrorsHigh": ("가용성 99% (입구)", "critical"),   # 웹이 응답 못 함: 웹 지표에는 안 잡힌다 (#11)
    "WebRequestsRejected": ("요청 제한 초당 320건", "warning"),
}


def handle(alert, prom, now, conn, hands, after=None):
    """울린 알림 1건: 분류(코드) → 진단(점검표 · LLM) → 조치 · 등급(표) → 기록 → Slack 보고. 실행은 tick이 5분 뒤에 한다.
    hands.cluster: 이 운영 AI가 지키는 곳 (azure · local-k3d). 둘이 같은 Slack 채널에 보내므로 보고마다 밝힌다.
    after: 재진단이면 (안 먹힌 앞선 기록, 이유). "안 먹혔다"를 첫 증거로 남기고, 같은 조치를 또 고르면 사람에게.
    LLM이 있으면 그 판단으로 조치하고, 실패하면 점검표 판단으로 간다. 두 판단은 늘 함께 남긴다 (#12 채점).
    ponytail: 재진단은 점검표만 본다 (경보 하나에 LLM 1번). LLM이 다른 원인을 찾을 기회는 없고 같은 조치면 사람에게 간다."""
    kind = alert.get("labels", {}).get("alertname")
    sli, severity = KINDS.get(kind, (None, "unknown"))
    tape, kube, prom_ = checklist.recorder(hands.kube, prom)
    found = checklist.diagnose(kind, kube, prom_, now)
    record = {"event": "diagnosis", "cluster": hands.cluster, "at": now.isoformat(), "alertname": kind,
              "startsAt": alert.get("startsAt"), "fingerprint": alert.get("fingerprint"), "sli": sli,
              "severity": severity, **found, "checklist": found}
    checklist.sweep(kube, prom_, now)   # 이 점검표가 안 본 읽기까지 녹화 (#20 진단기를 같은 상태로 다시 돌린다)
    if hands.llm and not after and kind in KINDS:
        record["llm"] = said = hands.llm(conn, alert, tape, now)
        if "failed" not in said:
            record.update(said)
        elif said.get("tell"):
            hands.slack(f"💸 LLM 상한 [{hands.cluster}] · {said['failed']} → 점검표로 진단한다")
    if after:
        before, reason = after
        record["after"] = before["id"]
        record["evidence"].insert(0, {"look": "앞선 조치",
                                      "saw": f"기록 #{before['id']} {actions.ACTIONS[before['action']][1]} · {reason}"})
    print(json.dumps(record, ensure_ascii=False), flush=True)
    # 판단 기록도 이 행(diagnosis 칸)에 남는다 → #12 채점이 읽는다
    row = actions.plan(conn, record, now, tried=after[0]["action"] if after else None, gate=hands.gate)
    conn.execute("INSERT INTO snapshots (record_id, taken, tape) VALUES (?, ?, ?)",
                 (row["id"], now.timestamp(), json.dumps(tape, ensure_ascii=False)))
    conn.commit()
    hands.slack(message(report(record, row, hands), row, hands))
    return {**record, **{k: row[k] for k in ("id", "action", "tier", "state")}}


def tick(conn, now, hands):
    """주기 처리: 5분 기다린 T1 실행(#8) → 실행한 조치 확인(#9) → 안 먹힌 조치는 재진단 1회."""
    actions.tick(conn, now, hands)
    for before, reason in actions.check(conn, now, hands):
        diagnosis = json.loads(before["diagnosis"])
        alert = {"labels": {"alertname": before["alertname"]}, "startsAt": diagnosis.get("startsAt"),
                 "fingerprint": diagnosis.get("fingerprint")}
        handle(alert, hands.prom, now, conn, hands, after=(before, reason))


def message(text, row, hands):
    """기다리는 조치면 글 아래에 승인 · 거부 버튼을 붙인다. 클릭을 받을 수 없는 곳(앱 토큰 없음)은 글만.
    버튼 값은 "클러스터:기록 번호": 로컬 · Azure가 같은 채널에 보내므로, 번호만으로는 어느 DB의 기록인지 모른다."""
    if row["state"] != "waiting" or not hands.buttons:
        return text
    value = f"{hands.cluster}:{row['id']}"
    button = lambda action_id, label, style: {"type": "button", "action_id": action_id, "value": value,
                                               "style": style, "text": {"type": "plain_text", "text": label}}
    now_label = "승인 (지금 실행)" if hands.mode == "execute" else "승인 (지금 드라이런)"
    return {"text": text, "blocks": [
        {"type": "section", "text": {"type": "mrkdwn", "text": text[:2900]}},   # section 글은 3000자까지
        {"type": "actions", "elements": [button("approve", now_label, "primary"), button("reject", "거부", "danger")]},
    ]}


def on_click(payload, now, conn, hands, approver, respond):
    """Slack 버튼 1번. 승인자만 누를 수 있다. 승인은 지금 실행(T1 · T2), 거부는 실행하지 않는다.
    respond(response_url, body): 원래 글을 버튼 없는 글로 바꾸거나, 누른 사람에게만 보이는 답을 보낸다."""
    url, user = payload["response_url"], payload.get("user", {}).get("id")
    clicked = payload["actions"][0]
    cluster, _, record_id = clicked["value"].rpartition(":")
    record_id = int(record_id)
    if not approver or user != approver:
        return respond(url, {"response_type": "ephemeral", "replace_original": False,
                             "text": "이 버튼은 승인자만 누를 수 있다"})
    if cluster != hands.cluster:   # 같은 번호의 다른 기록을 건드리지 않는다
        return respond(url, {"response_type": "ephemeral", "replace_original": False,
                             "text": f"다른 클러스터({cluster or '모름'})의 버튼이라 여기({hands.cluster})서는 처리하지 않는다"})
    row = actions.decide(conn, record_id, clicked["action_id"] == "approve", now, hands, by="Slack 버튼")
    original = payload.get("message", {}).get("text", "")
    if row is None:   # 5분 무응답으로 이미 실행됐거나 이미 누른 버튼
        found = conn.execute("SELECT * FROM records WHERE id = ?", (record_id,)).fetchone()
        if found is None:
            return respond(url, {"response_type": "ephemeral", "replace_original": False,
                                 "text": f"기록 #{record_id}이 없다"})
        # 타이머로 끝난 조치는 원래 글의 버튼을 지울 수단이 없었다 → 지금 결과만 붙이고 버튼을 지운다
        done = f"이미 끝난 조치: {actions.ICON[found['state']]} · 기록 #{record_id} · {found['result']}"
    else:
        verdict = "승인" if clicked["action_id"] == "approve" else "거부"
        done = f"{verdict}: {actions.ICON[row['state']]} · 기록 #{row['id']} {actions.ACTIONS[row['action']][1]} · {row['result']}"
    return respond(url, {"replace_original": True, "text": f"{original}\n\n{done}" if original else done})


def report(r, row, hands):
    lines = [f"🩺 운영 AI 진단 [{r['cluster']}] · {r['alertname']} ({r['sli'] or 'SLI 모름'} · {r['severity']})",
             f"판단: {r['verdict'] or '판단 못 함 → 사람이 봐 주세요'}"]
    if "llm" in r:   # LLM이 못 본 이유는 기록에만 둔다 (금지어에 걸린 건을 구별할 수 없게 문구가 하나다)
        lines.append("LLM 판단 없음 (점검표로)" if "failed" in r["llm"] else f"점검표: {r['checklist']['verdict'] or '판단 못 함'}")
    lines.append("본 것:")
    lines += [f"• {e['look']}: {e['saw']}" for e in r["evidence"]]
    lines.append(actions.describe(row, hands.mode, hands.buttons))
    return "\n".join(lines)


# 앱 클러스터 읽기 · 쓰기. 할 수 있는 조치 · 이름은 RBAC가 정한다 (deploy/opsai-act)
_kube, _patch = appcluster.get, appcluster.patch


def _prom(query):
    url = os.environ["PROM_URL"] + "/api/v1/query?" + urllib.parse.urlencode({"query": query})
    with urllib.request.urlopen(url, timeout=10) as r:
        result = json.load(r)["data"]["result"]
    return float(result[0]["value"][1]) if result else None


def _slack(message):
    """글(str) 또는 버튼 붙은 글({"text", "blocks"})을 Incoming Webhook으로 보낸다."""
    url = os.environ.get("SLACK_WEBHOOK_URL")   # Secret slack-webhook. 없으면(로컬 · 시험) 기록만 한다
    if not url:
        return
    _post(url, message if isinstance(message, dict) else {"text": message})


def _respond(url, body):
    # 클릭에 딸려 온 response_url로만 답한다 (Slack 주소가 아니면 보내지 않는다)
    if not url.startswith("https://hooks.slack.com/"):
        raise ValueError(f"Slack response_url이 아니다: {url[:40]}")
    _post(url, body)


def _post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10).close()
    except Exception as e:   # 보고가 실패해도 기록은 남았다
        print(f"slack failed: {e}", flush=True)


def _buttons():
    """Slack 버튼을 Socket Mode로 받는다 (밖으로만 연결, 받는 주소를 열지 않는다).
    연결이 여러 개면 클릭이 그중 하나에만 간다 → 앱 토큰(Secret slack-socket)은 운영 VM 한 곳에만 둔다. 없으면 버튼 없이 돈다."""
    token = os.environ.get("SLACK_APP_TOKEN")
    if not token:
        return None
    from slack_sdk.socket_mode import SocketModeClient   # 버튼을 켠 곳에서만 쓴다
    from slack_sdk.socket_mode.response import SocketModeResponse

    def listener(client, req):
        client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))   # 3초 안에 받았다고 답한다
        if req.type == "interactive" and req.payload.get("type") == "block_actions":
            try:
                with _db() as conn:
                    on_click(req.payload, datetime.now(timezone.utc), conn, _hands(), os.environ.get("SLACK_APPROVER", ""),
                             _respond)
            except Exception as e:
                print(f"button failed: {type(e).__name__}: {e}", flush=True)

    client = SocketModeClient(app_token=token)
    client.socket_mode_request_listeners.append(listener)
    client.connect()
    print("slack buttons: connected (socket mode)", flush=True)
    return client


def _hands():
    # 실행 여부는 클러스터마다 (ConfigMap ops-ai-env). 값이 없으면 안전한 쪽(dry-run)
    return actions.Hands(_kube, _patch, _slack, os.environ.get("ACTIONS", "dry-run"),
                         os.environ.get("CLUSTER_NAME", "unknown"), bool(os.environ.get("SLACK_APP_TOKEN")), _prom,
                         _llm if os.environ.get("ANTHROPIC_API_KEY") else None,   # Secret anthropic. 없으면 점검표만
                         llm.gate if os.environ.get("TYPESAFE_API_KEY") else None)   # Secret typesafe. 없으면 표대로


def _llm(conn, alert, tape, now):
    return llm.judge(conn, alert, tape, now, llm.client())


def _db():
    return closing(actions.connect(os.environ.get("OPSAI_DB", "/data/opsai.db")))   # 운영 VM 디스크(PVC)


def _handle(alert, now):
    with _db() as conn:
        handle(alert, _prom, now, conn, _hands())


def _tick():
    try:
        with _db() as conn:
            tick(conn, datetime.now(timezone.utc), _hands())
    except Exception as e:   # 한 번 실패해도 다음 주기에 다시 본다 (기다리는 조치 · 확인은 DB에 그대로 있다)
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
    try:
        buttons = await asyncio.to_thread(_buttons)
    except Exception as e:   # 버튼이 안 돼도 진단 · 5분 무응답 실행은 돈다
        print(f"slack buttons failed: {type(e).__name__}: {e}", flush=True)
        buttons = None
    yield
    for t in tasks:
        t.cancel()
    if buttons:
        buttons.close()


app = FastAPI(title="banjjak-ops-ai", lifespan=lifespan)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


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
