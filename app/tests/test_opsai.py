import json
from contextlib import closing
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from banjjak.actions import Hands, connect, decide, tick
from banjjak.opsai import app, handle

client = TestClient(app)
NOW = datetime(2026, 10, 21, 14, 0, tzinfo=timezone.utc)


def firing(alertname):
    return {"status": "firing", "labels": {"alertname": alertname}, "annotations": {"summary": "..."},
            "startsAt": "2026-10-21T13:50:00Z", "fingerprint": "f1"}


def fake_kube(responses):
    """가짜 앱 클러스터 API. 경로 → 응답(JSON이면 dict, 로그면 str). 모르는 경로는 실패한다 (진짜 API의 404처럼)."""
    def kube(path):
        if path not in responses:
            raise LookupError(f"없는 경로 {path}")
        return responses[path]
    return kube


def fake_prom(values=None):
    return lambda query: (values or {}).get(query)


NS = "/namespaces/banjjak"


def pod(name, ready=True, waiting=None):
    state = {"waiting": {"reason": waiting}} if waiting else {"running": {}}
    return {"metadata": {"name": name, "creationTimestamp": "2026-10-21T13:00:05Z"},
            "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
                       "containerStatuses": [{"name": "c", "restartCount": 0, "state": state}]}}


def job(name, owner, ok, at):
    status = {"succeeded": 1} if ok else {"failed": 2, "conditions": [{"type": "Failed", "status": "True"}]}
    return {"metadata": {"name": name, "creationTimestamp": at, "ownerReferences": [{"kind": "CronJob", "name": owner}]},
            "status": status}


def rs(revision, image):
    """웹 배포의 판 하나 (ReplicaSet). 롤백은 이 template으로 되돌린다."""
    return {"metadata": {"name": f"web-h{revision}", "annotations": {"deployment.kubernetes.io/revision": str(revision)}},
            "spec": {"template": {"metadata": {"labels": {"app": "web", "pod-template-hash": f"h{revision}"}},
                                  "spec": {"containers": [{"name": "web", "image": image}]}}}}


def cluster(*, suspend=False, collect_ok=True, collect_log="", last_success="2026-10-21T11:00:20Z", db_ready=True,
            deployed="2026-10-20T09:00:00Z", web_ready=True, web_waiting=None, analyze_ok=True,
            change_cause=None):
    """앱 클러스터 하나: 웹 · 수집기 · DB (k8s API가 실제로 돌려주는 모양에서 쓰는 칸만)."""
    return {
        f"/apis/apps/v1{NS}/deployments/web": {
            "metadata": {"annotations": {
                "kubernetes.io/change-cause": change_cause or "deploy a169f9e04c1b at " + deployed,
                "deployment.kubernetes.io/revision": "5"}},
            "status": {"conditions": [
                {"type": "Available", "status": "True", "lastUpdateTime": "2026-10-21T13:59:00Z"},
                {"type": "Progressing", "status": "True", "reason": "NewReplicaSetAvailable", "lastUpdateTime": deployed},
            ]}},
        f"/api/v1{NS}/pods?labelSelector=app%3Dweb": {"items": [pod("web-5d9c-abc", ready=web_ready, waiting=web_waiting)]},
        f"/apis/apps/v1{NS}/replicasets?labelSelector=app%3Dweb": {"items": [
            rs(3, "ghcr.io/gongbugi/banjjak:older"), rs(5, "ghcr.io/gongbugi/banjjak:bad"), rs(4, "ghcr.io/gongbugi/banjjak:good")]},
        f"/apis/batch/v1{NS}/cronjobs/collector": {
            "spec": {"suspend": suspend},
            "status": {"lastScheduleTime": "2026-10-21T13:00:00Z", "lastSuccessfulTime": last_success}},
        f"/apis/batch/v1{NS}/cronjobs/analyzer": {
            "spec": {}, "status": {"lastScheduleTime": "2026-10-21T13:20:00Z", "lastSuccessfulTime": "2026-10-21T12:20:40Z"}},
        f"/api/v1{NS}/pods?labelSelector=job-name%3Danalyzer-9": {"items": [pod("analyzer-9-q2w", ready=False)]},
        f"/api/v1{NS}/pods/analyzer-9-q2w/log?tailLines=50": "psycopg.errors.UndefinedColumn: column \"method\" does not exist\n",
        f"/apis/batch/v1{NS}/jobs": {"items": [
            job("analyzer-9", "analyzer", analyze_ok, "2026-10-21T13:20:00Z"),
            job("collector-2", "collector", collect_ok, "2026-10-21T13:00:00Z"),
            job("collector-1", "collector", True, "2026-10-21T12:00:00Z"),
        ]},
        f"/api/v1{NS}/pods?labelSelector=job-name%3Dcollector-2": {"items": [pod("collector-2-x7k", ready=False)]},
        f"/api/v1{NS}/pods/collector-2-x7k/log?tailLines=50": collect_log,
        f"/api/v1{NS}/pods?labelSelector=app%3Dpostgres": {"items": [pod("postgres-0", ready=db_ready)]},
    }


class Ops:
    """운영 AI 하나: 진짜 SQLite 파일 + 가짜 앱 클러스터(읽기 · 쓰기) · Slack. 쓰기 요청은 patched에 쌓인다."""

    def __init__(self, db, kube=None, prom=None, mode="execute", cluster="azure"):
        self.db, self.prom, self.sent, self.patched = db, prom or fake_prom(), [], []
        self.hands = Hands(kube or fake_kube({}), lambda path, body, ctype: self.patched.append((path, body)),
                           self.sent.append, mode, cluster)

    def alert(self, alertname, at=NOW):
        with closing(connect(self.db)) as conn:
            return handle(firing(alertname), self.prom, at, conn, self.hands)

    def tick(self, at):
        with closing(connect(self.db)) as conn:   # 매번 새 연결 = 파드가 바뀌어도 DB에서 이어받는다
            tick(conn, at, self.hands)

    def decide(self, record_id, approve, at):
        with closing(connect(self.db)) as conn:
            decide(conn, record_id, approve, at, self.hands, by="본인")

    def states(self):
        with closing(connect(self.db)) as conn:
            return [(r["action"], r["tier"], r["state"]) for r in conn.execute("SELECT * FROM records ORDER BY id")]


def run(alertname, kube=None, prom=None, cluster="azure"):
    """알림 1건을 처리하고 (기록, Slack에 보낸 글)을 돌려준다."""
    ops = Ops(":memory:", kube, prom, cluster=cluster)
    return ops.alert(alertname), ops.sent


# 로컬 k3d와 Azure가 같은 Slack 채널에 보낸다 (2026-10-22 로컬 경보가 Azure 장애처럼 보였다)
def test_report_names_the_cluster_it_came_from():
    record, sent = run("DataStale", fake_kube(cluster(suspend=True)), cluster="local-k3d")
    assert record["cluster"] == "local-k3d"
    assert sent[0].splitlines()[0].startswith("🩺 운영 AI 진단 [local-k3d] · DataStale")


def test_unknown_alert_is_handed_to_a_human():
    record, sent = run("SomethingNew")
    assert (record["alertname"], record["severity"], record["verdict"]) == ("SomethingNew", "unknown", None)
    assert record["diagnoser"] == "checklist-v1"
    assert len(sent) == 1 and "판단 못 함" in sent[0]


def test_data_stale_finds_a_suspended_collector():
    record, sent = run("DataStale", fake_kube(cluster(suspend=True)))
    assert (record["sli"], record["severity"]) == ("데이터 신선도 2시간", "warning")
    assert record["cause"] == "collector_suspended"
    # 점검표가 정해진 순서로 본다
    assert [e["look"] for e in record["evidence"]] == ["수집기 CronJob", "마지막 수집기 Job", "DB 파드"]
    assert "멈춤" in record["evidence"][0]["saw"] and "2시간 59분 전" in record["evidence"][0]["saw"]
    assert "수집기 CronJob이 멈춰 있다" in sent[0]


def test_data_stale_shows_why_the_last_collect_failed():
    log = "Traceback (most recent call last):\nurllib.error.HTTPError: HTTP Error 401: Unauthorized\n"
    record, sent = run("DataStale", fake_kube(cluster(collect_ok=False, collect_log=log)))
    assert record["cause"] == "collector_failing"
    assert [e["look"] for e in record["evidence"]] == ["수집기 CronJob", "마지막 수집기 Job", "실패한 Job 로그", "DB 파드"]
    assert "collector-2 실패" in sent[0] and "HTTP Error 401: Unauthorized" in sent[0]


# 2026-10-22 로컬 수집기의 실제 로그 모양 (85줄 중 끝부분, 긴 줄은 줄임). 끝 5줄만 보면 SQL 문장 · 매개변수뿐이다
SCHEMA_LOG = "\n".join([
    "Traceback (most recent call last):",
    '  File "/usr/local/lib/python3.13/site-packages/psycopg/cursor.py", line 117, in execute',
    "    raise ex.with_traceback(None)",
    'psycopg.errors.UndefinedColumn: column "topics" of relation "repos" does not exist',
    "LINE 1: ... (id, full_name, html_url, description, language, topics, cr...",
    "                                                             ^",
    "",
    "The above exception was the direct cause of the following exception:",
    "",
    "Traceback (most recent call last):",
    '  File "/usr/local/lib/python3.13/site-packages/psycopg/cursor.py", line 117, in execute',
    "    raise ex.with_traceback(None)",
    'sqlalchemy.exc.ProgrammingError: (psycopg.errors.UndefinedColumn) column "topics" of relation "repos" does not exist',
    "LINE 1: ... (id, full_name, html_url, description, language, topics, cr...",
    "                                                             ^",
    "[SQL: INSERT INTO repos (id, full_name, html_url, description, language, topics, created_at, last_seen_at) VALUES "
    + "(%(id)s::BIGINT, %(full_name)s::VARCHAR) " * 30 + "]",
    "[parameters: {'id': 1398080271, 'full_name': 'storytold/photocraft', " + "'topics': ['art', 'rust'], " * 40 + "}]",
    "(Background on this error at: https://sqlalche.me/e/21/f405)",
]) + "\n"


def test_failed_job_evidence_shows_the_error_lines_not_the_sql():
    record, sent = run("DataStale", fake_kube(cluster(collect_ok=False, collect_log=SCHEMA_LOG)))
    saw = record["evidence"][2]["saw"]
    assert saw == ('psycopg.errors.UndefinedColumn: column "topics" of relation "repos" does not exist | '
                   'sqlalchemy.exc.ProgrammingError: (psycopg.errors.UndefinedColumn) column "topics" of relation "repos" does not exist')
    assert "[SQL" not in sent[0] and "[parameters" not in sent[0]


def test_long_log_lines_are_cut():
    record, _ = run("DataStale", fake_kube(cluster(collect_ok=False, collect_log="RuntimeError: " + "x" * 500 + "\n")))
    saw = record["evidence"][2]["saw"]
    assert len(saw) == 201 and saw.endswith("…")


def test_log_without_error_lines_shows_its_last_lines():
    log = "start\nfetching page 1\nfetching page 2\nKilled\n"
    record, _ = run("DataStale", fake_kube(cluster(collect_ok=False, collect_log=log)))
    assert record["evidence"][2]["saw"] == "fetching page 1 | fetching page 2 | Killed"


def test_db_down_is_named_before_the_job_it_breaks():
    record, _ = run("DataStale", fake_kube(cluster(collect_ok=False, db_ready=False)))
    assert record["cause"] == "db_down"


def test_collector_that_has_not_succeeded_for_two_hours():
    record, _ = run("DataStale", fake_kube(cluster(last_success="2026-10-21T11:30:00Z")))
    assert record["cause"] == "collector_late"


def test_nothing_found_is_handed_to_a_human():
    record, sent = run("DataStale", fake_kube(cluster(last_success="2026-10-21T13:00:20Z")))
    assert (record["cause"], record["verdict"]) == (None, None)
    assert "판단 못 함" in sent[0]


def test_errors_right_after_a_deploy():
    record, sent = run("WebErrorRateHigh", fake_kube(cluster(deployed="2026-10-21T13:50:00Z")))
    assert (record["severity"], record["cause"]) == ("critical", "recent_deploy")
    assert [e["look"] for e in record["evidence"]] == ["웹 최근 배포", "웹 파드", "DB 파드"]
    assert "10분 전 · deploy a169f9e04c1b" in sent[0]


def test_errors_with_no_ready_web_pod():
    record, sent = run("WebErrorRateHigh", fake_kube(cluster(web_ready=False, web_waiting="CrashLoopBackOff")))
    assert record["cause"] == "web_down"
    assert "0/1 준비 · CrashLoopBackOff" in sent[0]


def requests_per_second(now, hour_ago):
    """가짜 운영 Prometheus: 요청량 질의에 지금 값, 1시간 전(offset 1h) 질의에 그때 값을 준다."""
    return lambda q: (hour_ago if "offset 1h" in q else now) if "banjjak_http_request_duration_seconds_count" in q else None


def test_slow_pages_under_a_traffic_surge():
    record, sent = run("WebLatencyP95High", fake_kube(cluster()), requests_per_second(6.0, 0.4))
    assert (record["severity"], record["cause"]) == ("warning", "traffic_surge")
    assert [e["look"] for e in record["evidence"]] == ["웹 최근 배포", "웹 파드", "DB 파드", "요청량 (지금 / 1시간 전)"]
    assert "6.00 / 0.40 req/s" in sent[0]


def test_slow_pages_with_ordinary_traffic_are_not_a_surge():
    record, _ = run("WebLatencyP95High", fake_kube(cluster()), requests_per_second(0.3, 0.02))
    assert record["cause"] is None   # 15배지만 초당 1건도 안 된다 → 급증이 아니다


def app_metrics(targets, web_up, web_total):
    """가짜 운영 Prometheus: 앱 클러스터 긁기 대상 수, 그중 웹 파드가 몇 개 응답했나."""
    return fake_prom({'count(up{cluster="app"})': targets,
                      'sum(up{cluster="app",app="web"})': web_up, 'count(up{cluster="app",app="web"})': web_total})


def test_missing_data_age_when_app_metrics_stop_arriving():
    record, sent = run("DataAgeMissing", fake_kube(cluster()), fake_prom({}))
    assert (record["severity"], record["cause"]) == ("critical", "metrics_not_arriving")
    assert [e["look"] for e in record["evidence"]] == ["웹 최근 배포", "웹 파드", "DB 파드", "운영 VM에 오는 앱 지표"]
    assert "앱 클러스터 지표가 하나도 없다" in sent[0]


# 2026-10-22 로컬 k3d에서 실제로 난 장애: 옛 이미지(지표 포트 분리 전)로 배포 → 웹은 준비됐는데 9100을 못 긁는다
def test_missing_data_age_right_after_a_deploy_that_lost_the_metrics_port():
    record, sent = run("DataAgeMissing", fake_kube(cluster(deployed="2026-10-21T13:50:00Z")), app_metrics(2.0, 0.0, 1.0))
    assert record["cause"] == "recent_deploy"
    assert "긁기 대상 2개 · 웹 0/1 응답" in sent[0]


def test_missing_data_age_when_the_web_metrics_port_stops_answering():
    record, _ = run("DataAgeMissing", fake_kube(cluster()), app_metrics(2.0, 0.0, 1.0))
    assert record["cause"] == "web_metrics_down"


def test_missing_data_age_while_web_metrics_answer_is_not_explained():
    record, _ = run("DataAgeMissing", fake_kube(cluster()), app_metrics(2.0, 1.0, 1.0))
    assert record["cause"] is None


def test_low_analysis_success_from_a_failing_analyzer():
    record, sent = run("AnalysisSuccessLow", fake_kube(cluster(analyze_ok=False)))
    assert record["cause"] == "analyzer_failing"
    assert [e["look"] for e in record["evidence"]] == ["분석기 CronJob", "마지막 분석기 Job", "실패한 Job 로그", "DB 파드"]
    assert "UndefinedColumn" in sent[0]


def test_silent_app_cluster_is_the_first_symptom():
    record, _ = run("DataStale", fake_kube({}))
    assert record["cause"] == "app_unreachable"
    assert all(e["saw"].startswith("못 읽음") for e in record["evidence"])


# --- 조치 (#8): 원인 코드 → 조치 · 등급 → 실행 / 거부 / 드라이런 / 사람에게 ---

COLLECTOR = f"/apis/batch/v1{NS}/cronjobs/collector"


def test_t1_runs_after_five_minutes_without_an_answer(tmp_path):
    ops = Ops(str(tmp_path / "opsai.db"), fake_kube(cluster(suspend=True)))
    record = ops.alert("DataStale")
    assert (record["action"], record["tier"], record["state"]) == ("resume_collector", "T1", "waiting")
    assert "조치: 수집기 CronJob 재개 (T1) · 5분 뒤 실행" in ops.sent[0]
    ops.tick(NOW + timedelta(minutes=4, seconds=59))
    assert ops.patched == []
    ops.tick(NOW + timedelta(minutes=5))   # 기다리던 상태는 DB에서 이어받는다 (새 연결)
    assert ops.patched == [(COLLECTOR, {"spec": {"suspend": False}})]
    assert ops.states() == [("resume_collector", "T1", "executed")]
    assert "실행" in ops.sent[-1] and "수집기 CronJob 재개" in ops.sent[-1]
    ops.tick(NOW + timedelta(minutes=10))
    assert len(ops.patched) == 1   # 한 번만 한다


def test_rejected_t1_is_not_run(tmp_path):
    ops = Ops(str(tmp_path / "opsai.db"), fake_kube(cluster(suspend=True)))
    record = ops.alert("DataStale")
    ops.decide(record["id"], approve=False, at=NOW + timedelta(minutes=1))
    ops.tick(NOW + timedelta(minutes=5))
    assert ops.patched == []
    assert ops.states() == [("resume_collector", "T1", "rejected")]
    assert "거부" in ops.sent[-1]


def test_t2_waits_for_approval(tmp_path):
    ops = Ops(str(tmp_path / "opsai.db"), fake_kube({}))   # 앱 클러스터가 아무 답도 하지 않는다
    record = ops.alert("DataAgeMissing")
    assert (record["action"], record["tier"], record["state"]) == ("restart_app_vm", "T2", "waiting")
    assert "승인 전에는 실행하지 않는다" in ops.sent[0]
    ops.tick(NOW + timedelta(hours=6))
    assert ops.patched == [] and ops.states() == [("restart_app_vm", "T2", "waiting")]


def test_same_action_more_than_twice_an_hour_goes_up_a_tier(tmp_path):
    ops = Ops(str(tmp_path / "opsai.db"), fake_kube(cluster(suspend=True)))
    for minutes in (0, 10, 20):
        ops.alert("DataStale", at=NOW + timedelta(minutes=minutes))
        ops.tick(NOW + timedelta(minutes=minutes + 5))
    assert ops.states() == [("resume_collector", "T1", "executed"), ("resume_collector", "T1", "executed"),
                            ("resume_collector", "T2", "waiting")]
    assert "1시간에 3번째 → T2로 올림" in ops.sent[-1]
    ops.tick(NOW + timedelta(hours=2))
    assert len(ops.patched) == 2


def test_dry_run_records_instead_of_running(tmp_path):
    ops = Ops(str(tmp_path / "opsai.db"), fake_kube(cluster(suspend=True)), mode="dry-run")
    ops.alert("DataStale")
    assert "5분 뒤 드라이런" in ops.sent[0]
    ops.tick(NOW + timedelta(minutes=5))
    assert ops.patched == []
    assert ops.states() == [("resume_collector", "T1", "dry_run")]
    assert "드라이런" in ops.sent[-1]


def test_causes_without_an_action_go_to_a_human(tmp_path):
    ops = Ops(str(tmp_path / "opsai.db"), fake_kube(cluster(collect_ok=False)))
    record = ops.alert("DataStale")
    assert record["cause"] == "collector_failing"
    assert ops.states() == [(None, None, "human")]
    assert "조치: 없음 → 사람이 봐 주세요" in ops.sent[0]


def test_rollback_goes_back_one_revision(tmp_path):
    ops = Ops(str(tmp_path / "opsai.db"), fake_kube(cluster(deployed="2026-10-21T13:50:00Z")))
    assert ops.alert("WebErrorRateHigh")["action"] == "rollback_web"
    ops.tick(NOW + timedelta(minutes=5))
    # 지금 판(5) 바로 앞(4)의 template으로. pod-template-hash는 k8s가 다시 붙인다
    assert ops.patched == [(f"/apis/apps/v1{NS}/deployments/web", [
        {"op": "replace", "path": "/spec/template",
         "value": {"metadata": {"labels": {"app": "web"}},
                   "spec": {"containers": [{"name": "web", "image": "ghcr.io/gongbugi/banjjak:good"}]}}},
        {"op": "add", "path": "/metadata/annotations/kubernetes.io~1change-cause", "value": "ops-ai rollback to revision 4"},
    ])]
    assert "판 5 → 4" in ops.sent[-1]


def test_rollback_does_not_undo_its_own_rollback(tmp_path):
    # 되돌린 판도 "최근 배포"로 보인다. 또 되돌리면 망가진 판으로 돌아간다 → 사람에게
    kube = fake_kube(cluster(deployed="2026-10-21T13:50:00Z", change_cause="ops-ai rollback to revision 4"))
    ops = Ops(str(tmp_path / "opsai.db"), kube)
    ops.alert("WebErrorRateHigh")
    ops.tick(NOW + timedelta(minutes=5))
    assert ops.patched == []
    assert ops.states() == [("rollback_web", "T1", "failed")]
    assert "실패" in ops.sent[-1] and "사람" in ops.sent[-1]


def test_traffic_surge_scales_web_to_two(tmp_path):
    rps = lambda q: (0.4 if "offset 1h" in q else 6.0) if "duration_seconds_count" in q else None
    ops = Ops(str(tmp_path / "opsai.db"), fake_kube(cluster()), rps)
    assert ops.alert("WebLatencyP95High")["action"] == "scale_web"
    ops.tick(NOW + timedelta(minutes=5))
    assert ops.patched == [(f"/apis/apps/v1{NS}/deployments/web/scale", {"spec": {"replicas": 2}})]


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


def test_records_each_alert_and_diagnoses_the_firing_one(capsys):
    r = client.post("/alerts", json=PAYLOAD)
    assert r.status_code == 200
    assert r.json() == {"received": 2}
    lines = [json.loads(l) for l in capsys.readouterr().out.splitlines()]
    alerts = [l for l in lines if l["event"] == "alert"]
    assert [(l["alertname"], l["status"]) for l in alerts] == [("DataStale", "firing"), ("WebLatencyP95High", "resolved")]
    assert alerts[0]["summary"] == "마지막 수집 기록이 2시간보다 오래됐다"
    # 풀린 알림은 진단하지 않는다. 시험 환경에는 앱 클러스터 설정이 없어 도구가 하나도 답하지 않는다
    diagnoses = [l for l in lines if l["event"] == "diagnosis"]
    assert [(d["alertname"], d["cause"]) for d in diagnoses] == [("DataStale", "app_unreachable")]


def test_healthz():
    assert client.get("/healthz").json() == {"status": "ok"}
