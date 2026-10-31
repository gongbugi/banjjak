"""운영 AI ② · ③: 원인 코드 → 조치 · 등급 → 기다림 → 실행 → 확인 (또는 거부 · 드라이런 · 사람에게).

등급은 조치 종류가 코드로 정한다. 같은 조치를 1시간에 2번 넘게 하면 한 단계 올린다 (볼트 02 운영 AI 조치 등급).
T1: 보고가 나간 뒤 5분 안에 거부가 없으면 실행. T2: 승인 전에는 실행하지 않는다. T0는 첫 T0 조치(#20 LLM 대체 모델)와 함께 만든다.
승인 · 타이머는 기록 번호만 가리키므로 실행 직전에 진단 때와 같은지 다시 본다 (보안 점검 1회차).
확인(#9): 실행한 조치는 정해진 시간 동안 1분마다 본다. 경보가 풀리면 회복, 나빠지면 바로 사람에게,
시간 안에 안 풀리면 재진단 1회(opsai.tick) → 그 조치도 안 먹히면 사람에게.
판단 · 조치 · 확인 기록은 SQLite(운영 VM 디스크)에 둔다 → 파드가 바뀌어도 이어받는다.
mode: execute(실행) · dry-run(쓰기 없이 요청만 만들어 본다). 실행은 클러스터마다 정한다 (2026-10-24부터 로컬 · Azure 모두 execute).
"""
import copy
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Callable, NamedTuple

from banjjak.checklist import NS, _t

WAIT = timedelta(minutes=5)   # T1 무응답 대기 (본인 결정)
HOUR = timedelta(hours=1)
STALE = timedelta(hours=1)   # 이보다 오래된 진단으로는 실행하지 않는다 (T2는 마감이 없어 며칠 뒤 버튼도 눌린다)
KST = timezone(timedelta(hours=9))

# 조치 → (등급, 사람이 읽을 이름)
ACTIONS = {
    "rollback_web": ("T1", "웹을 이전 이미지로 롤백"),
    "resume_collector": ("T1", "수집기 CronJob 재개"),
    "resume_analyzer": ("T1", "분석기 CronJob 재개"),
    "restart_app_vm": ("T2", "앱 VM 재시작"),
}
# 원인 → 조치. 없으면 조치 없이 사람에게 (Job 다시 돌리기 · 재시작을 고르지 않는 이유는 #8 변경 기록)
# 급증(traffic_surge)도 조치 없음: 레플리카는 HPA가 맡는다 (#11, 최대 2). 운영 AI가 늘려도 HPA가 바로 덮어쓴다
PLAN = {
    "recent_deploy": "rollback_web",
    "collector_suspended": "resume_collector",
    "analyzer_suspended": "resume_analyzer",
    "app_unreachable": "restart_app_vm",
}

# 확인 시간: 조치 → 경보가 풀리기를 기다리는 시간 (🟡 2026-10-22 Claude 제안, 장애 실험 실측으로 다시 본다)
VERIFY = {
    "rollback_web": timedelta(minutes=10),       # 파드 교체 약 1분 + 경보 식 5분 창이 비워지는 시간 + 여유
    "resume_collector": timedelta(minutes=70),   # 다음 정시 수집까지 최대 60분 + 지표 반영
    "resume_analyzer": timedelta(minutes=90),    # 다음 :20 실행까지 최대 60분 + 실행 시간 (다음 Job 성공으로 본다)
}
FIRST_CHECK = timedelta(minutes=2)   # 첫 확인. 악화는 두 번째 확인부터 본다 (파드 교체 순간의 튐을 악화로 보지 않는다)
EVERY_CHECK = timedelta(minutes=1)
# 악화를 재는 웹 SLI (경보 규칙과 같은 범위). 5xx가 한 번도 없으면 분자가 비어 0으로 본다
ERR = ('(sum(rate(banjjak_http_request_duration_seconds_count{route=~"/|/api/trending", status=~"5.."}[5m])) or vector(0))'
       ' / sum(rate(banjjak_http_request_duration_seconds_count{route=~"/|/api/trending"}[5m]))')
P95 = 'histogram_quantile(0.95, sum by (le) (rate(banjjak_http_request_duration_seconds_bucket{route=~"/|/api/trending"}[5m])))'

# records.state: human(조치 없음) · waiting · running(실행 중) · executed · dry_run · rejected · failed · skipped(실행 직전에 멈춤)
# checks.state: checking · recovered · not_recovered · worse
SCHEMA = """CREATE TABLE IF NOT EXISTS records (
    id INTEGER PRIMARY KEY, created REAL NOT NULL, alertname TEXT, cause TEXT, action TEXT, tier TEXT,
    state TEXT NOT NULL, due REAL, decided REAL, result TEXT, diagnosis TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS checks (
    record_id INTEGER PRIMARY KEY, until REAL NOT NULL, next REAL NOT NULL, count INTEGER NOT NULL DEFAULT 0,
    before_err REAL, before_p95 REAL, state TEXT NOT NULL, decided REAL, result TEXT);
CREATE TABLE IF NOT EXISTS snapshots (
    record_id INTEGER PRIMARY KEY, taken REAL NOT NULL, tape TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY, at REAL NOT NULL, ledger TEXT NOT NULL, kind TEXT NOT NULL, note TEXT, model TEXT,
    input INTEGER, cache_write INTEGER, cache_read INTEGER, output INTEGER, cost REAL NOT NULL DEFAULT 0)"""


class Hands(NamedTuple):
    """조치에 쓰는 바깥: 앱 클러스터 읽기 · 쓰기, Slack, 실행 여부, 클러스터 이름, 버튼을 받을 수 있나, 운영 Prometheus."""
    kube: Callable          # path -> dict | str
    patch: Callable         # (path, body, content_type) -> dict
    slack: Callable         # text 또는 {"text", "blocks"} -> None
    mode: str               # execute · dry-run
    cluster: str
    buttons: bool = False   # Slack 버튼 클릭이 여기로 오나 (Socket Mode 앱 토큰이 있는 한 곳만)
    prom: Callable = None   # query -> float | None (확인 · 악화 판정)
    llm: Callable = None    # (conn, alert, tape, now) -> 진단 | {"failed": 이유}. 없으면 점검표만 (키가 없는 곳)
    gate: Callable = None   # (conn, record, action, now) -> {"p", "say"}. 없으면 표대로


def connect(path):
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def plan(conn, record, now, tried=None, gate=None):
    """진단 기록 1건 → 조치 · 등급을 정해 저장하고 그 행을 돌려준다.
    tried: 재진단이면 안 먹힌 앞선 조치. 같은 조치를 또 고르면 다시 하지 않고 사람에게.
    조치는 표가 고른다. LLM(ask_human)과 jev(gate)는 등급을 올리기만 한다."""
    action = PLAN.get(record["cause"])
    tier, state, due, note = None, "human", None, None
    if tried and action == tried:
        action, note = None, f"재진단도 같은 조치({ACTIONS[tried][1]})를 고른다 → 사람에게"
    if action:
        tier, state = ACTIONS[action][0], "waiting"
        before = conn.execute("SELECT count(*) FROM records WHERE action = ? AND created > ?",
                              (action, (now - HOUR).timestamp())).fetchone()[0]
        if before >= 2 and tier == "T1":
            tier, note = "T2", f"1시간에 {before + 1}번째 → T2로 올림"
        if tier == "T1" and record.get("ask_human"):
            tier, note = "T2", "LLM이 승인을 요청 → T2로 올림"
        if tier == "T1" and (why := _held(conn, record, action, now, tried)):
            tier, note = "T2", f"{why} → T2로 올림"
        if tier == "T1" and gate:
            record["gate"] = said = gate(conn, record, action, now)
            if said["say"] == "approve":
                tier, note = "T2", f"jev {said['p']:.2f} → T2로 올림"
            elif said["say"] == "hold":
                action, tier, state, note = None, None, "human", f"jev {said['p']:.2f} → 사람에게"
        if tier == "T1":
            due = (now + WAIT).timestamp()
    cur = conn.execute(
        "INSERT INTO records (created, alertname, cause, action, tier, state, due, result, diagnosis)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (now.timestamp(), record["alertname"], record["cause"], action, tier, state, None, note,
         json.dumps(record, ensure_ascii=False)))
    conn.commit()
    return {**_row(conn, cur.lastrowid), "due": due}   # DB의 due는 보고가 나간 뒤에 적는다 (arm)


def _held(conn, record, action, now, tried):
    """타이머로 실행하면 안 되는 이유. 사람이 이미 답했거나 판단이 엇갈린 조치는 승인을 기다린다."""
    if conn.execute("SELECT 1 FROM records WHERE action = ? AND state = 'rejected' AND decided > ?",
                    (action, (now - HOUR).timestamp())).fetchone():
        return "1시간 안에 거부한 조치"
    # 재전송은 LLM을 다시 부르지 않아 앞선 unknown · ask_human · jev 보류가 안 보인다 → 같은 경보면 타이머를 걸지 않는다
    if not tried and conn.execute(
            "SELECT 1 FROM records WHERE json_extract(diagnosis, '$.fingerprint') = ? AND json_extract(diagnosis, '$.startsAt') = ?",
            (record.get("fingerprint"), record.get("startsAt"))).fetchone():
        return "같은 경보를 다시 받았다"
    if record["cause"] != record["checklist"]["cause"]:
        return "LLM과 점검표의 원인이 다르다"
    if record["alertname"] == "EntranceErrorsHigh" and action == "rollback_web":
        return "입구 오류에는 공개 확인 경로의 503도 섞인다"
    return None


def arm(conn, row, delivered):
    """T1 타이머는 보고가 나간 뒤에 건다: "5분 안에 거부할 수 있었다"가 성립해야 무응답을 동의로 친다."""
    if row["tier"] != "T1":
        return row
    if delivered:
        conn.execute("UPDATE records SET due = ? WHERE id = ?", (row["due"], row["id"]))
    else:
        conn.execute("UPDATE records SET tier = 'T2', result = '보고 실패 → T2로 올림' WHERE id = ?", (row["id"],))
    conn.commit()
    return _row(conn, row["id"])


def describe(row, mode, buttons):
    """보고에 붙는 조치 한 줄."""
    if not row["action"]:
        return "조치: 없음 → 사람이 봐 주세요" + (f" · {row['result']}" if row["result"] else "")
    if row["tier"] == "T2":
        when = "승인 전에는 실행하지 않는다" + ("" if buttons else " (버튼 없음: 이 클러스터에서는 승인할 길이 없다)")
    else:
        at = datetime.fromtimestamp(row["due"], KST).strftime("%H:%M")
        answer = "그 전에 버튼으로 승인 · 거부" if buttons else "버튼 없음"
        when = f"5분 뒤 {'실행' if mode == 'execute' else '드라이런'} ({at} KST, {answer})"
    note = f" · {row['result']}" if row["result"] else ""
    return f"조치: {ACTIONS[row['action']][1]} ({row['tier']}) · {when} · 기록 #{row['id']}{note}"


def tick(conn, now, hands):
    """기다린 지 5분이 지난 T1을 실행한다 (거부가 없었다). 운영 AI가 주기적으로 부른다."""
    for row in conn.execute("SELECT * FROM records WHERE state = 'waiting' AND due <= ?",
                            (now.timestamp(),)).fetchall():
        if _claim(conn, row["id"]):
            _carry_out(conn, dict(row), now, hands, by="5분 무응답", timer=True)


def decide(conn, record_id, approve, now, hands, by):
    """기다리는 조치에 사람이 답한다 (Slack 버튼이 부른다). 이미 끝난 조치면 None."""
    if not _claim(conn, record_id):
        return None
    row = _row(conn, record_id)
    if approve:
        return _carry_out(conn, row, now, hands, by)
    return _finish(conn, row, now, "rejected", f"거부 ({by})", hands)


def _claim(conn, record_id):
    """기다리는 조치를 한 곳만 잡는다. 타이머 · 승인 · 거부가 겹쳐도 먼저 잡은 쪽만 움직인다.
    ponytail: 잡은 채 프로세스가 죽으면 running으로 남고 다시 돌지 않는다 (두 번 실행하지 않는 쪽을 골랐다. 경보는 Alertmanager가 따로 보낸다)."""
    claimed = conn.execute("UPDATE records SET state = 'running' WHERE id = ? AND state = 'waiting'", (record_id,)).rowcount
    conn.commit()
    return claimed == 1


ICON = {"executed": "✅ 실행", "dry_run": "📝 드라이런 (실행하지 않음)", "rejected": "🚫 거부",
        "failed": "⚠️ 실패 → 사람이 봐 주세요", "human": "🙋 운영 AI가 할 수 없는 조치 → 사람이 한다",
        "running": "⏳ 실행 중", "skipped": "⏭️ 실행하지 않음 → 사람이 봐 주세요"}


def _carry_out(conn, row, now, hands, by, timer=False):
    run = RUN.get(row["action"])
    if run is None:
        return _finish(conn, row, now, "human", by, hands)
    execute = hands.mode == "execute"
    # 드라이런도 읽기는 다 해서 실제로 보낼 요청까지 만든다. 쓰기만 하지 않는다
    patch = hands.patch if execute else (lambda path, body, content_type: None)
    before = _snapshot(hands.prom) if execute else (None, None)   # 악화는 "조치 직전" 대비
    why = _changed(row, now, hands, timer)
    if why:
        return _finish(conn, row, now, "skipped", f"{why} ({by})", hands)
    try:
        state, result = ("executed" if execute else "dry_run"), f"{run(hands.kube, patch)} ({by})"
    except Exception as e:
        state, result = "failed", f"{type(e).__name__}: {e}"
    done = _finish(conn, row, now, state, result, hands)
    if state == "executed":   # 실제로 바꾼 것만 확인한다
        conn.execute("INSERT INTO checks (record_id, until, next, before_err, before_p95, state)"
                     " VALUES (?, ?, ?, ?, ?, 'checking')",
                     (row["id"], (now + VERIFY[row["action"]]).timestamp(), (now + FIRST_CHECK).timestamp(), *before))
        conn.commit()
    return done


def _changed(row, now, hands, timer):
    """실행 직전에 다시 본다. 진단 때와 달라졌거나 못 보면 그 이유 (실행하지 않는다). 경보가 풀렸는지는 타이머만 본다: 사람의 승인은 그걸 알고 누른 것이다."""
    if now.timestamp() - row["created"] > STALE.total_seconds():
        return f"진단한 지 {int(STALE.total_seconds() // 3600)}시간이 넘었다"
    try:
        seen = json.loads(row["diagnosis"]).get("revision")
        if row["action"] == "rollback_web" and (seen is None or seen != revision(hands.kube)):
            return "진단 뒤 판이 바뀌었다"
        if timer and hands.prom(f'ALERTS{{alertname="{row["alertname"]}",alertstate="firing"}}') is None:
            return "경보가 풀렸다"
    except Exception as e:
        return f"다시 확인 못 함 ({type(e).__name__})"
    return None


def check(conn, now, hands):
    """실행한 조치를 1분마다 본다 (#9). 돌려주는 값: 안 먹혀서 재진단할 (기록, 이유) 목록."""
    redo = []
    for c in conn.execute("SELECT * FROM checks WHERE state = 'checking' AND next <= ?",
                          (now.timestamp(),)).fetchall():
        c, rec = dict(c), _row(conn, c["record_id"])
        count = c["count"] + 1
        try:
            verdict = _judge(c, rec, count, now, hands)
        except Exception as e:   # 못 읽었으면 이번엔 판정하지 않는다. 확인 시간이 지나면 미회복
            print(f"check #{rec['id']} failed: {type(e).__name__}: {e}", flush=True)
            verdict = ("not_recovered", f"확인 못 함 ({type(e).__name__})") if now.timestamp() >= c["until"] else None
        if verdict is None:
            conn.execute("UPDATE checks SET count = ?, next = ? WHERE record_id = ?",
                         (count, (now + EVERY_CHECK).timestamp(), rec["id"]))
            continue
        state, result = verdict
        conn.execute("UPDATE checks SET count = ?, state = ?, decided = ?, result = ? WHERE record_id = ?",
                     (count, state, now.timestamp(), result, rec["id"]))
        again = json.loads(rec["diagnosis"]).get("after")   # 이 조치가 이미 재진단으로 고른 것인가
        if state == "not_recovered" and not again:
            redo.append((rec, result))
        head = {"recovered": "✅ 회복", "worse": "🚨 악화 → 사람이 봐 주세요",
                "not_recovered": "🙋 재진단한 조치도 안 먹힘 → 사람이 봐 주세요" if again else "🔁 안 먹힘 → 재진단"}[state]
        hands.slack(f"{head} [{hands.cluster}] · 기록 #{rec['id']} {ACTIONS[rec['action']][1]} · {result}")
    conn.commit()
    return redo


def _judge(c, rec, count, now, hands):
    """한 번 본다: (상태, 결과) 또는 아직이면 None."""
    minutes = int((now.timestamp() - rec["decided"]) // 60)
    if rec["action"] == "resume_analyzer":   # 분석 성공률은 24시간 비율이라 재개만으로는 한참 안 풀린다
        if _next_job_succeeded(hands.kube, "analyzer", rec["decided"]):
            return "recovered", f"{minutes}분 뒤 다음 분석 Job 성공"
    elif hands.prom(f'ALERTS{{alertname="{rec["alertname"]}",alertstate="firing"}}') is None:
        return "recovered", f"{minutes}분 뒤 경보 풀림"
    if count >= 2:   # 첫 확인은 악화 판정에서 유예
        worse = _worse(c, hands.prom)
        if worse:
            return "worse", worse
    if now.timestamp() >= c["until"]:
        return "not_recovered", f"{int(VERIFY[rec['action']].total_seconds() // 60)}분 안에 풀리지 않았다"
    return None


def _snapshot(prom):
    """웹 오류율 · p95. 못 재면 None (요청이 없거나 Prometheus가 답하지 않는다)."""
    def read(query):
        try:
            return prom(query) if prom else None
        except Exception:
            return None
    return read(ERR), read(P95)


def _worse(c, prom):
    """조치 직전보다 오류율 1.5배 이상이면서 2% 이상, 또는 p95 1.5배 이상 (스펙 #1)."""
    err, p95 = _snapshot(prom)
    before_err, before_p95 = c["before_err"] or 0.0, c["before_p95"]
    if err is not None and err >= 0.02 and err >= 1.5 * before_err:
        return f"오류율 {before_err:.1%} → {err:.1%}"
    if p95 is not None and before_p95 and p95 >= 1.5 * before_p95:
        return f"p95 {before_p95:.2f} → {p95:.2f}초"
    return None


def _next_job_succeeded(kube, cronjob, since):
    jobs = kube(f"/apis/batch/v1{NS}/jobs")["items"]
    return any(j["status"].get("succeeded") and _t(j["metadata"]["creationTimestamp"]).timestamp() > since
               and any(o["kind"] == "CronJob" and o["name"] == cronjob for o in j["metadata"].get("ownerReferences", []))
               for j in jobs)


def _finish(conn, row, now, state, result, hands):
    conn.execute("UPDATE records SET state = ?, decided = ?, result = ? WHERE id = ?",
                 (state, now.timestamp(), result, row["id"]))
    conn.commit()
    hands.slack(f"{ICON[state]} [{hands.cluster}] · 기록 #{row['id']} {ACTIONS[row['action']][1]} · {result}")
    return _row(conn, row["id"])


def _row(conn, record_id):
    return dict(conn.execute("SELECT * FROM records WHERE id = ?", (record_id,)).fetchone())


# --- 실행기: 앱 클러스터 banjjak 네임스페이스에 PATCH 한 번. 권한은 RBAC가 이 조치 · 이름으로만 준다 ---
# RBAC는 이름까지, 어떤 필드를 어떻게 바꾸는지는 승인 정책(deploy/opsai-act, ValidatingAdmissionPolicy)이 막는다

REVISION = "deployment.kubernetes.io/revision"
MERGE = "application/merge-patch+json"


def revision(kube):
    """웹 배포의 지금 판 번호. 진단 때 적어 두고 실행 직전에 견준다."""
    return kube(f"/apis/apps/v1{NS}/deployments/web")["metadata"].get("annotations", {}).get(REVISION)


def _rollback_web(kube, patch):
    """kubectl rollout undo와 같다: 지금 판 바로 앞 ReplicaSet의 template으로 되돌린다."""
    notes = kube(f"/apis/apps/v1{NS}/deployments/web")["metadata"].get("annotations", {})
    if notes.get("kubernetes.io/change-cause", "").startswith("ops-ai rollback"):
        # 되돌린 판도 "최근 배포"로 보인다. 또 되돌리면 망가진 판으로 돌아간다
        raise RuntimeError("이미 운영 AI가 되돌린 판이라 또 되돌리지 않는다")
    current = int(notes[REVISION])
    revision = lambda s: int(s["metadata"].get("annotations", {}).get(REVISION, 0))
    older = [s for s in kube(f"/apis/apps/v1{NS}/replicasets?labelSelector=app%3Dweb")["items"] if revision(s) < current]
    if not older:
        raise LookupError("되돌아갈 이전 판이 없다")
    prev = max(older, key=revision)
    template = copy.deepcopy(prev["spec"]["template"])
    template["metadata"]["labels"].pop("pod-template-hash", None)   # k8s가 다시 붙인다
    patch(f"/apis/apps/v1{NS}/deployments/web", [
        {"op": "test", "path": "/metadata/annotations/deployment.kubernetes.io~1revision", "value": notes[REVISION]},
        {"op": "replace", "path": "/spec/template", "value": template},
        {"op": "add", "path": "/metadata/annotations/kubernetes.io~1change-cause",
         "value": f"ops-ai rollback to revision {revision(prev)}"},
    ], "application/json-patch+json")
    tag = template["spec"]["containers"][0]["image"].rsplit(":", 1)[-1][:12]
    return f"판 {current} → {revision(prev)} ({tag})"


def _resume(cronjob):
    def run(kube, patch):
        patch(f"/apis/batch/v1{NS}/cronjobs/{cronjob}", {"spec": {"suspend": False}}, MERGE)
        return f"{cronjob} 다시 켬"
    return run


# 앱 VM 재시작(T2)은 실행기가 없다: 운영 AI에 Azure 권한이 없어 승인돼도 사람이 한다
RUN = {"rollback_web": _rollback_web,
       "resume_collector": _resume("collector"), "resume_analyzer": _resume("analyzer")}
