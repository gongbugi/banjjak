from datetime import datetime, timedelta, timezone

from banjjak import db
from banjjak.collect import collect_once
from banjjak.trending import trending

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)


def item(id, name, stars, created):
    return {
        "id": id,
        "full_name": name,
        "html_url": f"https://github.com/{name}",
        "description": f"{name} desc",
        "language": "Python",
        "stargazers_count": stars,
        "created_at": created,
    }


def fake_search(pages):
    """가짜 GitHub 검색. 호출된 쿼리를 기록하고 페이지별 결과를 돌려준다."""
    calls = []

    def search(query, page):
        calls.append((query, page))
        return {"items": pages[page - 1] if page <= len(pages) else []}

    search.calls = calls
    return search


def test_collect_queries_recent_repos_and_records_stars():
    search = fake_search([[item(1, "a/fast", 100, "2026-10-01T00:00:00Z")]])
    with db.engine().begin() as conn:
        assert collect_once(search, conn, NOW) == 1
    assert search.calls[0] == ("created:>=2026-09-21 stars:>=100", 1)


def test_collect_stops_after_a_short_page():
    full = [item(i, f"o/r{i}", 500 - i, "2026-10-10T00:00:00Z") for i in range(100)]
    search = fake_search([full, [item(999, "o/last", 101, "2026-10-10T00:00:00Z")]])
    with db.engine().begin() as conn:
        assert collect_once(search, conn, NOW) == 101
    assert [p for _, p in search.calls] == [1, 2]


def test_trending_ranks_by_stars_gained_per_day():
    created = "2026-10-18T12:00:00Z"
    earlier, later = NOW - timedelta(hours=12), NOW
    with db.engine().begin() as conn:
        collect_once(fake_search([[item(1, "a/fast", 100, created), item(2, "b/big", 1000, created)]]), conn, earlier)
        collect_once(fake_search([[item(1, "a/fast", 148, created), item(2, "b/big", 1012, created),
                                   item(3, "c/new", 300, created)]]), conn, later)
        rows = trending(conn, limit=10)

    assert [(r["full_name"], r["stars_per_day"], r["estimated"]) for r in rows] == [
        ("c/new", 100.0, True),    # 기록 1개뿐 → 생성 후 3일 동안 300개로 추정
        ("a/fast", 96.0, False),   # 12시간에 48개 → 하루 96개
        ("b/big", 24.0, False),    # 12시간에 12개 → 하루 24개
    ]
