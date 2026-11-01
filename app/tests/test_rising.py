from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from banjjak import db, main
from banjjak.analyze import METHOD
from banjjak.collect import PER_PAGE, collect_once
from banjjak.main import app
from banjjak.rising import candidates, entrants, rising, save, sweep
from banjjak.trending import latest_capture

NOW = datetime(2026, 11, 7, 15, 40, tzinfo=timezone.utc)
DAY = timedelta(days=1)
client = TestClient(app)


def item(id, stars, name=None, created="2020-01-01T00:00:00Z"):
    name = name or f"o/r{id}"
    return {"id": id, "full_name": name, "html_url": f"https://github.com/{name}", "description": f"{name} desc",
            "language": "Go", "stargazers_count": stars, "created_at": created}


def fake_github(repos):
    """별 범위와 쪽을 진짜처럼 처리하는 가짜 검색: 별 많은 순, 한 질의에 1,000개까지만."""
    calls = []

    def search(query, page):
        calls.append(query)
        band = query.split("stars:")[1]
        low, top = band.split("..") if ".." in band else (band.removeprefix(">="), None)
        hits = sorted((r for r in repos if int(low) <= r["stargazers_count"] and (top is None or r["stargazers_count"] <= int(top))),
                      key=lambda r: -r["stargazers_count"])
        return {"items": hits[:1000][(page - 1) * PER_PAGE:page * PER_PAGE], "incomplete_results": False}

    search.calls = calls
    return search


def record(at, *items):
    with db.engine().begin() as conn:
        save(conn, {i["id"]: i for i in items}, at)


def test_sweep_walks_past_the_1000_result_limit():
    repos = [item(i, 50_000 - i // 3) for i in range(2500)]   # 같은 별 수가 경계에 걸려도 빠지지 않는다
    search = fake_github(repos)
    found = sweep(search, "", 20_000)
    assert found.keys() == {r["id"] for r in repos}
    assert search.calls[0] == "stars:>=20000"
    assert search.calls[10] == f"stars:20000..{repos[999]['stargazers_count']}"


def test_candidates_are_big_repos_and_recent_risers():
    calls = fake_github([item(1, 30_000), item(2, 1_500)])
    assert candidates(calls, NOW).keys() == {1, 2}
    assert sorted(set(calls.calls)) == ["created:>=2026-07-10 stars:>=1000", "stars:>=20000"]


def test_rising_ranks_by_stars_gained_since_the_last_record():
    record(NOW - 2 * DAY, item(1, 100), item(2, 100))
    record(NOW - DAY, item(1, 1_000), item(2, 50_000), item(4, 5_000))   # 4번은 오늘 빠졌다
    record(NOW, item(1, 1_800), item(2, 50_200), item(3, 2_100))         # 3번은 오늘 들어와서 비교할 기록이 없다
    with db.engine().connect() as conn:
        updated, rows = rising(conn)
    assert updated == NOW
    assert [(r["rank"], r["full_name"], r["gained"], r["days"]) for r in rows] == [(1, "o/r1", 800, 1), (2, "o/r2", 200, 1)]


def test_a_missed_day_is_counted_as_a_longer_span():
    record(NOW - 3 * DAY, item(1, 100))
    record(NOW, item(1, 400))
    with db.engine().connect() as conn:
        assert rising(conn)[1][0]["days"] == 3


def test_entrants_are_repos_that_were_not_in_yesterdays_ranking():
    record(NOW - 2 * DAY, item(1, 100))
    record(NOW - DAY, item(1, 200), item(2, 100))
    with db.engine().connect() as conn:
        assert entrants(conn, *rising(conn)) == set()              # 어제 순위를 매길 기록이 아직 없다
    record(NOW, item(1, 300), item(2, 900))
    with db.engine().connect() as conn:
        assert entrants(conn, *rising(conn)) == {2}
        assert entrants(conn, None, []) == set()


def test_first_record_has_nothing_to_compare_yet():
    record(NOW, item(1, 30_000))
    with db.engine().connect() as conn:
        assert rising(conn) == (NOW, [])
    r = client.get("/?view=rising")
    assert r.status_code == 200
    assert "기록을 쌓는 중" in r.text


def test_daily_records_stay_out_of_the_hourly_ranking():
    # 탭 기록이 신선도(latest_capture)를 밀면 수집기가 멈춰도 데이터가 새것처럼 보인다
    with db.engine().begin() as conn:
        collect_once(lambda q, p: {"items": [item(1, 500)] if p == 1 else []}, conn, NOW - 3 * DAY)
    record(NOW, item(2, 30_000))
    with db.engine().connect() as conn:
        assert latest_capture(conn) == NOW - 3 * DAY


def test_title_matches_the_other_tab_unless_a_day_was_missed():
    record(NOW - DAY, item(1, 30_000))
    record(NOW, item(1, 30_900))
    tab = client.get("/?view=rising").text
    assert "하루 별 증가 순위" in tab and "최근 하루 동안" in tab and "하루 <b>+900</b>" in tab
    record(NOW + 2 * DAY, item(1, 31_000))
    main._cache.clear()
    tab = client.get("/?view=rising").text
    assert "2일 별 증가 순위" in tab and "최근 2일 동안" in tab and "2일 <b>+100</b>" in tab


def test_tabs_say_who_they_cover():
    for path in ("/", "/?view=rising"):
        tab = client.get(path).text
        assert "30일 안에 생긴 레포만" in tab and "오래된 레포는 별 2만부터" in tab
    assert '<a href="/?view=rising" aria-current="page">전체 레포' in client.get("/?view=rising").text
    assert '<a href="/" aria-current="page">새로 생긴 레포' in client.get("/").text


def test_panel_on_the_tab_lists_todays_entrants_with_what_they_are():
    record(NOW - 2 * DAY, item(1, 30_000, "x/old"))
    record(NOW - DAY, item(1, 30_100, "x/old"), item(2, 30_000, "x/art"))
    record(NOW, item(1, 30_200, "x/old"), item(2, 30_900, "x/art"))
    panel = client.get("/?view=rising").text.split("<aside")[1]
    assert "오늘 새로 든 레포" in panel and 'href="#r1">x/art' in panel and "x/old" not in panel
    assert "1위 · 하루 +900" in panel and "x/art desc" in panel


def test_tab_filters_by_field_and_stays_on_the_tab():
    record(NOW - 2 * DAY, item(1, 30_000, "x/tool"), item(2, 30_000, "x/art"))
    record(NOW, item(1, 30_900, "x/tool"), item(2, 30_100, "x/art"))
    with db.engine().begin() as conn:
        conn.execute(db.analyses.insert(), [{"repo_id": i, "method": METHOD, "analyzed_at": NOW, "checked_at": NOW, "ok": True,
                                             "category": c, "readme_sha": "s"} for i, c in ((1, "dev-tools"), (2, "creative"))])
    tab = client.get("/?view=rising&field=dev-tools").text
    assert "x/tool" in tab and "x/art" not in tab
    assert '<a href="/?view=rising&amp;field=dev-tools" aria-current="page">' in tab
    assert '<a href="/?view=rising&amp;field=creative">' in tab and '<a href="/?view=rising">전체<span' in tab
    none = client.get("/?view=rising&field=security").text
    assert "이 분야 레포가 없어요" in none and '<a href="/?view=rising">전체 보기</a>' in none
    client.get("/?view=rising&field=<script>")
    assert sorted(main._cache) == ["rising", "rising:dev-tools", "rising:security"]   # 캐시 칸은 아는 분야만큼


def test_tab_shows_the_ranking_and_only_known_views_are_taken():
    record(NOW - 2 * DAY, item(1, 30_000, "x/old"))
    record(NOW, item(1, 30_900, "x/old"))
    tab = client.get("/?view=rising").text
    assert "x/old" in tab and "<b>+900</b>" in tab and "2일" in tab
    assert "등장" not in tab   # 첫 분석이 11-01로 몰린 탭이라 며칠째가 뜻이 없다
    assert '<a href="/" aria-current="page">새로 생긴 레포' in client.get("/?view=nope").text   # 모르는 값은 기본 화면 → 캐시 칸이 늘지 않는다
