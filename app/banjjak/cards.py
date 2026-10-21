"""카드: 분석 결과를 순위에 붙이고, 이번 주 분야별 집계를 낸다. 숫자는 전부 코드가 DB에서 계산한다."""
from datetime import timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import distinct_on

from banjjak import db
from banjjak.analyze import CATEGORY_NAMES, METHOD, NOTE_NAMES

KST = timezone(timedelta(hours=9))   # 한국은 서머타임이 없어 고정 오프셋으로 충분하다
RELATED = 3


def _latest_ok(conn, repo_ids):
    a = db.analyses
    rows = conn.execute(select(a).where(a.c.repo_id.in_(repo_ids), a.c.method == METHOD, a.c.ok)
                        .order_by(a.c.repo_id, a.c.analyzed_at.desc()).ext(distinct_on(a.c.repo_id))).mappings()
    return {r["repo_id"]: r for r in rows}


def attach_cards(conn, items):
    """순위 항목마다 card(없으면 None)와 같은 분야의 다른 순위 레포(related)를 붙인다."""
    latest = _latest_ok(conn, [i["id"] for i in items])
    for i in items:
        r = latest.get(i["id"])
        i["card"] = None if r is None else {
            "category": r["category"], "category_name": CATEGORY_NAMES[r["category"]], "usage": r["usage"],
            "notes": [{"kind": n["kind"], "name": NOTE_NAMES[n["kind"]], "quote": n["quote"]} for n in r["notes"]],
        }
    for i in items:
        cat = i["card"] and i["card"]["category"]
        i["related"] = [] if cat in (None, "unclassified") else [
            j["full_name"] for j in items if j is not i and j["card"] and j["card"]["category"] == cat][:RELATED]
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
    first = select(a.c.repo_id, func.min(a.c.analyzed_at).label("first")).where(a.c.method == METHOD) \
        .group_by(a.c.repo_id).subquery()
    latest = _latest_ok(conn, select(first.c.repo_id).where(first.c.first >= last_start))
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
