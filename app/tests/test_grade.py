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
        for kind, start, end, by in [("bad_deploy", 0, 20, "fixed"), ("latency", 60, 80, "time"),
                                     ("collector_suspend", 85, 120, "fixed"), ("errors", 15 * 60, 15 * 60 + 20, "time")]:
            c.execute("INSERT INTO injections (kind, manual, params, started, until, state, ended, ended_by)"
                      " VALUES (?, 0, '{}', ?, ?, 'ended', ?, ?)", (kind, at(start), at(end), at(end), by))
        c.commit()
    with closing(actions.connect(str(tmp_path / "opsai.db"))) as c:
        def alert(minutes, name, checklist, llm, action=None, after=None):
            diag = {"alertname": name, "checklist": {"cause": checklist}, "llm": llm, **({"after": after} if after else {})}
            c.execute("INSERT INTO records (created, alertname, cause, action, tier, state, diagnosis)"
                      " VALUES (?, ?, ?, ?, 'T1', 'executed', ?)", (at(minutes), name, checklist, action, json.dumps(diag)))
        alert(6, "WebErrorRateHigh", "recent_deploy", {"cause": "recent_deploy"}, "rollback_web")
        alert(12, "WebErrorRateHigh", "recent_deploy", None, after=1)              # 재진단은 탐지로 세지 않는다
        alert(91, "DataStale", "collector_suspended", {"failed": "상한"})        # 앞 주입(latency)의 15분 꼬리 안이지만 이 주입 것
        alert(300, "WebLatencyP95High", None, {"cause": None})                    # 주입 없는 경보
        alert(400, "WebErrorRateHigh", "recent_deploy", {"cause": "recent_deploy"})   # 사람 배포 10분 뒤
        for minutes in (6, 6, 300):
            c.execute("INSERT INTO llm_calls (at, ledger, kind, cost) VALUES (?, 'live', 'call', 0.01)", (at(minutes),))
        c.execute("INSERT INTO llm_calls (at, ledger, kind, cost) VALUES (?, 'replay', 'call', 9)", (at(6),))
        c.commit()
    return grade.load_answers(str(tmp_path / "chaos.db")), *grade.load_ops(str(tmp_path / "opsai.db"))


def test_pairs_alerts_and_counts(tmp_path):
    rows, stray = grade.grade(*make(tmp_path), at(-60), at(24 * 60), [at(390)])
    by = {r["kind"]: r for r in rows}
    assert by["bad_deploy"]["alerts"] == ["WebErrorRateHigh"] and by["bad_deploy"]["mttd"] == 6
    assert by["latency"]["alerts"] == [] and by["collector_suspend"]["alerts"] == ["DataStale"]
    assert by["errors"]["night"] is True and by["bad_deploy"]["cost"] == 0.02
    s = grade.summary(rows, stray)
    assert s["탐지율"] == "2/4 (50%)"
    assert s["주입 없는 경보"] == "1 (배포 구간 1 따로)"
    assert s["점검표 원인 맞음"] == "2/2 (100%)"
    assert s["LLM 원인 맞음"] == "1/1 (100%) · 실패 1"
    assert s["조치로 풀림"] == "2/2 (100%)" and s["복구까지 낮 (중앙값)"] == "28분 (n=2)"
    assert s["LLM 비용"] == "$0.0300 · 경보당 $0.0075"
    text = grade.report(rows, stray, at(-60), at(24 * 60))
    assert "| collector_suspend | 낮 | DataStale | 6분 | collector_suspended | collector_suspended ✓ | 실패 |" in text
