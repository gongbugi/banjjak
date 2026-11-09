"""채점 (#12): 장애 주입 정답(injections)과 운영 AI 판단(records · checks · llm_calls)을 짝지어 숫자로 낸다.

사용 (레포 루트): app/.venv/Scripts/python deploy/grade.py <정답> <opsai.db> [--since ISO] [--until ISO] [--deploy ISO ...]
                  [--exclude 시작ISO 끝ISO ...] [--split ISO ...] [--out 파일.md]
- 정답: Azure는 내보낸 chaos.db, 로컬 시험장은 drill-out/answers.jsonl (급증은 주입기 밖에서 넣어 시각만 있다)
- Azure 자료는 정답과 판단을 함께 읽는다 → 실험이 끝난 뒤에만 돌린다. 그 전에는 시험장 자료로만
- --deploy: 사람이 한 배포 시각. 그 뒤 30분의 경보는 "배포 구간"으로 따로 센다 (주입기도 그 30분은 쉰다)
- --exclude: 사람이 부하를 넣은 창 등. 그 안에서 시작한 주입 · 경보는 아예 뺀다. 손 주입(정답을 알고 넣은 것)도 뺀다
- --split: 표를 나눌 시각. 없으면 PHASES (빈도 · 탐지 능력 · 판단 주체가 바뀐 시각)
짝짓기: 같은 경보의 재알림(fingerprint · startsAt이 같은 기록)은 하나로 묶는다. 묶은 경보의 첫 기록이
주입 [시작, 끝 + 15분] 안이면 그 주입의 탐지다. 어느 주입에도 안 든 경보는 "주입 없는 경보".
"""
import argparse
import json
import sqlite3
import statistics
import sys
from contextlib import closing
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "app")
from banjjak.chaos import AFTER_DEPLOY, NEEDS_ACTION   # noqa: E402
from replay import EXPECT                # noqa: E402  주입 종류 → 맞는 원인 (#20 재생과 같은 표)

KST = timezone(timedelta(hours=9))
GRACE = timedelta(minutes=15)   # 경보는 주입이 끝난 뒤에도 몇 분 남는다 (for · 평가 주기)
NIGHT = (23, 7)                 # 🟡 23시 ~ 7시 KST를 밤으로 본다 (본인 정의 없음). T2는 사람이 깰 때까지 기다린다
# 볼트 MOC에 "#12에서 나눈다"로 적은 시각: (시작, 그때 바뀐 것). 첫 구간(--since부터)은 "시작"
PHASES = [("2026-10-28T16:08:52+09:00", "주 5회"), ("2026-10-28T18:04:18+09:00", "+ 입구 경보"),
          ("2026-10-28T20:03:26+09:00", "고빈도"), ("2026-10-30T14:37:59+09:00", "+ 급증"),
          ("2026-10-30T23:06:50+09:00", "주 5회"), ("2026-10-31T16:28:54+09:00", "+ LLM")]


def load_answers(path):
    """주입 목록: kind · started · ended (초, 끝나지 않았으면 None) · ended_by · manual."""
    if path.endswith(".jsonl"):
        out = []
        for line in open(path, encoding="utf-8"):
            a = json.loads(line)
            i = a.get("injection") or {"kind": a["kind"], "ended_by": a.get("ended_by"),
                                       "started": datetime.fromisoformat(a["injected_at"]).timestamp(),
                                       "ended": datetime.fromisoformat(a["ended_at"]).timestamp() if a.get("ended_at") else None}
            # 시험장은 전부 손으로 넣는다 (정답을 아는 시험 자체). 손 주입으로 거르지 않는다
            out.append({**{k: i.get(k) for k in ("kind", "started", "ended", "ended_by")}, "manual": 0})
        return out
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as c:
        c.row_factory = sqlite3.Row
        return [dict(r) for r in c.execute("SELECT kind, started, ended, ended_by, manual FROM injections ORDER BY started")]


def load_ops(path):
    """경보 기록 (진단 JSON을 펼침) · 조치 확인 결과 · 경보마다 LLM 비용 (진단과 같은 시각에 장부에 남는다)."""
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as c:
        c.row_factory = sqlite3.Row
        records = [dict(r, diag=json.loads(r["diagnosis"])) for r in c.execute("SELECT * FROM records ORDER BY created")]
        checks = {r["record_id"]: dict(r) for r in c.execute("SELECT * FROM checks")}
        costs = {}
        has_ledger = c.execute("SELECT 1 FROM sqlite_master WHERE name = 'llm_calls'").fetchone()   # LLM 전 시험장 DB에는 없다
        for r in c.execute("SELECT at, cost FROM llm_calls WHERE ledger = 'live'") if has_ledger else ():
            costs[r["at"]] = costs.get(r["at"], 0) + r["cost"]
    return records, checks, costs


def _night(ts):
    h = datetime.fromtimestamp(ts, KST).hour
    return h >= NIGHT[0] or h < NIGHT[1]


def _llm(diag):
    """LLM 판단: 원인 · 실패 · 없음 (키가 없던 때)."""
    said = diag.get("llm")
    return "없음" if said is None else "실패" if "failed" in said else said.get("cause")


def episodes(records):
    """경보 하나 = 기록 목록. Alertmanager는 계속 울리는 경보를 4시간마다 · 같은 이름 묶음이 바뀔 때 다시 보내고,
    운영 AI는 받을 때마다 기록을 남긴다. fingerprint · startsAt이 같으면 같은 경보다 (없는 손 시험 기록은 따로)."""
    out = {}
    for r in records:
        if r["diag"].get("after"):   # 재진단은 탐지로 세지 않는다
            continue
        d = r["diag"]
        key = (r["alertname"], d["fingerprint"], d["startsAt"]) if d.get("fingerprint") and d.get("startsAt") else r["id"]
        out.setdefault(key, []).append(r)
    return list(out.values())


def grade(answers, records, checks, costs, since, until, deploys=(), excludes=()):
    """주입마다 한 줄 + 주입 없는 경보 + 뺀 것의 수. 시각은 초."""
    out_of = lambda ts: any(s <= ts <= e for s, e in excludes)   # noqa: E731
    span = [a for a in answers if since <= a["started"] < until]
    shots = [a for a in span if not a["manual"] and not out_of(a["started"])]
    span_eps = [e for e in episodes(records) if since <= e[0]["created"] < until]
    eps = [e for e in span_eps if not out_of(e[0]["created"])]
    dropped = {"손 주입": sum(bool(a["manual"]) for a in span),
               "제외 창 주입": sum(not a["manual"] and out_of(a["started"]) for a in span), "제외 창 경보": len(span_eps) - len(eps)}
    # 경보는 그 직전에 시작한 주입의 것이다 (끝 + 15분 안일 때만). 앞 주입의 꼬리가 다음 주입의 경보를 가져가지 않게
    owner = {}
    for n, e in enumerate(eps):
        before = [i for i, a in enumerate(shots) if a["started"] <= e[0]["created"] <= (a["ended"] or until) + GRACE.total_seconds()]
        if before:
            owner[n] = max(before, key=lambda i: shots[i]["started"])
    cost = lambda e: sum(costs.get(r["created"], 0) for r in e)   # noqa: E731  재알림마다 LLM을 다시 부른다
    rows = []
    for n, a in enumerate(shots):
        hit = [e for i, e in enumerate(eps) if owner.get(i) == n]
        r = hit[0][0] if hit else None
        mine = [x for e in hit for x in e]
        # 복구 끝: 운영 AI가 회복을 확인한 시각 (1분마다 확인). 없으면 주입기가 본 시각 (10분마다 돈다)
        healed = [checks[x["id"]]["decided"] for x in mine if (checks.get(x["id"]) or {}).get("state") == "recovered"]
        end = min(healed) if healed else a["ended"]
        expect = EXPECT.get(a["kind"], "?")   # "?": 맞는 원인을 정해 두지 않은 종류 (파드 죽이기 등)
        rows.append({
            **a, "night": _night(a["started"]), "alerts": [e[0]["alertname"] for e in hit],
            "mttd": (r["created"] - a["started"]) / 60 if r else None,
            "mttr": (end - a["started"]) / 60 if end else None,
            # 운영 AI가 조치를 실행했으면 운영 AI가 고친 것 (사람 승인 포함). 아니면 사람 · 다른 배포
            "fixer": None if a["ended_by"] != "fixed" else
            "운영 AI" if any(x["action"] and x["state"] == "executed" for x in mine) else "사람 · 그 밖",
            # LLM 전 기록은 진단이 곧 점검표다
            "expect": expect, "checklist": r["diag"].get("checklist", r["diag"])["cause"] if r else None,
            "llm": _llm(r["diag"]) if r else None,
            "action": r and r["action"], "tier": r and r["tier"], "state": r and r["state"],
            "check": r and (checks.get(r["id"]) or {}).get("result"),
            "cost": sum(cost(e) for e in hit)})
    near = lambda r: any(0 <= r["created"] - d <= AFTER_DEPLOY.total_seconds() for d in deploys)   # noqa: E731
    stray = [{"alertname": e[0]["alertname"], "created": e[0]["created"], "deploy": near(e[0]), "cost": cost(e)}
             for i, e in enumerate(eps) if i not in owner]
    return rows, stray, dropped


def _rate(hits, n):
    return f"{hits}/{n} ({hits / n:.0%})" if n else "-"


def _mid(xs):
    xs = [x for x in xs if x is not None]
    return f"{statistics.median(xs):.0f}분 (n={len(xs)})" if xs else "-"


def summary(rows, stray):
    """숫자 한 묶음: 탐지율 · MTTD · 오탐 · MTTR 낮 / 밤 · 점검표 대 LLM · 건당 LLM 비용."""
    # 경보가 나야 하는 종류 = 맞는 원인이 정해진 종류. 파드 종료는 k8s가 경보 전에 되살리는 게 정상이라 따로 센다
    want = [r for r in rows if r["kind"] in EXPECT]
    calm = [r for r in rows if r["kind"] not in EXPECT]
    seen = [r for r in want if r["alerts"]]
    llm = [r for r in seen if r["llm"] not in ("없음", "실패")]
    alerts = sum(len(r["alerts"]) for r in rows) + len(stray)
    cost = sum(r["cost"] for r in rows) + sum(s["cost"] for s in stray)
    # 복구 시간은 조치해야 풀리는 종류로만 잰다. 저절로 끝나는 종류의 길이는 주입기가 정한 값이다
    fix = [r for r in rows if r["kind"] in NEEDS_ACTION]
    fixed = [r for r in fix if r["ended_by"] == "fixed"]
    return {
        "주입": str(len(rows)), "탐지율": _rate(len(seen), len(want)), "탐지까지 (중앙값)": _mid(r["mttd"] for r in seen),
        "경보 없이 회복 (파드 종료)": _rate(sum(not r["alerts"] for r in calm), len(calm)),
        "주입 없는 경보": f"{sum(not s['deploy'] for s in stray)} (배포 구간 {sum(s['deploy'] for s in stray)} 따로)",
        "조치로 풀림": _rate(len(fixed), len(fix)) + (f" · 운영 AI {sum(r['fixer'] == '운영 AI' for r in fixed)}" if fix else ""),
        "복구까지 낮 (중앙값)": _mid(r["mttr"] for r in fixed if not r["night"]),
        "복구까지 밤 (중앙값)": _mid(r["mttr"] for r in fixed if r["night"]),
        "점검표 원인 맞음": _rate(sum(r["checklist"] == r["expect"] for r in seen), len(seen)),
        "LLM 원인 맞음": _rate(sum(r["llm"] == r["expect"] for r in llm), len(llm)) + f" · 실패 {sum(r['llm'] == '실패' for r in seen)}",
        "LLM 비용": f"${cost:.4f} · 경보당 ${cost / alerts:.4f}" if alerts else "-"}


def _table(head, cols, first=()):
    """열 = (이름, 주입 줄, 주입 없는 경보), 행 = summary 칸. first: 맨 위에 더 붙일 (이름, 칸마다 값)."""
    out = [f"| {head} | " + " | ".join(c[0] for c in cols) + " |", "|---" * (len(cols) + 1) + "|"]
    out += [f"| {k} | " + " | ".join(v) + " |" for k, v in first]
    out += [f"| {k} | " + " | ".join(summary(c[1], c[2])[k] for c in cols) + " |" for k in summary([], [])]
    return out


def report(rows, stray, dropped, since, until, splits):
    """마크다운: 전체 · 구간별 표 + 종류별 표 + 주입마다 한 줄 + 주입 없는 경보. splits = [(시각, 바뀐 것)]."""
    t = lambda ts: datetime.fromtimestamp(ts, KST).strftime("%m-%d %H:%M")   # noqa: E731
    marks = [(since, "시작")] + [(ts, name) for ts, name in sorted(splits) if since < ts < until]
    cuts = [(start, end, name) for (start, name), (end, _) in zip(marks, marks[1:] + [(until, "")])]
    cols = [("전체", rows, stray)] + [(f"{name} ({t(start)} ~)", [r for r in rows if start <= r["started"] < end],
                                      [s for s in stray if start <= s["created"] < end]) for start, end, name in cuts]
    days = [f"{(until - since) / 86400:.1f}일"] + [f"{(end - start) / 86400:.1f}일" for start, end, _ in cuts]
    out = _table("", cols, [("기간", days)])   # 주입 없는 경보처럼 기간에 따라 느는 숫자는 기간으로 나눠 읽는다
    kinds = sorted({r["kind"] for r in rows})
    out += [""] + _table("종류", [(k, [r for r in rows if r["kind"] == k], []) for k in kinds])
    out += ["", "| 주입 | 종류 | 낮밤 | 경보 | 탐지 | 정답 원인 | 점검표 | LLM | 조치 | 확인 | 끝 | 복구 | LLM 비용 |", "|---" * 13 + "|"]
    minutes = lambda m: "-" if m is None else f"{m:.0f}분"   # noqa: E731
    for r in rows:
        def said(got):   # 원인 None = "모른다"는 판단. 정답이 None인 종류에선 그게 맞다
            if not r["alerts"]:
                return "-"
            mark = "" if r["expect"] == "?" or got in ("없음", "실패") else " ✓" if got == r["expect"] else " ✗"
            return f"{got or '모름'}{mark}"
        act = " ".join(x for x in (r["action"], r["tier"], r["state"]) if x) or "-"
        ended = (r["ended_by"] or "진행 중") + (f" ({r['fixer']})" if r["fixer"] else "")
        out.append(f"| {t(r['started'])} | {r['kind']} | {'밤' if r['night'] else '낮'} | {', '.join(r['alerts']) or '없음'} | "
                   f"{minutes(r['mttd'])} | {r['expect'] or '모름'} | {said(r['checklist'])} | {said(r['llm'])} | {act} | "
                   f"{r['check'] or '-'} | {ended} | {minutes(r['mttr'])} | ${r['cost']:.4f} |")
    out += ["", f"주입 없는 경보 {len(stray)}건" + "".join(f"\n- {t(s['created'])} {s['alertname']}{' (배포 구간)' if s['deploy'] else ''}"
                                                       for s in stray)]
    out += ["", "뺀 것: " + " · ".join(f"{k} {v}" for k, v in dropped.items())]
    return "\n".join(out)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("answers")
    p.add_argument("opsai")
    p.add_argument("--since", default="2026-10-24T17:31:55+09:00")   # 구간 2 (장애 실험) 시작
    p.add_argument("--until", default="2026-11-20T17:01:00+09:00")
    p.add_argument("--deploy", action="append", default=[])
    p.add_argument("--exclude", nargs=2, action="append", default=[], metavar=("START", "END"))
    p.add_argument("--split", action="append", default=[])
    p.add_argument("--out")
    a = p.parse_args()
    ts = lambda s: datetime.fromisoformat(s).timestamp()   # noqa: E731
    since, until = ts(a.since), ts(a.until)
    splits = [(ts(s), "") for s in a.split] or [(ts(s), name) for s, name in PHASES]
    rows, stray, dropped = grade(load_answers(a.answers), *load_ops(a.opsai), since, until,
                                 [ts(d) for d in a.deploy], [(ts(s), ts(e)) for s, e in a.exclude])
    text = report(rows, stray, dropped, since, until, splits)
    if a.out:
        open(a.out, "w", encoding="utf-8").write(text + "\n")
    sys.stdout.reconfigure(encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
