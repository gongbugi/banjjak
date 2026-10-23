"""장애 주입기 1회 실행 (#10): 주사위는 고정하고, 앱 클러스터는 가짜(요청을 기록), 정답 기록은 진짜 SQLite."""
import json
from contextlib import closing
from datetime import datetime, timedelta, timezone

from banjjak.chaos import App, connect, inject, run_once

NOW = datetime(2026, 10, 24, 3, 0, tzinfo=timezone.utc)
NS = "/namespaces/banjjak"
FAULT_CM = f"/api/v1{NS}/configmaps/web-fault"
WEB = f"/apis/apps/v1{NS}/deployments/web"
COLLECTOR = f"/apis/batch/v1{NS}/cronjobs/collector"


class Dice:
    """random()이 차례로 돌려줄 값. 다 쓰면 0.5."""
    def __init__(self, *values):
        self.values = list(values)

    def random(self):
        return self.values.pop(0) if self.values else 0.5


def pod(name, created):
    return {"metadata": {"name": name, "creationTimestamp": created}}


class Cluster:
    """가짜 앱 클러스터: 상태를 들고 있고, 쓰기 요청은 calls에 쌓인다 (상태는 시험이 직접 바꾼다)."""
    def __init__(self, deployed="2026-10-23T20:00:00Z"):
        self.calls = []
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
                       lambda path: self.calls.append(("delete", path)))

    def kube(self, path):
        return self.state[path]


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
