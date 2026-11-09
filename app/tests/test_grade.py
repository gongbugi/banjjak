"""#12 채점기 (deploy/grade.py): 주입마다 경보를 짝짓고, 점검표 · LLM 판단을 정답과 대조한다."""
import json
import os
import sys
from contextlib import closing
from datetime import datetime, timedelta, timezone

from banjjak import actions, chaos

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "deploy"))
import grade   # noqa: E402

KST = timezone(timedelta(hours=9))
DAY = datetime(2026, 10, 26, 10, 0, tzinfo=KST)


def at(minutes):
    return (DAY + timedelta(minutes=minutes)).timestamp()


def make(tmp_path):
    with closing(chaos.connect(str(tmp_path / "chaos.db"))) as c:
        for kind, start, end, by, manual in [("bad_deploy", 0, 20, "fixed", 0), ("latency", 60, 80, "time", 0),
                                             ("collector_suspend", 85, 120, "fixed", 0), ("web_pod_kill", 200, 210, "time", 0),
                                             ("latency", 500, 520, "time", 1),   # 손 주입: 정답을 알고 넣은 것이라 뺀다
                                             ("errors", 15 * 60, 15 * 60 + 20, "time", 0)]:
            c.execute("INSERT INTO injections (kind, manual, params, started, until, state, ended, ended_by)"
                      " VALUES (?, ?, '{}', ?, ?, 'ended', ?, ?)", (kind, manual, at(start), at(end), at(end), by))
        c.commit()
    with closing(actions.connect(str(tmp_path / "opsai.db"))) as c:
        def alert(minutes, name, checklist, llm, action=None, after=None, episode=None):
            diag = {"alertname": name, "checklist": {"cause": checklist}, "llm": llm, **({"after": after} if after else {}),
                    **({"fingerprint": episode, "startsAt": f"{episode}-start"} if episode else {})}
            return c.execute("INSERT INTO records (created, alertname, cause, action, tier, state, diagnosis)"
                             " VALUES (?, ?, ?, ?, 'T1', 'executed', ?)",
                             (at(minutes), name, checklist, action, json.dumps(diag))).lastrowid
        rollback = alert(6, "WebErrorRateHigh", "recent_deploy", {"cause": "recent_deploy"}, "rollback_web")
        alert(12, "WebErrorRateHigh", "recent_deploy", None, after=1)              # 재진단은 탐지로 세지 않는다
        alert(91, "DataStale", "collector_suspended", {"failed": "상한"})        # 앞 주입(latency)의 15분 꼬리 안이지만 이 주입 것
        alert(300, "WebLatencyP95High", None, {"cause": None}, episode="f1")     # 주입 없는 경보
        alert(905, "WebLatencyP95High", None, {"cause": None}, episode="f1")     # 그 경보의 재알림: errors 주입의 탐지가 아니다
        alert(400, "WebErrorRateHigh", "recent_deploy", {"cause": "recent_deploy"})   # 사람 배포 10분 뒤
        alert(700, "WebErrorRateHigh", None, {"cause": None})                     # 부하 측정 창 안: 뺀다
        # 운영 AI가 롤백 뒤 회복을 확인한 시각 = 복구 끝. 수집 멈춤은 확인 기록이 없다 (사람이 고쳤다)
        c.execute("INSERT INTO checks (record_id, until, next, state, decided, result) VALUES (?, 0, 0, 'recovered', ?, '경보가 풀렸다')",
                  (rollback, at(17)))
        for minutes in (6, 6, 300, 905):
            c.execute("INSERT INTO llm_calls (at, ledger, kind, cost) VALUES (?, 'live', 'call', 0.01)", (at(minutes),))
        c.execute("INSERT INTO llm_calls (at, ledger, kind, cost) VALUES (?, 'replay', 'call', 9)", (at(6),))
        c.commit()
    return grade.load_answers(str(tmp_path / "chaos.db")), *grade.load_ops(str(tmp_path / "opsai.db"))


def test_pairs_alerts_and_counts(tmp_path):
    rows, stray, dropped = grade.grade(*make(tmp_path), at(-60), at(24 * 60), [at(390)], [(at(690), at(710))])
    by = {r["kind"]: r for r in rows}
    assert by["bad_deploy"]["alerts"] == ["WebErrorRateHigh"] and by["bad_deploy"]["mttd"] == 6
    assert by["latency"]["alerts"] == [] and by["collector_suspend"]["alerts"] == ["DataStale"]
    assert by["errors"]["alerts"] == [] and by["errors"]["night"] is True and by["bad_deploy"]["cost"] == 0.02
    assert dropped == {"손 주입": 1, "제외 창 주입": 0, "제외 창 경보": 1}
    # 복구: 운영 AI가 고쳤으면 회복 확인 시각, 아니면 주입기가 본 시각
    assert (by["bad_deploy"]["mttr"], by["bad_deploy"]["fixer"]) == (17, "운영 AI")
    assert (by["collector_suspend"]["mttr"], by["collector_suspend"]["fixer"]) == (35, "사람 · 그 밖")
    s = grade.summary(rows, stray)
    assert s["탐지율"] == "2/4 (50%)"                 # 경보가 나야 하는 종류만. 파드 종료는 따로
    assert s["경보 없이 회복 (파드 종료)"] == "1/1 (100%)"
    assert s["주입 없는 경보"] == "1 (배포 구간 1 따로)"   # 재알림은 같은 경보
    assert s["점검표 원인 맞음"] == "2/2 (100%)"
    assert s["LLM 원인 맞음"] == "1/1 (100%) · 실패 1"
    assert s["조치로 풀림"] == "2/2 (100%) · 운영 AI 1" and s["복구까지 낮 (중앙값)"] == "26분 (n=2)"
    assert s["LLM 비용"] == "$0.0400 · 경보당 $0.0100"
    text = grade.report(rows, stray, dropped, at(-60), at(24 * 60), [(at(100), "둘째")])
    assert "| collector_suspend | 낮 | DataStale | 6분 | collector_suspended | collector_suspended ✓ | 실패 |" in text
    assert "| 기간 | 1.0일 | 0.1일 | 0.9일 |" in text and "| 주입 | 5 | 3 | 2 |" in text   # 전체 · 구간 둘
    assert "| 종류 | bad_deploy | collector_suspend | errors | latency | web_pod_kill |" in text   # 종류별 표
    assert "뺀 것: 손 주입 1 · 제외 창 주입 0 · 제외 창 경보 1" in text
