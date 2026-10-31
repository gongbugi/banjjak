"""카드 AI 평가 (#19): 고정 표본을 코드판과 같은 README로 돌려 #5의 잣대로 나란히 본다. 돈이 든다 (호출 35번, 약 $0.04).

사용 (레포 루트, 키는 deploy/card_eval.ps1이 넣는다): powershell -ExecutionPolicy Bypass -File .\\deploy\\card_eval.ps1
읽는 것: drill-out/card-eval/ sample.json · answers.json · export.json · trending.json (git 제외, 이 PC에만)
남는 것: drill-out/card-eval/ result-<방식>.json · summary-<방식>.txt. 집계만 찍는다: 프롬프트를 쓰는 쪽이 레포별 정답을 보면 정답지에 맞춰 고치게 된다.
운영 DB에 쓰지 않고 상한 장부에도 들어가지 않는다. 표본이 작아 정확도 오차가 약 ±16%p라 층을 합치지 않는다.
"""
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, "app")
from banjjak import cardai, llm              # noqa: E402
from banjjak.analyze import AI_METHOD        # noqa: E402

BASE = Path("drill-out/card-eval")
WON = 1400       # 원/달러 어림
DAILY = 40       # 하루 분석 수 (10-28 실측 26~53)


def load(name):
    return json.loads((BASE / name).read_text(encoding="utf-8"))


def _line(rows, answers, pick):
    answered = [r for r in rows if r["id"] in answers]
    return {"분류": sum(pick(r) != "unclassified" for r in rows),
            "맞음": sum(pick(r) == answers[r["id"]]["category"] for r in answered),
            "위한 도구 오분류": sum((pick(r) == "ai-coding") != answers[r["id"]]["for_agent"] for r in answered)}


def score(rows, answers):
    """한 층의 잣대. AI가 실패한 레포는 화면처럼 코드판으로 친다."""
    return {"레포": len(rows), "정답": sum(r["id"] in answers for r in rows), "AI 통과": sum(r["ai"]["ok"] for r in rows),
            "코드판": _line(rows, answers, lambda r: r["code"]["category"]),
            "AI판": _line(rows, answers, lambda r: r["ai"]["category"] if r["ai"]["ok"] else r["code"]["category"])}


def sentences(rows, worth):
    """알아둘 점: 사람이 채점한 문장 중 화면에 남은 것. worth = {레포: {문장 번호: yes | no}}"""
    shown, kept_yes, kept, dropped_yes = 0, 0, 0, 0
    for r in rows:
        ai = {n["quote"] for n in r["ai"]["notes"]} if r["ai"]["ok"] else {n["quote"] for n in r["code"]["notes"]}
        for index, said in worth.get(r["id"], {}).items():
            shown += 1
            stays, yes = r["code"]["notes"][int(index)]["quote"] in ai, said == "yes"
            kept += stays
            kept_yes += stays and yes
            dropped_yes += yes and not stays
    return {"문장": shown, "코드판 yes": kept_yes + dropped_yes, "AI 남김": kept, "AI 남김 yes": kept_yes, "AI가 뺀 yes": dropped_yes}


def targets(sample, key, export, ranking):
    """표본 30개 + 문장만 채점한 나머지 레포. 나머지는 분야 잣대에 넣지 않는다."""
    rows = [{"id": str(i["id"]), "repo": i, "readme": i["readme"], "code": i["code"], "sampled": True} for i in sample["items"]]
    for repo_id in key["extra_notes"]:
        e = export[repo_id]
        rows.append({"id": repo_id, "repo": e, "readme": e["content"], "sampled": False,
                     "code": {"category": e["category"], "usage": e["usage"], "notes": ranking[repo_id]["card"]["notes"]}})
    return rows


def main():
    sample, key = load("sample.json"), load("answers.json")
    rows = targets(sample, key, {str(r["id"]): r for r in load("export.json")}, {str(i["id"]): i for i in load("trending.json")["items"]})
    client = llm.client()
    for n, r in enumerate(rows, 1):
        r["ai"] = cardai.enrich(client, r["repo"], r["readme"], r["code"])
        print(f"{n}/{len(rows)}", "통과" if r["ai"]["ok"] else "실패", flush=True)
    path = BASE / f"result-{AI_METHOD}.json"
    path.write_text(json.dumps([{k: r[k] for k in ("id", "sampled", "ai")} for r in rows], ensure_ascii=False, indent=1), encoding="utf-8")

    picked = [r for r in rows if r["sampled"]]
    worth = {**{k: a["notes"] for k, a in key["answers"].items()}, **key["extra_notes"]}
    cost, tokens = sum(r["ai"]["cost"] for r in rows), sum(r["ai"]["tokens"] for r in rows)
    report = "\n".join([
        f"{AI_METHOD} · 모델 {cardai.MODEL} · effort {cardai.EFFORT}",
        f"커버율 (관문 통과): {sum(r['ai']['ok'] for r in rows)}/{len(rows)}",
        f"실패 이유: {dict(Counter(r['ai']['error'].split(':')[1].strip()[:40] for r in rows if not r['ai']['ok']))}",
        f"코드판 미분류: {score([r for r in picked if r['code']['category'] == 'unclassified'], key['answers'])}",
        f"코드판 분류됨: {score([r for r in picked if r['code']['category'] != 'unclassified'], key['answers'])}",
        f"알아둘 점: {sentences(rows, worth)}",
        f"비용: 합계 ${cost:.4f} · 카드당 {tokens / len(rows):,.0f}토큰 · ${cost / len(rows):.5f} · {cost / len(rows) * WON:.1f}원"
        f" · 하루 {DAILY}건이면 월 {cost / len(rows) * DAILY * 30 * WON:,.0f}원",
        f"레포별 결과: {path} (재채점을 마치기 전에는 열지 않는다)"])
    print("\n" + report)
    (BASE / f"summary-{AI_METHOD}.txt").write_text(report, encoding="utf-8")


if __name__ == "__main__":
    main()
