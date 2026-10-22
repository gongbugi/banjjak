"""운영 AI ②: 원인 코드 → 조치 · 등급 → 기다림 → 실행 (또는 거부 · 드라이런 · 사람에게).

등급은 조치 종류가 코드로 정한다. 같은 조치를 1시간에 2번 넘게 하면 한 단계 올린다 (볼트 02 운영 AI 조치 등급).
T1: 5분 안에 거부가 없으면 실행. T2: 승인 전에는 실행하지 않는다. T0는 첫 T0 조치(#20 LLM 대체 모델)와 함께 만든다.
판단 기록과 기다리는 조치는 SQLite(운영 VM 디스크)에 둔다 → 파드가 바뀌어도 이어받는다.
mode: execute(실행) · dry-run(쓰기 없이 요청만 만들어 본다). Azure는 Slack 버튼(#8 2단계)이 생길 때까지 dry-run이고 쓰기 권한도 없다.
"""
import copy
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Callable, NamedTuple

from banjjak.checklist import NS

WAIT = timedelta(minutes=5)   # T1 무응답 대기 (본인 결정)
HOUR = timedelta(hours=1)
KST = timezone(timedelta(hours=9))

# 조치 → (등급, 사람이 읽을 이름)
ACTIONS = {
    "rollback_web": ("T1", "웹을 이전 이미지로 롤백"),
    "scale_web": ("T1", "웹 레플리카를 2개로"),
    "resume_collector": ("T1", "수집기 CronJob 재개"),
    "resume_analyzer": ("T1", "분석기 CronJob 재개"),
    "restart_app_vm": ("T2", "앱 VM 재시작"),
}
# 원인 → 조치. 없으면 조치 없이 사람에게 (Job 다시 돌리기 · 재시작을 고르지 않는 이유는 #8 변경 기록)
PLAN = {
    "recent_deploy": "rollback_web",
    "traffic_surge": "scale_web",
    "collector_suspended": "resume_collector",
    "analyzer_suspended": "resume_analyzer",
    "app_unreachable": "restart_app_vm",
}

# state: human(조치 없음) · waiting · executed · dry_run · rejected · failed
SCHEMA = """CREATE TABLE IF NOT EXISTS records (
    id INTEGER PRIMARY KEY, created REAL NOT NULL, alertname TEXT, cause TEXT, action TEXT, tier TEXT,
    state TEXT NOT NULL, due REAL, decided REAL, result TEXT, diagnosis TEXT NOT NULL)"""


class Hands(NamedTuple):
    """조치에 쓰는 바깥: 앱 클러스터 읽기 · 쓰기, Slack, 실행 여부, 클러스터 이름."""
    kube: Callable          # path -> dict | str
    patch: Callable         # (path, body, content_type) -> dict
    slack: Callable         # text -> None
    mode: str               # execute · dry-run
    cluster: str


def connect(path):
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute(SCHEMA)
    return conn


def plan(conn, record, now):
    """진단 기록 1건 → 조치 · 등급을 정해 저장하고 그 행을 돌려준다."""
    action = PLAN.get(record["cause"])
    tier, state, due, note = None, "human", None, None
    if action:
        tier, state = ACTIONS[action][0], "waiting"
        before = conn.execute("SELECT count(*) FROM records WHERE action = ? AND created > ?",
                              (action, (now - HOUR).timestamp())).fetchone()[0]
        if before >= 2 and tier == "T1":
            tier, note = "T2", f"1시간에 {before + 1}번째 → T2로 올림"
        if tier == "T1":
            due = (now + WAIT).timestamp()
    cur = conn.execute(
        "INSERT INTO records (created, alertname, cause, action, tier, state, due, result, diagnosis)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (now.timestamp(), record["alertname"], record["cause"], action, tier, state, due, note,
         json.dumps(record, ensure_ascii=False)))
    conn.commit()
    return _row(conn, cur.lastrowid)


def describe(row, mode):
    """보고에 붙는 조치 한 줄."""
    if not row["action"]:
        return "조치: 없음 → 사람이 봐 주세요"
    if row["tier"] == "T2":
        when = "승인 전에는 실행하지 않는다 (승인 버튼은 #8 2단계)"
    else:
        at = datetime.fromtimestamp(row["due"], KST).strftime("%H:%M")
        when = f"5분 뒤 {'실행' if mode == 'execute' else '드라이런'} ({at} KST, 거부 버튼은 #8 2단계)"
    note = f" · {row['result']}" if row["result"] else ""
    return f"조치: {ACTIONS[row['action']][1]} ({row['tier']}) · {when} · 기록 #{row['id']}{note}"


def tick(conn, now, hands):
    """기다린 지 5분이 지난 T1을 실행한다 (거부가 없었다). 운영 AI가 주기적으로 부른다."""
    for row in conn.execute("SELECT * FROM records WHERE state = 'waiting' AND due <= ?",
                            (now.timestamp(),)).fetchall():
        _carry_out(conn, dict(row), now, hands, by="5분 무응답")


def decide(conn, record_id, approve, now, hands, by):
    """기다리는 조치에 사람이 답한다 (Slack 버튼이 부른다). 이미 끝난 조치면 None."""
    row = conn.execute("SELECT * FROM records WHERE id = ? AND state = 'waiting'", (record_id,)).fetchone()
    if row is None:
        return None
    if approve:
        return _carry_out(conn, dict(row), now, hands, by)
    return _finish(conn, dict(row), now, "rejected", f"거부 ({by})", hands)


ICON = {"executed": "✅ 실행", "dry_run": "📝 드라이런 (실행하지 않음)", "rejected": "🚫 거부",
        "failed": "⚠️ 실패 → 사람이 봐 주세요", "human": "🙋 운영 AI가 할 수 없는 조치 → 사람이 한다"}


def _carry_out(conn, row, now, hands, by):
    run = RUN.get(row["action"])
    if run is None:
        return _finish(conn, row, now, "human", by, hands)
    execute = hands.mode == "execute"
    # 드라이런도 읽기는 다 해서 실제로 보낼 요청까지 만든다. 쓰기만 하지 않는다
    patch = hands.patch if execute else (lambda path, body, content_type: None)
    try:
        state, result = ("executed" if execute else "dry_run"), f"{run(hands.kube, patch)} ({by})"
    except Exception as e:
        state, result = "failed", f"{type(e).__name__}: {e}"
    return _finish(conn, row, now, state, result, hands)


def _finish(conn, row, now, state, result, hands):
    conn.execute("UPDATE records SET state = ?, decided = ?, result = ? WHERE id = ?",
                 (state, now.timestamp(), result, row["id"]))
    conn.commit()
    hands.slack(f"{ICON[state]} [{hands.cluster}] · 기록 #{row['id']} {ACTIONS[row['action']][1]} · {result}")
    return _row(conn, row["id"])


def _row(conn, record_id):
    return dict(conn.execute("SELECT * FROM records WHERE id = ?", (record_id,)).fetchone())


# --- 실행기: 앱 클러스터 banjjak 네임스페이스에 PATCH 한 번. 권한은 RBAC가 이 조치 · 이름으로만 준다 ---
# ponytail: 롤백 권한은 웹 이미지를 바꿀 수 있는 권한과 같다 (RBAC는 어떤 필드를 바꾸는지까지 못 막는다).
# 이름 제한 + 이 허용 목록까지가 선이다. 더 막으려면 승인 정책(ValidatingAdmissionPolicy)

REVISION = "deployment.kubernetes.io/revision"
MERGE = "application/merge-patch+json"


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
        {"op": "replace", "path": "/spec/template", "value": template},
        {"op": "add", "path": "/metadata/annotations/kubernetes.io~1change-cause",
         "value": f"ops-ai rollback to revision {revision(prev)}"},
    ], "application/json-patch+json")
    tag = template["spec"]["containers"][0]["image"].rsplit(":", 1)[-1][:12]
    return f"판 {current} → {revision(prev)} ({tag})"


def _scale_web(kube, patch):
    patch(f"/apis/apps/v1{NS}/deployments/web/scale", {"spec": {"replicas": 2}}, MERGE)
    return "웹 레플리카 2"


def _resume(cronjob):
    def run(kube, patch):
        patch(f"/apis/batch/v1{NS}/cronjobs/{cronjob}", {"spec": {"suspend": False}}, MERGE)
        return f"{cronjob} 다시 켬"
    return run


# 앱 VM 재시작(T2)은 실행기가 없다: 운영 AI에 Azure 권한이 없어 승인돼도 사람이 한다
RUN = {"rollback_web": _rollback_web, "scale_web": _scale_web,
       "resume_collector": _resume("collector"), "resume_analyzer": _resume("analyzer")}
