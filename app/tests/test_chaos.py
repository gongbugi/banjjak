"""장애 주입기 1회 실행 (#10): 주사위는 고정하고, 앱 클러스터는 가짜(요청을 기록), 정답 기록은 진짜 SQLite."""
import json
from contextlib import closing
from datetime import datetime, timedelta, timezone

import pytest

from banjjak.chaos import App, connect, inject, run_once

NOW = datetime(2026, 10, 24, 3, 0, tzinfo=timezone.utc)
NS = "/namespaces/banjjak"
FAULT_CM = f"/api/v1{NS}/configmaps/web-fault"
WEB = f"/apis/apps/v1{NS}/deployments/web"
COLLECTOR = f"/apis/batch/v1{NS}/cronjobs/collector"
LOAD = "/repos/gongbugi/banjjak-load/actions"


class Dice:
    """random()이 차례로 돌려줄 값. 다 쓰면 0.5."""
    def __init__(self, *values):
        self.values = list(values)

    def random(self):
        return self.values.pop(0) if self.values else 0.5


def pod(name, created):
    return {"metadata": {"name": name, "creationTimestamp": created}}


class Cluster:
    """가짜 앱 클러스터 + 가짜 GitHub: 상태를 들고 있고, 쓰기 요청은 calls에 쌓인다 (상태는 시험이 직접 바꾼다)."""
    def __init__(self, deployed="2026-10-23T20:00:00Z", load=True):
        self.calls = []
        self.runs = []   # 비공개 레포 load 워크플로 실행 목록 (GitHub가 돌려줄 것)
        self.state = {
            f"/api/v1{NS}/pods?labelSelector=app%3Dweb": {"items": [
                pod("web-old", "2026-10-23T10:00:00Z"), pod("web-new", "2026-10-23T20:00:00Z")]},
            f"/api/v1{NS}/pods?labelSelector=app%3Dpostgres": {"items": [pod("postgres-0", "2026-10-21T08:00:00Z")]},
            WEB: {"metadata": {"annotations": {"kubernetes.io/change-cause": "deploy 10182a25b07c at " + deployed}},
                  "spec": {"template": {"spec": {"containers": [
                      {"name": "web", "env": [{"name": "DATABASE_URL", "valueFrom": {}}]}]}}},
                  "status": {"conditions": [{"type": "Progressing", "lastUpdateTime": deployed}]}},
            COLLECTOR: {"spec": {"suspend": False}},
        }
        self.app = App(self.kube, lambda path, body, ctype: self.calls.append(("patch", path, body)),
                       lambda path: self.calls.append(("delete", path)), self.github if load else None)

    def kube(self, path):
        return self.state[path]

    def github(self, method, path, body=None):
        self.calls.append(("github", method, path, body))
        return {"workflow_runs": self.runs} if method == "GET" else None


class Chaos:
    def __init__(self, tmp_path, cluster=None):
        self.db, self.cluster = str(tmp_path / "chaos.db"), cluster or Cluster()

    def run(self, at, dice=None, switch="on"):
        with closing(connect(self.db)) as conn:
            return run_once(conn, at, self.cluster.app, dice or Dice(), switch)

    def inject(self, kind, at=NOW, dice=None):
        with closing(connect(self.db)) as conn:
            return inject(conn, kind, at, self.cluster.app, dice or Dice(), manual=True)

    def answers(self):   # 정답 기록
        with closing(connect(self.db)) as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM injections ORDER BY id")]


HIT = 0.0001   # 주사위가 맞는 값 (확률보다 작다)


def test_switch_off_injects_nothing(tmp_path):
    chaos = Chaos(tmp_path)
    assert chaos.run(NOW, Dice(HIT), switch="off") == "off"
    assert chaos.answers() == [] and chaos.cluster.calls == []


def test_dice_decides_when_and_what(tmp_path):
    chaos = Chaos(tmp_path)
    assert chaos.run(NOW, Dice(0.5)) == "dice"   # 빗나감
    # 맞으면 종류를 가중치로 고른다: 두 번째 주사위 0.3 → 지연, 세 번째 · 네 번째는 세기 · 지속 시간
    assert chaos.run(NOW, Dice(HIT, 0.3, 0.5, 0.5)) == "injected latency"
    patch = chaos.cluster.calls[-1]
    assert patch[:2] == ("patch", FAULT_CM) and json.loads(patch[2]["data"]["fault.json"]) == {"delay": 1.5}
    [answer] = chaos.answers()
    assert (answer["kind"], answer["state"], answer["manual"]) == ("latency", "active", 0)
    assert answer["until"] - answer["started"] == 22.5 * 60   # 15~30분 사이


def test_one_at_a_time(tmp_path):
    chaos = Chaos(tmp_path)
    chaos.inject("latency")
    assert chaos.run(NOW + timedelta(minutes=10), Dice(HIT)) == "active latency"
    assert len(chaos.answers()) == 1


def test_at_least_twelve_hours_between_injections(tmp_path):
    chaos = Chaos(tmp_path)
    chaos.inject("web_pod_kill")
    chaos.run(NOW + timedelta(minutes=10))   # 파드 종료는 10분 뒤 끝난 것으로 기록
    assert chaos.run(NOW + timedelta(hours=11), Dice(HIT)) == "gap"
    assert chaos.run(NOW + timedelta(hours=13), Dice(HIT, 0.3)).startswith("injected")


def test_no_injection_right_after_a_deploy(tmp_path):
    # 내 배포 직후에 넣으면 점검표가 "최근 배포 → 롤백"으로 내 배포를 되돌린다
    chaos = Chaos(tmp_path, Cluster(deployed="2026-10-24T02:45:00Z"))
    assert chaos.run(NOW, Dice(HIT)) == "recent deploy"


def test_self_recovering_fault_is_switched_off_when_its_time_is_up(tmp_path):
    chaos = Chaos(tmp_path)
    answer = chaos.inject("errors", dice=Dice(0.0, 0.0))   # 세기 20% · 지속 15분
    assert json.loads(chaos.cluster.calls[-1][2]["data"]["fault.json"]) == {"error_rate": 0.2}
    assert chaos.run(NOW + timedelta(minutes=14)) == "active errors"
    assert chaos.run(NOW + timedelta(minutes=15)) == "ended errors"
    assert chaos.cluster.calls[-1] == ("patch", FAULT_CM, {"data": {"fault.json": "{}"}})
    assert chaos.answers()[0]["ended_by"] == "time"


def test_bad_deploy_looks_like_a_deploy_and_is_fixed_by_a_rollback(tmp_path):
    chaos = Chaos(tmp_path)
    chaos.inject("bad_deploy", dice=Dice(0.0, 0.0))   # 5xx 쪽 · 세기 20%
    op, path, body = chaos.cluster.calls[-1]
    assert (op, path) == ("patch", WEB)
    assert body[0] == {"op": "add", "path": "/spec/template/spec/containers/0/env/-",
                       "value": {"name": "FAULT", "value": '{"error_rate": 0.2}'}}
    cause = body[1]["value"]
    assert cause.startswith("deploy config-") and "chaos" not in cause   # 운영 AI · 사람에게 정답이 새지 않게
    # 운영 AI가 롤백했다: FAULT가 빠지고 배포 기록이 바뀐다
    chaos.cluster.state[WEB]["metadata"]["annotations"]["kubernetes.io/change-cause"] = "ops-ai rollback to revision 9"
    assert chaos.run(NOW + timedelta(minutes=20)) == "ended bad_deploy"
    answer = chaos.answers()[0]
    assert (answer["ended_by"], answer["note"]) == ("fixed", "ops-ai rollback to revision 9")


def test_unfixed_bad_deploy_is_reverted_after_two_hours(tmp_path):
    chaos = Chaos(tmp_path)
    chaos.inject("bad_deploy", dice=Dice(0.9, 0.0))   # 지연 쪽
    containers = chaos.cluster.state[WEB]["spec"]["template"]["spec"]["containers"]
    containers[0]["env"].append({"name": "FAULT", "value": '{"delay": 1.0}'})   # 아무도 고치지 않았다
    assert chaos.run(NOW + timedelta(minutes=119)) == "active bad_deploy"
    assert chaos.run(NOW + timedelta(minutes=120)) == "ended bad_deploy"
    op, path, body = chaos.cluster.calls[-1]
    assert (op, path, body[0]) == ("patch", WEB, {"op": "remove", "path": "/spec/template/spec/containers/0/env/1"})
    assert chaos.answers()[0]["ended_by"] == "reverted"


def test_suspended_collector_is_fixed_by_resuming_it(tmp_path):
    chaos = Chaos(tmp_path)
    chaos.inject("collector_suspend")
    assert chaos.cluster.calls[-1] == ("patch", COLLECTOR, {"spec": {"suspend": True}})
    chaos.cluster.state[COLLECTOR]["spec"]["suspend"] = True
    assert chaos.run(NOW + timedelta(hours=3)) == "active collector_suspend"
    chaos.cluster.state[COLLECTOR]["spec"]["suspend"] = False   # 운영 AI가 다시 켰다
    assert chaos.run(NOW + timedelta(hours=3, minutes=10)) == "ended collector_suspend"
    assert chaos.answers()[0]["ended_by"] == "fixed"


def test_pod_kill_deletes_the_newest_web_pod(tmp_path):
    chaos = Chaos(tmp_path)
    chaos.inject("web_pod_kill")
    assert chaos.cluster.calls == [("delete", f"/api/v1{NS}/pods/web-new")]


def test_surge_asks_the_private_load_workflow_to_push_from_outside_the_vm(tmp_path):
    # 같은 VM 안 부하는 측정을 오염시킨다 (10-20 결정) → GitHub Actions 러너가 초당 800건 15분 (#11 측정 조건)
    chaos = Chaos(tmp_path)
    answer = chaos.inject("surge")
    assert chaos.cluster.calls == [("github", "POST", f"{LOAD}/workflows/load.yml/dispatches",
                                    {"ref": "main", "inputs": {"ref": "main", "rates": "800", "seconds": "900"}})]
    assert json.loads(answer["params"]) == {"rate": 800, "seconds": 900}
    assert answer["until"] - answer["started"] == 30 * 60   # 러너 준비 + 15분 + 여유


def test_surge_ends_on_its_own_and_stops_a_run_still_going(tmp_path):
    chaos = Chaos(tmp_path)
    chaos.inject("surge")
    assert chaos.run(NOW + timedelta(minutes=20)) == "active surge"
    chaos.cluster.runs = [{"id": 7, "status": "in_progress"}, {"id": 6, "status": "completed"}]
    assert chaos.run(NOW + timedelta(minutes=30)) == "ended surge"
    assert chaos.cluster.calls[-2:] == [("github", "GET", f"{LOAD}/workflows/load.yml/runs?per_page=5", None),
                                        ("github", "POST", f"{LOAD}/runs/7/cancel", None)]
    assert chaos.answers()[0]["ended_by"] == "time"


def test_surge_is_never_picked_without_the_load_token(tmp_path):
    # 토큰이 없으면 (로컬 k3d · Secret 넣기 전) 급증을 고르지 않는다. 골라서 실패하면 10분마다 같은 실패가 쌓인다
    roll = 0.99   # 가중치 맨 끝 = 급증
    (tmp_path / "with").mkdir(), (tmp_path / "without").mkdir()
    assert Chaos(tmp_path / "with").run(NOW, Dice(HIT, roll)) == "injected surge"
    assert Chaos(tmp_path / "without", Cluster(load=False)).run(NOW, Dice(HIT, roll)) == "injected collector_suspend"


def test_turning_the_switch_off_ends_what_is_running(tmp_path):
    chaos = Chaos(tmp_path)
    chaos.inject("latency")
    assert chaos.run(NOW + timedelta(minutes=5), switch="off") == "off"
    assert chaos.cluster.calls[-1] == ("patch", FAULT_CM, {"data": {"fault.json": "{}"}})
    assert chaos.answers()[0]["ended_by"] == "switch_off"


def test_manual_mode_never_rolls_dice_but_tends_what_runs(tmp_path):
    # 낮 시험 주입: 스위치를 manual로 두고 손으로 넣는다 → 무작위는 쉬고, 넣은 것은 평소처럼 끝낸다
    chaos = Chaos(tmp_path)
    assert chaos.run(NOW, Dice(HIT), switch="manual") == "manual"
    chaos.inject("latency", dice=Dice(0.0, 0.0))
    assert chaos.answers()[0]["manual"] == 1
    assert chaos.run(NOW + timedelta(minutes=15), switch="manual") == "ended latency"


def test_gap_and_chance_come_from_the_environment(tmp_path, monkeypatch):
    # 고빈도 구간 (10-28 본인 결정 B): 이미지를 다시 만들지 않고 CronJob 환경 변수로 바꾼다. 없으면 원래 값
    import importlib

    import banjjak.chaos as chaos_module
    try:
        monkeypatch.delenv("CHAOS_GAP_HOURS", raising=False)
        monkeypatch.delenv("CHAOS_CHANCE", raising=False)
        importlib.reload(chaos_module)
        assert chaos_module.MIN_GAP == timedelta(hours=12) and round(chaos_module.CHANCE * 7 * 24 * 6, 6) == 5
        monkeypatch.setenv("CHAOS_GAP_HOURS", "4")
        monkeypatch.setenv("CHAOS_CHANCE", "0.333")   # 10분마다 1/3 → 간격이 지나면 평균 30분 안에 넣는다
        importlib.reload(chaos_module)
        assert (chaos_module.MIN_GAP, chaos_module.CHANCE) == (timedelta(hours=4), 0.333)
        chaos = Chaos(tmp_path)
        chaos.inject("web_pod_kill")
        chaos.run(NOW + timedelta(minutes=10))
        assert chaos.run(NOW + timedelta(hours=3), Dice(HIT)) == "gap"
        assert chaos.run(NOW + timedelta(hours=5), Dice(0.3, 0.3)).startswith("injected")   # 0.3 < 1/3
    finally:
        monkeypatch.delenv("CHAOS_GAP_HOURS", raising=False)
        monkeypatch.delenv("CHAOS_CHANCE", raising=False)
        importlib.reload(chaos_module)   # 다른 시험은 원래 값으로


def test_answer_is_written_before_the_fault_goes_in(tmp_path):
    # 응답을 못 받은 넣기도 들어갔을 수 있다. 되돌리기는 기록만 보고 움직이므로 기록이 먼저다
    chaos = Chaos(tmp_path)
    good = chaos.cluster.app.patch

    def lost(path, body, ctype):
        good(path, body, ctype)
        raise TimeoutError("response lost")
    chaos.cluster.app = chaos.cluster.app._replace(patch=lost)
    with pytest.raises(TimeoutError):
        chaos.inject("latency")
    row = chaos.answers()[0]
    assert (row["kind"], row["state"], json.loads(row["params"])) == ("latency", "active", {"unsure": True})
    chaos.cluster.app = chaos.cluster.app._replace(patch=good)
    chaos.run(NOW + timedelta(minutes=10), switch="off")
    assert chaos.cluster.calls[-1] == ("patch", FAULT_CM, {"data": {"fault.json": "{}"}})
    assert chaos.answers()[0]["state"] == "ended"
