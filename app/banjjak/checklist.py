"""운영 AI 진단기 checklist-v1: 경보 종류별 점검표가 읽기 전용 도구를 정해진 순서로 불러 증거를 모은다. LLM 없음.

바꿔 끼우는 경계: diagnose(kind, kube, prom, now) -> {"diagnoser", "cause", "verdict", "evidence"}
  cause는 CAUSES 안의 코드 하나 또는 None(판단 못 함 → 사람에게). #8의 조치 표 · #12 채점이 이 코드를 쓴다.
  #20이 같은 모양의 Sonnet 진단기를 끼우고(원인은 같은 목록에서만), 이 점검표는 대조군 · LLM 장애 때 대체 경로로 남는다.
  kube(path): 앱 클러스터 API 읽기 (JSON이면 dict, 로그면 str). prom(query): 운영 Prometheus 즉시 값 (없으면 None).
"""
import re
from datetime import datetime, timedelta

NAME = "checklist-v1"
NS = "/namespaces/banjjak"
STALE = timedelta(hours=2)    # 데이터 신선도 목표 (스펙 #1)
RECENT = timedelta(minutes=30)   # 경보(for 5분)보다 넉넉히. 가정: 기준선 구간의 배포 → 경보 간격을 보고 다시 본다

CAUSES = {
    "app_unreachable": "앱 클러스터 API가 아무 답도 하지 않는다 (앱 VM이 멈췄거나 운영 AI 토큰이 없다)",
    "db_down": "DB 파드가 준비되지 않았다",
    "web_down": "준비된 웹 파드가 없다",
    "recent_deploy": "최근 배포 직후다",
    "traffic_surge": "요청이 1시간 전보다 크게 늘었다 (웹 파드는 HPA가 최대 2까지 늘린다)",
    "collector_suspended": "수집기 CronJob이 멈춰 있다 (suspend)",
    "collector_failing": "마지막 수집기 Job이 실패했다",
    "collector_late": "수집기가 2시간 넘게 성공하지 못했다",
    "metrics_not_arriving": "앱 클러스터 지표가 운영 VM에 오지 않는다 (에이전트 · 보내기 경로)",
    "web_metrics_down": "웹 파드는 준비됐는데 지표 주소(9100)를 긁지 못한다",
    "analyzer_suspended": "분석기 CronJob이 멈춰 있다 (suspend)",
    "analyzer_failing": "마지막 분석기 Job이 실패했다",
    "analyzer_late": "분석기가 2시간 넘게 성공하지 못했다",
}


def diagnose(kind, kube, prom, now):
    check = CHECKLISTS.get(kind)
    if check is None:
        return {"diagnoser": NAME, "cause": None, "verdict": None,
                "evidence": [{"look": "점검표", "saw": "이 경보의 점검표가 없다"}]}
    answered = []

    def kube_(path):
        body = kube(path)
        answered.append(path)
        return body

    evidence = []
    cause = check(evidence, kube_, prom, now)
    if not answered:   # 어느 점검표든 앱 클러스터가 한 번도 답하지 않았으면 그것이 첫 증상이다
        cause = "app_unreachable"
    return {"diagnoser": NAME, "cause": cause, "verdict": CAUSES.get(cause), "evidence": evidence}


def _see(evidence, look, fn):
    """한 번 보고 기록한다. 못 읽었으면 지어내지 않고 못 읽었다고 남긴다."""
    try:
        facts, saw = fn()
    except Exception as e:
        evidence.append({"look": look, "saw": f"못 읽음 ({type(e).__name__}: {e})"})
        return None
    evidence.append({"look": look, "saw": saw})
    return facts


def _t(s):
    return datetime.fromisoformat(s)   # k8s 시각 "2026-10-21T13:00:00Z"


def _ago(s, now):
    if not s:
        return "기록 없음"
    m = int((now - _t(s)).total_seconds() // 60)
    return f"{m // 60}시간 {m % 60}분 전" if m >= 60 else f"{m}분 전"


def _cronjob(kube, name, now):
    c = kube(f"/apis/batch/v1{NS}/cronjobs/{name}")
    suspend, ok_at = c["spec"].get("suspend", False), c.get("status", {}).get("lastSuccessfulTime")
    late = not ok_at or now - _t(ok_at) > STALE
    saw = f"{'멈춤 (suspend)' if suspend else '켜져 있음'} · 마지막 성공 {_ago(ok_at, now)}"
    return {"suspend": suspend, "late": late}, saw


def _last_job(kube, cronjob):
    jobs = [j for j in kube(f"/apis/batch/v1{NS}/jobs")["items"]
            if any(o["kind"] == "CronJob" and o["name"] == cronjob for o in j["metadata"].get("ownerReferences", []))]
    if not jobs:
        return None, "Job 기록 없음"
    j = max(jobs, key=lambda j: j["metadata"]["creationTimestamp"])
    failed = any(c["type"] == "Failed" and c["status"] == "True" for c in j["status"].get("conditions", []))
    state = "실패" if failed else "성공" if j["status"].get("succeeded") else "도는 중"
    return {"name": j["metadata"]["name"], "failed": failed}, f"{j['metadata']['name']} {state}"


# 파이썬 예외 줄 모양: "모듈.클래스: 메시지" 또는 "...Error: 메시지" (들여쓴 스택 · [SQL ...] 줄은 아니다)
EXCEPTION_LINE = re.compile(r"^(\w+\.)+\w+: |^\w*(Error|Exception)\b")
LINE_MAX = 200


def _job_log(kube, job):
    pods = kube(f"/api/v1{NS}/pods?labelSelector=job-name%3D{job}")["items"]
    last = max(pods, key=lambda p: p["metadata"]["creationTimestamp"])   # 다시 시도했으면 마지막 시도
    lines = [l for l in kube(f"/api/v1{NS}/pods/{last['metadata']['name']}/log?tailLines=50").splitlines() if l.strip()]
    # 오류 줄이 있으면 그것만 (끝 줄은 SQL 문장 · 매개변수일 때가 많다), 없으면 끝 3줄
    picked = list(dict.fromkeys(l for l in lines if EXCEPTION_LINE.match(l)))[-2:] or lines[-3:]
    cut = [l if len(l) <= LINE_MAX else l[:LINE_MAX] + "…" for l in picked]
    return None, " | ".join(cut) or "로그 없음"


def _pods(kube, app, now):
    items = kube(f"/api/v1{NS}/pods?labelSelector=app%3D{app}")["items"]
    ready = sum(any(c["type"] == "Ready" and c["status"] == "True" for c in p["status"].get("conditions", []))
                for p in items)
    containers = [c for p in items for c in p["status"].get("containerStatuses", [])]
    restarts = sum(c.get("restartCount", 0) for c in containers)
    # 지금 기다리는 이유(CrashLoopBackOff 등)와 최근에 죽은 이유(OOMKilled 등). 오래전에 죽은 기록은 지난 일이다
    why = {c.get("state", {}).get("waiting", {}).get("reason") for c in containers}
    why |= {t.get("reason") for c in containers if (t := c.get("lastState", {}).get("terminated"))
            and t.get("finishedAt") and now - _t(t["finishedAt"]) < RECENT}
    saw = " · ".join([f"{ready}/{len(items)} 준비"] + sorted(filter(None, why)) + ([f"재시작 {restarts}회"] if restarts else []))
    return {"ready": ready}, saw


def _rollout(kube, name, now):
    d = kube(f"/apis/apps/v1{NS}/deployments/{name}")
    # Progressing 조건의 시각 = 마지막 배포(롤백 · rollout restart 포함)가 움직인 때. 파드가 죽었다 살아나는 것으로는 안 바뀐다
    at = next(c["lastUpdateTime"] for c in d["status"]["conditions"] if c["type"] == "Progressing")
    cause = d["metadata"].get("annotations", {}).get("kubernetes.io/change-cause", "배포 기록 없음")
    return {"recent": now - _t(at) < RECENT}, f"{_ago(at, now)} · {cause}"


def _web(evidence, kube, now):
    """웹 쪽 공통: 최근 배포 · 웹 파드 · DB 파드. 뿌리부터: DB가 죽으면 웹 준비 검사(/readyz)도 떨어진다."""
    dep = _see(evidence, "웹 최근 배포", lambda: _rollout(kube, "web", now))
    web = _see(evidence, "웹 파드", lambda: _pods(kube, "web", now))
    db = _see(evidence, "DB 파드", lambda: _pods(kube, "postgres", now))
    if db and not db["ready"]:
        return "db_down"
    if web and not web["ready"]:
        return "web_down"
    if dep and dep["recent"]:
        return "recent_deploy"
    return None


# 사용자 화면만 센다 (경보 규칙과 같은 범위)
REQS = 'sum(rate(banjjak_http_request_duration_seconds_count{{route=~"/|/api/trending"}}[5m]{}))'
# 입구(Traefik)에서 센다 (#11): 요청 제한이 거절한 요청은 웹에 닿지 않아 웹 지표에는 안 보인다
ENTRANCE = 'sum(rate(traefik_entrypoint_requests_total{{entrypoint="web"}}[5m]{}))'
REJECTED = ('sum(rate(traefik_entrypoint_requests_total{entrypoint="web",code="429"}[5m]))'
            ' / sum(rate(traefik_entrypoint_requests_total{entrypoint="web"}[5m]))')


def _surge(prom, reqs=REQS):
    now, before = prom(reqs.format("")) or 0.0, prom(reqs.format(" offset 1h")) or 0.0
    # 가정: 1시간 전의 3배 이상이면서 초당 1건 이상. 평소 요청이 거의 0이라 배수만 보면 몇 건에도 급증이 된다
    return {"surge": now >= 1 and now >= 3 * before}, f"{now:.2f} / {before:.2f} req/s"


def _web_traffic(evidence, kube, prom, now):
    """공개 요청에 움직이는 웹 경보. 급증이 보이면 배포 나이만으로 되돌리지 않는다: 부하가 멀쩡한 배포를 롤백하면 안 된다."""
    cause = _web(evidence, kube, now)
    traffic = _see(evidence, "요청량 (지금 / 1시간 전)", lambda: _surge(prom))
    if cause in (None, "recent_deploy") and traffic and traffic["surge"]:
        return "traffic_surge"
    return cause


def _rejected(evidence, kube, prom, now):
    """입구 요청 제한(초당 320건)이 거절하고 있다. 급증이면 보고만 한다: 웹 파드는 HPA가 이미 늘린다 (#11)."""
    traffic = _see(evidence, "입구 요청량 (지금 / 1시간 전)", lambda: _surge(prom, ENTRANCE))
    _see(evidence, "입구 거절 비율 (429)", lambda: ({}, f"{prom(REJECTED):.0%}"))
    _see(evidence, "웹 HPA", lambda: _hpa(kube))
    _see(evidence, "웹 파드", lambda: _pods(kube, "web", now))
    return "traffic_surge" if traffic and traffic["surge"] else None


def _hpa(kube):
    h = kube(f"/apis/autoscaling/v2{NS}/horizontalpodautoscalers/web")
    st, spec = h.get("status", {}), h["spec"]
    cpu = next((m["resource"]["current"].get("averageUtilization") for m in st.get("currentMetrics") or []
                if m.get("type") == "Resource"), None)
    saw = f"{st.get('currentReplicas', 0)}/{spec['maxReplicas']} 파드 (최소 {spec.get('minReplicas', 1)})"
    return {"current": st.get("currentReplicas", 0), "max": spec["maxReplicas"]}, saw + (f" · CPU {cpu}%" if cpu is not None else "")


def _arriving(prom):
    # 앱 에이전트가 보내는 긁기 결과 (external_labels cluster=app). 에이전트 자신도 대상이라 웹은 따로 센다
    n = prom('count(up{cluster="app"})')
    if not n:
        return {"n": 0, "web_up": 0}, "앱 클러스터 지표가 하나도 없다"
    up, total = prom('sum(up{cluster="app",app="web"})') or 0, prom('count(up{cluster="app",app="web"})') or 0
    return {"n": n, "web_up": up}, f"긁기 대상 {n:.0f}개 · 웹 {up:.0f}/{total:.0f} 응답"


def _data_age_missing(evidence, kube, prom, now):
    """데이터 나이 지표 자체가 없다: 웹이 못 재는가(웹 · DB), 재도 못 오는가(지표 경로), 배포가 지표를 잃었나."""
    dep = _see(evidence, "웹 최근 배포", lambda: _rollout(kube, "web", now))
    web = _see(evidence, "웹 파드", lambda: _pods(kube, "web", now))
    db = _see(evidence, "DB 파드", lambda: _pods(kube, "postgres", now))
    sent = _see(evidence, "운영 VM에 오는 앱 지표", lambda: _arriving(prom))
    if db and not db["ready"]:   # 웹은 DB를 못 읽으면 지표를 아예 내지 않는다
        return "db_down"
    if web and not web["ready"]:
        return "web_down"
    if sent and not sent["n"]:
        return "metrics_not_arriving"
    if sent and not sent["web_up"]:   # 웹은 준비됐는데 지표 주소만 못 긁는다: 방금 배포했으면 그 배포가 뿌리
        return "recent_deploy" if dep and dep["recent"] else "web_metrics_down"
    return None


def _batch(evidence, kube, now, cronjob, label):
    """CronJob 하나(수집기 · 분석기)와 DB를 본다. 원인 코드는 <cronjob>_suspended / _failing / _late."""
    cron = _see(evidence, f"{label} CronJob", lambda: _cronjob(kube, cronjob, now))
    job = _see(evidence, f"마지막 {label} Job", lambda: _last_job(kube, cronjob))
    if job and job["failed"]:
        _see(evidence, "실패한 Job 로그", lambda: _job_log(kube, job["name"]))
    db = _see(evidence, "DB 파드", lambda: _pods(kube, "postgres", now))
    if cron and cron["suspend"]:
        return f"{cronjob}_suspended"
    if db and not db["ready"]:   # DB가 죽으면 Job도 실패한다 → 뿌리를 먼저
        return "db_down"
    if job and job["failed"]:
        return f"{cronjob}_failing"
    if cron and cron["late"]:
        return f"{cronjob}_late"
    return None


CHECKLISTS = {
    "DataStale": lambda ev, kube, prom, now: _batch(ev, kube, now, "collector", "수집기"),
    "WebErrorRateHigh": _web_traffic,
    "WebLatencyP95High": _web_traffic,
    "DataAgeMissing": _data_age_missing,
    "AnalysisSuccessLow": lambda ev, kube, prom, now: _batch(ev, kube, now, "analyzer", "분석기"),
    # #11 입구 경보: 거절(429)은 급증인지 보고, 입구 502 · 503(웹이 응답 못 함)은 웹 5xx와 같은 순서로 뿌리를 찾는다
    "WebRequestsRejected": _rejected,
    "EntranceErrorsHigh": lambda ev, kube, prom, now: _web(ev, kube, now),
}


# --- 녹화 · 재생 (#20 준비): 경보 순간의 원래 응답을 남겨, 나중에 LLM 진단기를 같은 상태 위에서 다시 돌린다 ---
# 진단기를 바꿔도 같은 경보로 비교할 수 있다 (LLM을 켜기 전 경보 · 프롬프트를 바꾼 뒤 회귀 시험)

FAILED = "__failed__"


def recorder(kube, prom):
    """읽기를 그대로 통과시키며 응답을 테이프에 남긴다. 같은 읽기를 또 하면 테이프 것을 준다 (한 경보 = 한 순간)."""
    tape = {"kube": {}, "prom": {}}

    def taped(part, read):
        def read_(key):
            if key not in tape[part]:
                try:
                    tape[part][key] = read(key)
                except Exception as e:
                    tape[part][key] = {FAILED: [type(e).__name__, str(e)]}
            return _play(tape[part][key])
        return read_
    return tape, taped("kube", kube), taped("prom", prom)


def replay(tape):
    """테이프 → (kube, prom). 테이프에 없는 읽기는 지어내지 않고 실패한다 (진짜 API의 404처럼)."""
    def play(part):
        def read(key):
            if key not in tape[part]:
                raise LookupError(f"녹화에 없는 읽기 {key}")
            return _play(tape[part][key])
        return read
    return play("kube"), play("prom")


def _play(body):
    if isinstance(body, dict) and FAILED in body:
        name, message = body[FAILED]
        raise type(name, (Exception,), {})(message)   # 원래 예외 이름 그대로 → 증거 문장("못 읽음 (…)")이 녹화 때와 같다
    return body


def sweep(kube, prom, now):
    """모든 점검표의 읽기를 한 번씩 (LLM은 경보와 상관없이 어느 도구든 고를 수 있다) + 마지막 Job 로그는 성공해도."""
    for check in CHECKLISTS.values():
        check([], kube, prom, now)
    for cronjob in ("collector", "analyzer"):
        _see([], "", lambda: _job_log(kube, _last_job(kube, cronjob)[0]["name"]))
