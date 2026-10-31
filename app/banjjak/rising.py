"""지금 뜨는 레포 탭: 별 많은 레포와 최근 뜬 레포를 하루 한 번 재서 7일 증가순으로 보여 준다. CronJob이 하루 한 번 돌린다.
수집기(최근 30일 · 매시간)와 표 · CronJob을 나눈다 → 신선도 SLI와 섞이지 않는다 (지표 하나에 원인 하나)."""
import os
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import distinct_on, insert

from banjjak import db
from banjjak.collect import PER_PAGE, github_search, pages, save_repo

BIG_STARS = 20_000
NEW_DAYS, NEW_STARS = 120, 1_000   # 최근 120일 안에 생겼으면 별 1천부터. pushed: 조건은 쓰지 않는다 (코드가 그대로여도 뜰 수 있다)
SEARCH_PAGES = 10                  # 검색은 한 질의에 1,000개까지만 준다
PAUSE = 2.1                        # 검색은 분당 30회까지
WINDOW = timedelta(days=7, minutes=30)   # 재는 시각이 밀려도 7일 전 기록까지 잡는다


def paced(search):
    def slow(query, page):
        time.sleep(PAUSE)
        return search(query, page)
    return slow


def sweep(search, query, low):
    """별 많은 쪽부터 내려간다: 1,000개를 다 받으면 그 마지막 별 수를 새 상한으로 다시 묻는다. 겹치는 레포는 id로 합친다."""
    found, top = {}, None
    while True:
        stars = f"{low}..{top}" if top else f">={low}"
        got = [it for items in pages(search, f"{query}stars:{stars}", SEARCH_PAGES) for it in items]
        found |= {it["id"]: it for it in got}
        lowest = min(it["stargazers_count"] for it in got)
        if len(got) < SEARCH_PAGES * PER_PAGE:
            return found
        if lowest == top:
            raise RuntimeError(f"별 {top}개인 레포가 1,000개를 넘어 더 내려갈 수 없다")
        top = lowest


def candidates(search, now):
    since = (now - timedelta(days=NEW_DAYS)).date().isoformat()
    return sweep(search, "", BIG_STARS) | sweep(search, f"created:>={since} ", NEW_STARS)


def save(conn, found, now):
    for it in found.values():
        save_repo(conn, it, now)
        conn.execute(insert(db.star_daily).values(repo_id=it["id"], captured_at=now, stars=it["stargazers_count"]))


def rising(conn, limit=50):
    """(기록 시각, 순위). 레포마다 7일 안의 가장 이른 기록과 비교한다 → 쌓인 날이 적으면 그만큼만 (실제 값은 최솟값)."""
    s, r = db.star_daily, db.repos
    latest = conn.execute(select(func.max(s.c.captured_at))).scalar()
    if latest is None:
        return None, []
    first = (select(s).ext(distinct_on(s.c.repo_id)).where(s.c.captured_at >= latest - WINDOW)
             .order_by(s.c.repo_id, s.c.captured_at).subquery())
    gained = (s.c.stars - first.c.stars).label("gained")
    rows = conn.execute(
        select(r, s.c.stars, first.c.captured_at.label("since"), gained).select_from(r)
        .join(s, s.c.repo_id == r.c.id)
        .join(first, first.c.repo_id == r.c.id)
        .where(s.c.captured_at == latest, first.c.captured_at < latest)
        .order_by(gained.desc(), r.c.id).limit(limit)
    ).mappings()
    return latest, [{
        "rank": i, "full_name": row["full_name"], "url": row["html_url"], "description": row["description"],
        "language": row["language"], "stars": row["stars"], "gained": row["gained"],
        "days": max(round((latest - row["since"]).total_seconds() / 86400), 1),
    } for i, row in enumerate(rows, 1)]


def main():
    now = datetime.now(timezone.utc)
    found = candidates(paced(github_search(os.environ["GITHUB_TOKEN"])), now)   # 트랜잭션을 몇 분씩 열어 두지 않게 검색부터
    with db.engine().begin() as conn:
        save(conn, found, now)
    print(f"recorded {len(found)} repos")


if __name__ == "__main__":
    main()
