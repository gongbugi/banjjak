"""장애 주입기 (#10): 운영 VM chaos 네임스페이스의 CronJob이 10분마다 한 번 돈다.

한 번 돌 때: 스위치 확인 → 진행 중인 주입을 돌봄(끝낼 때면 끝내고, 고쳐졌으면 기록) → 아니면 주사위 → 하나 골라 넣음.
규칙 (볼트 01 무작위 장애 주입 규칙 · 10-23 본인 결정, 빈도는 10-28 주 5회로): 평균 주 5회 · 밤 포함 · 한 번에 하나 · 주입 사이 최소 12시간 ·
배포 뒤 30분 안에는 안 넣음 · 일부는 저절로 회복, 일부는 조치해야 풀림(기한이 지나면 주입기가 되돌림).
정답 기록(언제 · 무엇을 · 어떻게 끝났나)은 운영 VM chaos 디스크의 SQLite. 운영 AI는 운영 클러스터 토큰이 없어 못 읽고,
Prometheus는 chaos를 긁지 않는다. 주입 사실은 어디에도 알리지 않는다 (실험 뒤 #12에서 공개).
스위치(/etc/chaos-switch/state): on = 무작위 · manual = 무작위는 쉬고 손으로 넣은 것만 돌봄 · off(또는 없음) = 진행 중인 것도 끝냄.
실행: python -m banjjak.chaos [run | inject <종류> | status]
"""
import json
import os
import random
import sqlite3
import sys
import urllib.request
from contextlib import closing
from datetime import datetime, timedelta, timezone
from typing import Callable, NamedTuple

from banjjak.checklist import NS, _t

# 10분마다 이 확률로 넣는다. 기본은 "주 5회" 확률 (10-28 주 2.5회 → 5회: 11-20까지 약 9건으로는 종류별 탐지율을 말할 수 없다)
# 🧮 최소 간격이 지난 뒤 평균 대기 = 10분 / CHANCE → 기본값이면 약 33.6시간이라, 간격 12시간을 더해 실제로는 약 주 3.7회
# 고빈도 구간(10-28 본인 결정 B, AI 켜기 전까지)은 CronJob 환경 변수로 바꾼다: 이미지를 다시 만들지 않는다
CHANCE = float(os.environ.get("CHAOS_CHANCE", 5 / (7 * 24 * 6)))
MIN_GAP = timedelta(hours=float(os.environ.get("CHAOS_GAP_HOURS", 12)))
AFTER_DEPLOY = timedelta(minutes=30)
WEIGHTS = {                         # 종류 → 가중치. 조치해야 풀리는 둘(나쁜 배포 · 수집 멈춤)이 1/3
    "web_pod_kill": 1, "db_pod_kill": 1, "latency": 2, "errors": 2, "bad_deploy": 2, "collector_suspend": 2,
    "surge": 2}                     # 급증(#11)은 맨 끝: 부하 토큰이 없으면 빼고 고른다
NEEDS_ACTION = {"bad_deploy": timedelta(hours=2), "collector_suspend": timedelta(hours=6)}   # 이 기한이 지나면 되돌림

FAULT_CM = f"/api/v1{NS}/configmaps/web-fault"
WEB = f"/apis/apps/v1{NS}/deployments/web"
COLLECTOR = f"/apis/batch/v1{NS}/cronjobs/collector"
MERGE = "application/merge-patch+json"
JSON_PATCH = "application/json-patch+json"
CHANGE_CAUSE = "/metadata/annotations/kubernetes.io~1change-cause"
# 급증 부하는 VM 밖 GitHub Actions 러너가 넣는다. 비공개 레포: 실행 기록(언제 부하를 넣었나)이 정답이라 공개 Actions에 안 남긴다
LOAD = "/repos/gongbugi/banjjak-load/actions"

SCHEMA = """CREATE TABLE IF NOT EXISTS injections (
    id INTEGER PRIMARY KEY, kind TEXT NOT NULL, manual INTEGER NOT NULL, params TEXT NOT NULL,
    started REAL NOT NULL, until REAL NOT NULL, state TEXT NOT NULL, ended REAL, ended_by TEXT, note TEXT)"""
# state: active · ended / ended_by: time(저절로 끝) · fixed(조치로 풀림, note = 마지막 배포 기록) · reverted(기한 지나 되돌림) · switch_off


class App(NamedTuple):
    """앱 클러스터: 읽기 · 쓰기 · 지우기 (계정 chaos/chaos, 주입에 필요한 것만) + 부하 워크플로 (없으면 None)."""
    kube: Callable
    patch: Callable
    delete: Callable
    github: Callable | None = None


def connect(path):
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute(SCHEMA)
    return conn


def run_once(conn, now, app, dice, switch):
    """10분마다 한 번. 돌려주는 값은 한 줄 상태 (주입기 로그에만 남는다)."""
    active = conn.execute("SELECT * FROM injections WHERE state = 'active'").fetchone()
    if switch not in ("on", "manual"):
        if active:
            _end(conn, dict(active), now, app, "switch_off")
        return "off"
    if active:
        return _tend(conn, dict(active), now, app)
    if switch == "manual":
        return "manual"
    last = conn.execute("SELECT max(ended) FROM injections").fetchone()[0]
    if last and now.timestamp() - last < MIN_GAP.total_seconds():
        return "gap"
    if _recent_deploy(app, now):
        return "recent deploy"
    if dice.random() >= CHANCE:
        return "dice"
    kind = _pick(dice, {k: w for k, w in WEIGHTS.items() if k != "surge" or app.github})
    inject(conn, kind, now, app, dice)
    return f"injected {kind}"


def inject(conn, kind, now, app, dice, manual=False):
    """장애 하나를 넣고 정답을 기록한다. 손으로 넣을 때(낮 시험 주입)도 이것을 쓴다."""
    params, until = INJECT[kind](app, now, dice)
    conn.execute("INSERT INTO injections (kind, manual, params, started, until, state) VALUES (?, ?, ?, ?, ?, 'active')",
                 (kind, int(manual), json.dumps(params), now.timestamp(), until.timestamp()))
    conn.commit()
    return dict(conn.execute("SELECT * FROM injections ORDER BY id DESC LIMIT 1").fetchone())


def _pick(dice, weights):
    roll, total = dice.random() * sum(weights.values()), 0
    for kind, weight in weights.items():
        total += weight
        if roll < total:
            return kind
    return kind


def _recent_deploy(app, now):
    # 웹 배포가 막 움직였으면 (Progressing 시각, 롤백 · 재시작 포함) 넣지 않는다
    conditions = app.kube(WEB)["status"].get("conditions", [])
    at = next((c["lastUpdateTime"] for c in conditions if c["type"] == "Progressing"), None)
    return at is not None and now - _t(at) < AFTER_DEPLOY


def _tend(conn, inj, now, app):
    if inj["kind"] in NEEDS_ACTION:
        fixed = FIXED[inj["kind"]](app)
        if fixed is not None:
            return _finish(conn, inj, now, "fixed", fixed)
    if now.timestamp() >= inj["until"]:
        return _end(conn, inj, now, app, "reverted" if inj["kind"] in NEEDS_ACTION else "time")
    return f"active {inj['kind']}"


def _end(conn, inj, now, app, why):
    REVERT[inj["kind"]](app, now, json.loads(inj["params"]))
    return _finish(conn, inj, now, why, None)


def _finish(conn, inj, now, why, note):
    conn.execute("UPDATE injections SET state = 'ended', ended = ?, ended_by = ?, note = ? WHERE id = ?",
                 (now.timestamp(), why, note, inj["id"]))
    conn.commit()
    return f"ended {inj['kind']}"


# --- 종류별: 넣기 → (세기, 끝낼 때) / 되돌리기 / 고쳐졌나 ---

def _kill_newest(label):
    def run(app, now, dice):
        pods = app.kube(f"/api/v1{NS}/pods?labelSelector=app%3D{label}")["items"]
        newest = max(pods, key=lambda p: p["metadata"]["creationTimestamp"])["metadata"]["name"]
        app.delete(f"/api/v1{NS}/pods/{newest}")
        return {"pod": newest}, now + timedelta(minutes=10)   # k8s가 새로 띄운다. 기록만 10분 뒤 끝낸다
    return run


def _switch(key, low, high):
    """웹 안 스위치(ConfigMap web-fault, 화면 요청에만). 세기 low~high · 15~30분."""
    def run(app, now, dice):
        fault = {key: round(low + dice.random() * (high - low), 2)}
        app.patch(FAULT_CM, {"data": {"fault.json": json.dumps(fault)}}, MERGE)
        return fault, now + timedelta(minutes=15 + dice.random() * 15)
    return run


def _clear_switch(app, now, params):
    app.patch(FAULT_CM, {"data": {"fault.json": "{}"}}, MERGE)


def _deploy_cause(now):
    # 평범한 배포처럼 보인다. "chaos"를 넣으면 운영 AI 보고 · Slack으로 정답이 샌다
    return f"deploy config-{int(now.timestamp()) % 0xFFFFFF:06x} at {now:%Y-%m-%dT%H:%M:%SZ}"


def _bad_deploy(app, now, dice):
    fault = {"error_rate": round(0.2 + dice.random() * 0.3, 2)} if dice.random() < 0.5 \
        else {"delay": round(1.0 + dice.random() * 1.0, 2)}
    app.patch(WEB, [
        {"op": "add", "path": "/spec/template/spec/containers/0/env/-", "value": {"name": "FAULT", "value": json.dumps(fault)}},
        {"op": "add", "path": CHANGE_CAUSE, "value": _deploy_cause(now)},
    ], JSON_PATCH)
    return fault, now + NEEDS_ACTION["bad_deploy"]


def _fault_env(app):
    env = app.kube(WEB)["spec"]["template"]["spec"]["containers"][0].get("env", [])
    return next((i for i, e in enumerate(env) if e["name"] == "FAULT"), None)


def _bad_deploy_fixed(app):
    # FAULT가 빠졌으면 누가 고쳤다 (운영 AI 롤백 · 사람 · 다른 배포). 누구인지는 배포 기록으로 남긴다
    if _fault_env(app) is not None:
        return None
    return app.kube(WEB)["metadata"].get("annotations", {}).get("kubernetes.io/change-cause", "")


def _revert_bad_deploy(app, now, params):
    index = _fault_env(app)
    if index is not None:
        app.patch(WEB, [{"op": "remove", "path": f"/spec/template/spec/containers/0/env/{index}"},
                        {"op": "add", "path": CHANGE_CAUSE, "value": _deploy_cause(now)}], JSON_PATCH)


def _suspend(app, now, dice):
    app.patch(COLLECTOR, {"spec": {"suspend": True}}, MERGE)
    return {}, now + NEEDS_ACTION["collector_suspend"]


def _resumed(app):
    return None if app.kube(COLLECTOR)["spec"].get("suspend") else "collector resumed"


def _surge(app, now, dice):
    """사용자 급증 (#11 측정 조건): 초당 800건 15분 = 2차 공급 400 × 2. 요청 제한 320을 넘겨 약 60%가 429."""
    app.github("POST", f"{LOAD}/workflows/load.yml/dispatches",
               {"ref": "main", "inputs": {"ref": "main", "rates": "800", "seconds": "900"}})
    return {"rate": 800, "seconds": 900}, now + timedelta(minutes=30)   # 러너 준비 + 15분 + 여유. 저절로 끝난다


def _stop_load(app, now, params):
    # 끝낼 때 아직 도는 실행이 있으면 멈춘다 (러너가 늦게 떴거나 스위치를 껐을 때)
    for run in app.github("GET", f"{LOAD}/workflows/load.yml/runs?per_page=5")["workflow_runs"]:
        if run["status"] != "completed":
            app.github("POST", f"{LOAD}/runs/{run['id']}/cancel")


def _github(token):
    """GitHub API (토큰은 비공개 레포 banjjak-load의 Actions 읽기 · 쓰기만)."""
    def call(method, path, body=None):
        req = urllib.request.Request("https://api.github.com" + path, method=method,
                                     data=None if method == "GET" else json.dumps(body or {}).encode(),
                                     headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
        return json.loads(raw) if raw else None
    return call


INJECT = {"web_pod_kill": _kill_newest("web"), "db_pod_kill": _kill_newest("postgres"),
          "latency": _switch("delay", 1.0, 2.0), "errors": _switch("error_rate", 0.2, 0.5),
          "bad_deploy": _bad_deploy, "collector_suspend": _suspend, "surge": _surge}
REVERT = {"web_pod_kill": lambda *a: None, "db_pod_kill": lambda *a: None, "latency": _clear_switch,
          "errors": _clear_switch, "bad_deploy": _revert_bad_deploy,
          "collector_suspend": lambda app, now, params: app.patch(COLLECTOR, {"spec": {"suspend": False}}, MERGE),
          "surge": _stop_load}
FIXED = {"bad_deploy": _bad_deploy_fixed, "collector_suspend": _resumed}


def main(argv):
    import os
    from banjjak import appcluster
    from banjjak.heartbeat import ping
    token = os.environ.get("LOAD_TOKEN")
    app = App(appcluster.get, appcluster.patch, appcluster.delete, _github(token) if token else None)
    now = datetime.now(timezone.utc)
    try:
        with open("/etc/chaos-switch/state") as f:   # ConfigMap chaos/switch. 없으면 off
            switch = f.read().strip()
    except OSError:
        switch = "off"
    with closing(connect(os.environ.get("CHAOS_DB", "/data/chaos.db"))) as conn:
        if argv[:1] == ["inject"]:
            print("injected", inject(conn, argv[1], now, app, random, manual=True))
        elif argv[:1] == ["status"]:
            for r in conn.execute("SELECT * FROM injections ORDER BY id"):
                print(dict(r))
        else:
            # 설정도 남긴다: 고빈도 구간이 언제부터 언제까지였는지 로그로도 가린다
            print(f"switch={switch} · {run_once(conn, now, app, random, switch)} · gap={MIN_GAP} · chance={CHANCE:.4f}"
                  f" · surge={'on' if token else 'off'}", flush=True)
    ping(os.environ.get("HEARTBEAT_URL"))   # 한 바퀴를 마쳤다


if __name__ == "__main__":
    main(sys.argv[1:])
