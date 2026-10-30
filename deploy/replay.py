"""재생 (#20): 시험장 테이프를 LLM 진단기로 다시 돌려 정답 · 점검표와 나란히 본다. 돈이 든다 (장부 replay, 상한 $8은 llm.judge가 막는다).

사용 (레포 루트, 키는 deploy/replay.ps1이 넣는다): powershell -ExecutionPolicy Bypass -File .\\deploy\\replay.ps1 [--model M] [--effort E] [--thinking between_tools] [--times N] [--compare]
읽는 것: drill-out/opsai-drill.db (테이프) · drill-out/answers.jsonl (정답). Azure 테이프에는 정답이 들어 있어 #12 전에는 돌리지 않는다
남는 것: drill-out/replay.db (장부) · drill-out/replay.jsonl (판단 하나에 한 줄) · drill-out/gate.jsonl (jev 점수 하나에 한 줄), git에 안 올린다
"""
import argparse
import json
import os
import sqlite3
import sys
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "app")
from banjjak import actions, llm          # noqa: E402
from banjjak.checklist import CAUSES      # noqa: E402
from banjjak.opsai import KINDS           # noqa: E402

# 주입 종류 → 맞는 원인. 지연 · 5xx 스위치는 운영 AI가 못 읽는 자리라 "모른다"가 정답이다
EXPECT = {"bad_deploy": "recent_deploy", "collector_suspend": "collector_suspended", "surge": "traffic_surge",
          "latency": None, "errors": None}
# 테이프에 있는 T1 원인. jev가 틀린 조치를 거르는지 보려고 조치 없는 경보의 근거에 일부러 붙인다
T1 = ("recent_deploy", "collector_suspended")
# 더 싸게 되는지 본다. 테이프는 medium이 이미 다 맞혀 더 비싼 설정은 돌리지 않는다
COMPARE = [(llm.MODEL, "medium"), (llm.MODEL, "low"), ("claude-haiku-5-5", "medium")]


def tapes():
    with closing(sqlite3.connect("file:drill-out/opsai-drill.db?mode=ro", uri=True)) as c:
        c.row_factory = sqlite3.Row
        return c.execute("SELECT r.alertname, r.cause, s.taken, s.tape FROM snapshots s"
                         " JOIN records r ON r.id = s.record_id ORDER BY s.record_id").fetchall()


def injected(row, answers):
    """이 경보를 울린 주입: 같은 경보 이름 중 울린 시각이 가장 가까운 것."""
    fits = [a for a in answers if row["alertname"] in a["alerts"]]
    return min(fits, key=lambda a: abs(datetime.fromisoformat(a["alert_at"]).timestamp() - row["taken"]))["kind"]


def aged(row):
    """시험장은 경보를 바로 울리려고 수집 시각만 3시간 당겼다. CronJob 성공 시각도 같이 당겨 진짜 장애 모습으로 맞춘다 (안 그러면 읽은 것끼리 어긋난다)."""
    tape = json.loads(row["tape"])
    if row["alertname"] == "DataStale":
        status = tape["kube"]["/apis/batch/v1/namespaces/banjjak/cronjobs/collector"]["status"]
        status["lastSuccessfulTime"] = (datetime.fromisoformat(status["lastSuccessfulTime"]) - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return tape


def one(conn, row, kind, client, args):
    now = datetime.fromtimestamp(row["taken"], timezone.utc)
    first, paid, started = conn.execute("SELECT coalesce(max(id), 0) FROM llm_calls").fetchone()[0], llm.spent(conn, "replay"), time.monotonic()
    said = llm.judge(conn, {"labels": {"alertname": row["alertname"]}}, aged(row), now, client, ledger="replay",
                     model=args.model, effort=args.effort, thinking={"type": args.thinking} if args.thinking else None)
    seconds = time.monotonic() - started
    calls = conn.execute("SELECT count(*) FROM llm_calls WHERE id > ? AND kind = 'call'", (first,)).fetchone()[0]
    action = actions.PLAN.get(said.get("cause"))
    gate = None
    if action and os.environ.get("TYPESAFE_API_KEY"):
        gate = llm.gate(conn, {**said, "alertname": row["alertname"], "sli": KINDS[row["alertname"]][0]}, action, now)
    return {"model": args.model, "effort": args.effort, "thinking": args.thinking, "kind": kind, "alertname": row["alertname"],
            "expect": EXPECT[kind], "checklist": row["cause"], "llm": said.get("cause"), "failed": said.get("failed"),
            "ask_human": said.get("ask_human"), "evidence": said.get("evidence"), "gate": gate, "calls": calls,
            "cost": llm.spent(conn, "replay") - paid, "seconds": round(seconds, 1)}


def calibrate(conn, lines):
    """jev 문턱 재료: 맞는 원인과 일부러 틀린 원인을 점수 낸다. Anthropic은 안 부른다."""
    cases = [(l, l["llm"], True) for l in lines if l["expect"] in T1 and l["llm"] == l["expect"]]
    cases += [(l, cause, False) for l in lines if l["expect"] not in T1 and not l["failed"] for cause in T1]
    scores = []
    with open("drill-out/gate.jsonl", "a", encoding="utf-8") as out:
        for l, cause, fits in cases:
            record = {"alertname": l["alertname"], "sli": KINDS[l["alertname"]][0], "verdict": CAUSES[cause], "evidence": l["evidence"]}
            said = llm.gate(conn, record, actions.PLAN[cause], datetime.now(timezone.utc))
            out.write(json.dumps({"kind": l["kind"], "cause": cause, "fits": fits, **said}, ensure_ascii=False) + "\n")
            if "p" in said:
                scores.append((cause, fits, said["p"]))
    for cause in T1:
        print(f"jev {cause}: 맞는 원인 {spread(scores, cause, True)} · 틀린 원인 {spread(scores, cause, False)}")


def spread(scores, cause, fits):
    picked = [p for c, f, p in scores if c == cause and f == fits]
    return f"{min(picked):.2f}~{max(picked):.2f} ({len(picked)}건)" if picked else "없음"


def run(conn, rows, answers, args):
    """설정 하나로 테이프를 다 돌고 요약을 찍는다."""
    print(f"\n== {args.model} · {args.effort}" + (f" · {args.thinking}" if args.thinking else ""))
    lines = []
    with open("drill-out/replay.jsonl", "a", encoding="utf-8") as out:
        for _ in range(args.times):
            for row in rows:
                line = one(conn, row, injected(row, answers), llm.client(), args)
                lines.append(line)
                out.write(json.dumps(line, ensure_ascii=False) + "\n")
                mark = "실패" if line["failed"] else "맞음" if line["llm"] == line["expect"] else "틀림"
                print(f"{mark} {line['kind']:18} 정답 {line['expect']} · 점검표 {line['checklist']} · LLM {line['llm']}"
                      f" · 호출 {line['calls']} · {line['seconds']}초 · ${line['cost']:.4f}"
                      + (f" · jev {line['gate'].get('p')}" if line["gate"] else "")
                      + (" · 승인 요청" if line["ask_human"] else "")
                      + (f" · {line['failed'][:80]}" if line["failed"] else ""), flush=True)
    judged = [l for l in lines if not l["failed"]]
    wrong = [l for l in judged if l["llm"] != l["expect"]]
    print(f"\n{len(lines)}건: 맞음 {len(judged) - len(wrong)} · 틀린 원인 {sum(1 for l in wrong if l['llm'])} · 모른다고 함 "
          f"{sum(1 for l in wrong if not l['llm'])} · 실패 {len(lines) - len(judged)}")
    print(f"이번 ${sum(l['cost'] for l in lines):.3f} · 건당 ${sum(l['cost'] for l in lines) / max(len(lines), 1):.4f}"
          f" · 호출 평균 {sum(l['calls'] for l in lines) / max(len(lines), 1):.1f} · 재생 장부 합계 ${llm.spent(conn, 'replay'):.3f}"
          f" / ${llm.CAPS['replay']['total']:g}")
    acting = [l for l in lines if l["llm"] in T1]
    print(f"조치 {len(acting)}건: LLM 승인 요청 {sum(1 for l in acting if l['ask_human'])}"
          f" · jev T1 통과 {sum(1 for l in acting if l['gate'] and l['gate']['say'] == 'keep')}")
    return lines


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=llm.MODEL)
    ap.add_argument("--effort", default=llm.EFFORT)
    ap.add_argument("--thinking", choices=["between_tools"])
    ap.add_argument("--times", type=int, default=1)
    ap.add_argument("--compare", action="store_true", help="COMPARE의 설정을 차례로 돈다")
    args = ap.parse_args()

    answers = [a for a in map(json.loads, open("drill-out/answers.jsonl", encoding="utf-8")) if a["alerts"]]
    rows = tapes()
    runs = []
    with closing(actions.connect("drill-out/replay.db")) as conn:
        for args.model, args.effort in COMPARE if args.compare else [(args.model, args.effort)]:
            runs.append(run(conn, rows, answers, args))
        if os.environ.get("TYPESAFE_API_KEY"):
            calibrate(conn, runs[0])


if __name__ == "__main__":
    main()
