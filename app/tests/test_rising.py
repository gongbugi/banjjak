from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from banjjak import db
from banjjak.collect import PER_PAGE, collect_once
from banjjak.main import app
from banjjak.rising import candidates, rising, save, sweep
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


def test_rising_ranks_by_stars_gained_over_the_week():
    record(NOW - 8 * DAY, item(1, 100), item(2, 100))           # 창 밖
    record(NOW - 7 * DAY, item(1, 1_000), item(2, 50_000))
    record(NOW - 2 * DAY, item(1, 1_500), item(2, 50_100), item(3, 2_000))   # 3번은 이틀 전에 들어왔다
    record(NOW - DAY, item(4, 5_000))                           # 어제까지 있다가 빠졌다
    record(NOW, item(1, 1_800), item(2, 50_200), item(3, 2_100))
    with db.engine().connect() as conn:
        updated, rows = rising(conn)
    assert updated == NOW
    assert [(r["rank"], r["full_name"], r["gained"], r["days"]) for r in rows] == [
        (1, "o/r1", 800, 7),
        (2, "o/r2", 200, 7),
        (3, "o/r3", 100, 2),
    ]


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


def test_tab_shows_the_ranking_and_only_known_views_are_taken():
    record(NOW - 2 * DAY, item(1, 30_000, "x/old"))
    record(NOW, item(1, 30_900, "x/old"))
    tab = client.get("/?view=rising").text
    assert "x/old" in tab and "<b>+900</b>" in tab and "2일" in tab
    assert "이번 주 순위에 새로 든 레포" not in tab
    assert "최근 30일 안에 생긴 레포" in client.get("/?view=nope").text   # 모르는 값은 기본 화면 → 캐시 칸이 늘지 않는다
