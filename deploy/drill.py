"""로컬 시험장 (k3d 전용): 장애를 종류마다 정해진 횟수만큼 넣고, 경보 → 운영 AI 진단 → 끝까지 지켜보며 정답을 남긴다.
#20 진단기를 같은 경보로 채점할 재료 (운영 AI가 경보마다 녹화한 테이프 + 이 정답 기록). Azure 실험(정답 모름)과는 따로 센다.

사용 (레포 루트, Git Bash): app/.venv/Scripts/python deploy/drill.py [회차=3] [종류 ...]
정답 기록: drill-out/answers.jsonl (git에 안 올린다)
- 주입은 deploy/chaos.sh (주입기 스위치 manual). 급증은 주입기에 아직 없어 여기서 PC 부하로 넣는다 (초당 800건 6분)
- 수집 멈춤은 경보가 2시간 뒤라 로컬 DB 수집 시각을 3시간 앞당겨 바로 울리게 하고, 재개되면 수집을 한 번 당겨 돌린다
- Docker Desktop이 꺼져 있으면 다시 켠다 (10-28 저녁에도 저절로 꺼졌다)
"""
import ast
import http.client
import json
import os
import subprocess
import sys
import threading
import time
import urllib.parse
from datetime import datetime

CTX = "k3d-banjjak"
KINDS = ["web_pod_kill", "db_pod_kill", "latency", "errors", "bad_deploy", "collector_suspend", "surge"]
OUT = os.path.join("drill-out", "answers.jsonl")
# 파이썬에서 그냥 "bash"를 부르면 Windows PATH가 WSL bash(system32)를 먼저 찾는다 → gh · kubectl 설정이 없다
BASH = "C:/Program Files/Git/bin/bash.exe"


def sh(*args, timeout=180):
    r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", timeout=timeout)
    return r.stdout.strip() if r.returncode == 0 else ""


def kube(*args, timeout=180):
    return sh("kubectl", "--context", CTX, *args, timeout=timeout)


def log(msg):
    print(f"{datetime.now():%m-%d %H:%M:%S} {msg}", flush=True)


def docker_up():
    """Docker Desktop · k3d API가 답할 때까지 (꺼져 있으면 켠다)."""
    for i in range(40):
        if sh("docker", "info", timeout=30) and kube("get", "nodes", timeout=30):
            return
        if i == 0:
            log("Docker가 꺼져 있다 → 켠다")
            subprocess.run(["powershell.exe", "-NoProfile", "-Command",
                            r'Start-Process "C:\Program Files\Docker\Docker\Docker Desktop.exe"'])
        time.sleep(15)
    raise SystemExit("Docker · k3d가 10분 안에 안 올라왔다")


def prom(query):
    out = kube("-n", "observe", "exec", "deploy/prometheus", "--", "wget", "-qO-",
               "http://localhost:9090/api/v1/query?" + urllib.parse.urlencode({"query": query}))
    try:
        return json.loads(out)["data"]["result"]
    except (ValueError, KeyError):
        return []


def firing():
    return sorted({r["metric"]["alertname"] for r in prom('ALERTS{alertstate="firing"}')} - {"Watchdog"})


def last_record():
    code = ("import os,sqlite3,json;c=sqlite3.connect(os.environ['OPSAI_DB']);"
            "r=c.execute('select id,alertname,cause,action,tier,state,result from records order by id desc limit 1').fetchone();"
            "print(json.dumps(r))")
    out = kube("-n", "ops-ai", "exec", "deploy/ops-ai", "--", "python", "-c", code)
    try:
        return json.loads(out)
    except ValueError:
        return None


def injection():
    """주입기 정답 기록의 마지막 줄 (로컬은 시험장이라 봐도 된다)."""
    out = sh(BASH, "deploy/chaos.sh", CTX, "status", timeout=240)
    rows = [l for l in out.splitlines() if l.startswith("{")]
    return ast.literal_eval(rows[-1]) if rows else None   # status는 dict(row)를 print한다


def psql(sql):
    return kube("-n", "banjjak", "exec", "deploy/postgres", "--", "sh", "-c",
                f'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "{sql}"')


def surge(seconds=360, rate=800, threads=8):
    """초당 rate건을 seconds초 (연결을 다시 쓴다: 요청마다 새 연결이면 Windows 임시 포트가 바닥난다)."""
    end, gap = time.time() + seconds, threads / rate

    def run():
        conn, nxt = http.client.HTTPConnection("localhost", 8080, timeout=10), time.time()
        while time.time() < end:
            try:
                conn.request("GET", "/api/trending"); conn.getresponse().read()
            except Exception:
                conn.close(); conn = http.client.HTTPConnection("localhost", 8080, timeout=10)
            nxt += gap
            time.sleep(max(0.0, nxt - time.time()))
    ts = [threading.Thread(target=run, daemon=True) for _ in range(threads)]
    [t.start() for t in ts]
    return ts


def wait(cond, minutes, every=20):
    end = time.time() + minutes * 60
    while time.time() < end:
        got = cond()
        if got:
            return got
        time.sleep(every)
    return None


def scenario(rnd, kind):
    docker_up()
    wait(lambda: not firing() or None, 20)   # 앞 장애의 경보가 다 풀린 뒤
    before = (last_record() or [0])[0]
    a = {"round": rnd, "kind": kind, "injected_at": datetime.now().isoformat(timespec="seconds")}
    log(f"{rnd}회차 {kind} 주입")
    loader = None
    if kind == "surge":
        loader = surge()
    else:
        print(sh(BASH, "deploy/chaos.sh", CTX, "inject", kind, timeout=240))
        if kind == "collector_suspend":
            psql("UPDATE star_snapshots SET captured_at = captured_at - interval '3 hours'")
            a["note"] = "수집 시각 3시간 앞당김 (경보를 바로 울리게)"
    # 경보: 파드 종료는 짧아서 안 울릴 수도 있다 (그것도 정답 기록이다)
    alerts = wait(lambda: firing() or None, 10 if kind.endswith("pod_kill") else 25)
    a["alerts"], a["alert_at"] = alerts or [], datetime.now().isoformat(timespec="seconds") if alerts else None
    rec = wait(lambda: (r := last_record()) and r[0] > before and r, 5) if alerts else None
    if rec:
        a["record"] = dict(zip(["id", "alertname", "cause", "action", "tier", "state", "result"], rec))
    log(f"  경보 {a['alerts']} · 진단 {a.get('record')}")
    # 끝: 급증은 부하가 끝나면, 나머지는 주입기가 끝났다고 적을 때까지 (조치로 풀림 · 기한 · 저절로)
    if loader:
        [t.join() for t in loader]
        a["ended_by"] = "부하 끝"
    else:
        if kind == "collector_suspend":   # 운영 AI가 재개하면 수집을 한 번 당겨 돌린다 (다음 정시까지 안 기다림)
            if wait(lambda: kube("-n", "banjjak", "get", "cronjob", "collector", "-o",
                                 "jsonpath={.spec.suspend}") == "false" or None, 15):
                kube("-n", "banjjak", "create", "job", f"collector-drill-{int(time.time())}", "--from=cronjob/collector")
        done = wait(lambda: (i := injection()) and i.get("state") == "ended" and i, 45, every=120)
        if not done:   # 끝나지 않으면 스위치를 내려 되돌리고 다시 manual
            kube("-n", "chaos", "patch", "configmap", "switch", "-p", '{"data":{"state":"off"}}')
            done = wait(lambda: (i := injection()) and i.get("state") == "ended" and i, 15, every=60)
            kube("-n", "chaos", "patch", "configmap", "switch", "-p", '{"data":{"state":"manual"}}')
        a["injection"] = done
        a["ended_by"] = (done or {}).get("ended_by")
    a["ended_at"] = datetime.now().isoformat(timespec="seconds")
    os.makedirs("drill-out", exist_ok=True)
    with open(OUT, "a", encoding="utf-8") as f:
        f.write(json.dumps(a, ensure_ascii=False) + "\n")
    log(f"  끝 ({a['ended_by']}) → 정답 기록")
    time.sleep(180)


if __name__ == "__main__":
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    kinds = sys.argv[2:] or KINDS
    kube("-n", "chaos", "patch", "configmap", "switch", "-p", '{"data":{"state":"manual"}}')
    try:
        for rnd in range(1, rounds + 1):
            for kind in kinds:   # 회차마다 종류를 한 바퀴 → 중간에 멈춰도 종류가 고르게 남는다
                scenario(rnd, kind)
    finally:
        kube("-n", "chaos", "patch", "configmap", "switch", "-p", '{"data":{"state":"off"}}')
        log("시험장 끝 (주입기 스위치 off)")
