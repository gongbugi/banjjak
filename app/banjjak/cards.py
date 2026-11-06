"""카드: 분석 결과를 순위에 붙이고, 이번 주 분야별 집계를 낸다. 숫자는 전부 코드가 DB에서 계산한다."""
from datetime import timedelta, timezone

from sqlalchemy import func, select, union_all
from sqlalchemy.dialects.postgresql import distinct_on

from banjjak import db
from banjjak.analyze import AI_METHOD, CATEGORY_NAMES, METHOD, NOTE_NAMES
from banjjak.collect import WINDOW_DAYS

KST = timezone(timedelta(hours=9))   # 한국은 서머타임이 없어 고정 오프셋으로 충분하다
RELATED = 3


def latest_ok(conn, repo_ids, method=METHOD):
    a = db.analyses
    rows = conn.execute(select(a).where(a.c.repo_id.in_(repo_ids), a.c.method == method, a.c.ok)
                        .order_by(a.c.repo_id, a.c.analyzed_at.desc()).ext(distinct_on(a.c.repo_id))).mappings()
    return {r["repo_id"]: r for r in rows}


def _shown(conn, repo_ids):
    """화면에 낼 행: 코드판의 최신 README를 AI판이 통과해 있으면 AI판, 아니면 코드판."""
    ai = latest_ok(conn, repo_ids, AI_METHOD)
    return {k: ai[k] if k in ai and ai[k]["readme_sha"] == r["readme_sha"] else r for k, r in latest_ok(conn, repo_ids).items()}


def attach_cards(conn, items):
    """순위 항목마다 card(없으면 None)와 같은 분야의 다른 순위 레포(related)를 붙인다."""
    latest = _shown(conn, [i["id"] for i in items])
    for i in items:
        r = latest.get(i["id"])
        i["card"] = None if r is None else {
            "category": r["category"], "category_name": CATEGORY_NAMES[r["category"]], "usage": r["usage"],
            "notes": [{"kind": n["kind"], "name": NOTE_NAMES[n["kind"]], "quote": n["quote"]} for n in r["notes"]],
            "summary": r["summary"],
        }
    for i in items:
        cat = i["card"] and i["card"]["category"]
        i["related"] = [] if cat in (None, "unclassified") else [
            j["full_name"] for j in items if j is not i and j["card"] and j["card"]["category"] == cat][:RELATED]
    return items


def attach_entry(conn, items, now):
    """순위 항목마다 순위에 처음 든 지 며칠째인지(entry_day)를 붙인다. 처음 분석된 때가 '새로 든 때'다. 분석 전이면 방금 든 것이다."""
    # ponytail: 빠졌다 돌아와도 첫 등장부터 세고, 기록은 2026-10-21부터라 그전 레포는 실제보다 짧다. 연속 일수는 순위 이력을 남겨야 한다
    a = db.analyses
    first = dict(conn.execute(select(a.c.repo_id, func.min(a.c.analyzed_at))
                              .where(a.c.repo_id.in_([i["id"] for i in items]), a.c.method == METHOD).group_by(a.c.repo_id)).all())
    for i in items:
        i["entry_day"] = (now - first.get(i["id"], now)).days + 1
    return items


def week_start(now):
    """이번 주 월요일 0시 (한국 시간)."""
    local = now.astimezone(KST)
    return (local - timedelta(days=local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)


def weekly_tally(conn, now):
    """분야별로 이번 주 · 지난주에 순위에 새로 든 레포 수. 처음 분석된 때가 '새로 든 때'다."""
    a = db.analyses
    this_start = week_start(now)
    last_start = this_start - timedelta(days=7)
    # ponytail: 전체 레포 탭의 레포도 분석되므로, 새로 생긴 레포 순위에 들 수 있는 나이로 가른다. 전체 레포 탭에만 든 신생 레포도
    # 세지만 2026-11-01 실측은 그런 레포가 0개였다 (30일 이내 12개가 모두 두 탭에 겹침). 정확히 하려면 순위 이력을 남겨야 한다
    r = db.repos
    first = (select(a.c.repo_id, func.min(a.c.analyzed_at).label("first")).join(r, r.c.id == a.c.repo_id)
             .where(a.c.method == METHOD).group_by(a.c.repo_id, r.c.created_at)
             .having(func.min(a.c.analyzed_at) <= r.c.created_at + timedelta(days=WINDOW_DAYS + 1)).subquery())
    latest = _shown(conn, select(first.c.repo_id).where(first.c.first >= last_start))
    firsts = dict(conn.execute(select(first.c.repo_id, first.c.first).where(first.c.first >= last_start)).all())
    counts = {}   # 분야 → [이번 주, 지난주]
    for repo_id, r in latest.items():
        counts.setdefault(r["category"], [0, 0])[0 if firsts[repo_id] >= this_start else 1] += 1
    return sorted(({"slug": s, "name": CATEGORY_NAMES[s], "this_week": t, "last_week": l}
                   for s, (t, l) in counts.items()),
                  # 미분류는 분야가 아니라 맨 뒤
                  key=lambda x: (x["slug"] == "unclassified", -x["this_week"], -x["last_week"], x["name"]))


def success_ratio(conn, now):
    """최근 24시간 분석 성공 비율. 분석이 없었으면 None (지표를 내지 않는다)."""
    a = db.analyses
    ok, total = conn.execute(select(func.count().filter(a.c.ok), func.count())
                             .where(a.c.method == METHOD, a.c.analyzed_at > now - timedelta(hours=24))).one()
    return ok / total if total else None


def ai_usage(conn, now):
    """카드 AI와 흐름 묶음의 최근 24시간 호출 · 토큰과 비용. 상한과 지표가 함께 본다 (키는 지표 이름). 분석기는 금방 끝나서 DB가 장부다."""
    a, f = db.analyses, db.flows
    calls = union_all(select(a.c.analyzed_at.label("at"), a.c.ok, a.c.tokens, a.c.cost)
                      .where(a.c.method.like("ai-%")),    # 프롬프트 버전을 올려도 쓴 돈은 그대로 센다
                      select(f.c.made_at, f.c.ok, f.c.tokens, f.c.cost)).subquery()
    day = calls.c.at > now - timedelta(hours=24)
    ok, failed, tokens, cost_day, cost = conn.execute(
        select(func.count().filter(day, calls.c.ok), func.count().filter(day, ~calls.c.ok),
               func.coalesce(func.sum(calls.c.tokens).filter(day), 0),
               func.coalesce(func.sum(calls.c.cost).filter(day), 0), func.coalesce(func.sum(calls.c.cost), 0))).one()
    return {"ok": ok, "failed": failed, "tokens": tokens, "cost_day_dollars": cost_day, "cost_dollars": cost}


FLOW_FOR = timedelta(days=2)   # 보여 주는 순위보다 이만큼 묵은 흐름은 안 보인다 (분석기가 멈췄을 때)
FLOW_MIN = 3                   # 레포 이만큼이 함께 떠야 흐름이다 (banjjak.flows도 이 값으로 고른다)


def flows_shown(conn, view, items, gain, fresh, at):
    """그 탭의 최근 흐름 중 지금 순위에 3개 이상 남은 것. 숫자(개수 · 별 합 · 최고 순위 · 새로 든 수)는 지금 순위로 센다.
    key는 저장된 순서라 하루 동안 그대로다 (?flow= 주소). 별 합이 큰 흐름부터."""
    f = db.flows
    groups = conn.execute(select(f.c.groups).where(f.c.view == view, f.c.ok, f.c.made_at > at - FLOW_FOR)
                          .order_by(f.c.made_at.desc()).limit(1)).scalar() or []
    out = []
    for key, g in enumerate(groups, 1):
        ids = set(g["members"])
        members = [i for i in items if i["id"] in ids]
        if len(members) >= FLOW_MIN:
            out.append({"key": str(key), "name": g["name"], "line": g["line"], "repos": members,
                        "gain": sum(i[gain] for i in members), "best": members[0]["rank"],
                        "fresh": sum(i["id"] in fresh for i in members)})
    return sorted(out, key=lambda g: -g["gain"])
