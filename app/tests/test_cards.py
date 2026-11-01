from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from prometheus_client import REGISTRY, generate_latest
from sqlalchemy.dialects.postgresql import insert

from banjjak import db, main
from banjjak.analyze import METHOD, NotFound, analyze_once
from banjjak.cards import attach_entry, weekly_tally
from banjjak.collect import collect_once

client = TestClient(main.app)
NOW = datetime(2026, 10, 21, 12, 20, tzinfo=timezone.utc)       # 수요일 21:20 (한국 시간)

ROCKET = """# rocket

## Install

```bash
pip install rocket
```

Rocket sends anonymous usage statistics by default.
"""


def _item(id, name, topics, stars):
    return {"id": id, "full_name": name, "html_url": f"https://github.com/{name}", "description": f"{name} desc",
            "language": "Python", "topics": topics, "stargazers_count": stars, "created_at": "2026-10-17T12:00:00Z"}


def _seed_and_analyze():
    items = [_item(1, "a/rocket", ["cli"], 400), _item(2, "b/drill", ["cli"], 300), _item(3, "c/paint", ["svg"], 200)]
    drill = "# drill\n\nCloud sync needs a [paid plan](https://x.dev/pricing).\n"
    readmes = {"a/rocket": {"sha": "s1", "content": ROCKET}, "b/drill": {"sha": "s2", "content": drill}}

    def fetch(name):
        if name not in readmes:
            raise NotFound(name)
        return readmes[name]

    with db.engine().begin() as conn:
        collect_once(lambda q, p: {"items": items if p == 1 else []}, conn, NOW - timedelta(minutes=20))
        assert analyze_once(fetch, conn, NOW) == (2, 1)


def test_api_cards_and_same_field_links():
    _seed_and_analyze()
    items = {i["full_name"]: i for i in client.get("/api/trending").json()["items"]}
    rocket = items["a/rocket"]
    assert rocket["card"]["category_name"] == "개발 도구"
    assert rocket["card"]["usage"] == "pip install rocket"
    assert rocket["card"]["notes"][0]["name"] == "사용 기록 전송"
    assert rocket["related"] == ["b/drill"]               # 같은 분야의 다른 순위 레포
    assert items["c/paint"]["card"] is None               # README가 없으면 카드 없이 순위만


def test_page_shows_card_and_missing_usage():
    _seed_and_analyze()
    html = client.get("/").text
    assert "pip install rocket" in html
    assert "Rocket sends anonymous usage statistics by default." in html
    assert "README에 없음" in html                        # b/drill은 설치 방법이 없다
    assert "개발 도구" in html
    # 인용은 원문 그대로 저장하되, 화면에서는 마크다운 링크를 글자만 보인다
    assert "Cloud sync needs a paid plan." in html and "](https://x.dev" not in html


def test_page_filters_by_field_and_ignores_unknown_ones():
    _seed_and_analyze()
    with db.engine().begin() as conn:
        conn.execute(insert(db.analyses), [_analysis(3, NOW, "creative")])   # c/paint는 다른 분야
    html = client.get("/?field=dev-tools").text
    assert "a/rocket" in html and "b/drill" in html and "c/paint" not in html
    assert '<a href="/?field=dev-tools" aria-current="page">' in html
    everything = client.get("/?field=nope").text
    assert "c/paint" in everything and '<a href="/" aria-current="page">' in everything   # 모르는 분야는 전체로
    client.get("/?field=<script>")
    assert sorted(main._cache) == ["index", "index:dev-tools"]   # 캐시 칸은 아는 분야만큼 (아무 주소로 늘지 않게)


def _analysis(repo_id, at, category="dev-tools", ok=True):
    return {"repo_id": repo_id, "method": METHOD, "analyzed_at": at, "checked_at": at, "ok": ok,
            "category": category if ok else None, "readme_sha": f"s{repo_id}" if ok else None}


def test_weekly_tally_counts_new_entrants_against_last_week():
    items = [_item(i, f"o/r{i}", [], 100) for i in range(1, 7)]
    with db.engine().begin() as conn:
        collect_once(lambda q, p: {"items": items if p == 1 else []}, conn, NOW)
        conn.execute(insert(db.analyses), [
            _analysis(1, NOW - timedelta(days=1)),                       # 이번 주 (월요일 0시 한국 시간 뒤)
            _analysis(2, NOW - timedelta(days=2), "creative"),           # 이번 주
            _analysis(3, NOW - timedelta(days=5)),                       # 지난주
            _analysis(4, NOW - timedelta(hours=1), ok=False),            # 실패는 분야가 없다
        ])
        conn.execute(insert(db.analyses), [_analysis(i, NOW - timedelta(hours=2), "unclassified") for i in (5, 6)])
        tally = [(t["slug"], t["this_week"], t["last_week"]) for t in weekly_tally(conn, NOW)]
    # 미분류는 분야가 아니라 맨 뒤 (가장 많아도)
    assert tally == [("dev-tools", 1, 1), ("creative", 1, 0), ("unclassified", 2, 0)]


def test_weekly_tally_leaves_out_old_repos_only_the_rising_tab_ranked():
    old = {**_item(2, "o/old", [], 30_000), "created_at": "2020-01-01T00:00:00Z"}
    with db.engine().begin() as conn:
        collect_once(lambda q, p: {"items": [_item(1, "o/new", [], 100), old] if p == 1 else []}, conn, NOW)
        conn.execute(insert(db.analyses), [_analysis(1, NOW), _analysis(2, NOW)])
        assert [(t["slug"], t["this_week"]) for t in weekly_tally(conn, NOW)] == [("dev-tools", 1)]


def test_entry_day_counts_from_the_first_analysis():
    items = [_item(i, f"o/r{i}", [], 100) for i in (1, 2, 3)]
    with db.engine().begin() as conn:
        collect_once(lambda q, p: {"items": items if p == 1 else []}, conn, NOW)
        conn.execute(insert(db.analyses), [
            _analysis(1, NOW - timedelta(hours=23)),
            _analysis(2, NOW - timedelta(hours=25)),
            {**_analysis(2, NOW - timedelta(hours=1)), "readme_sha": "new"},   # 다시 분석해도 첫 분석부터 센다
        ])
        got = attach_entry(conn, [{"id": 1}, {"id": 2}, {"id": 3}], NOW)
    assert [i["entry_day"] for i in got] == [1, 2, 1]                          # 3번은 순위에 들었지만 아직 분석 전


def test_panel_lists_todays_entrants_and_rows_show_the_day(monkeypatch):
    monkeypatch.setattr(main, "_now", lambda: NOW)
    items = [_item(1, "o/old", [], 300), _item(2, "o/new", [], 200), _item(3, "o/art", [], 100)]
    with db.engine().begin() as conn:
        collect_once(lambda q, p: {"items": items if p == 1 else []}, conn, NOW)
        conn.execute(insert(db.analyses), [_analysis(1, NOW - timedelta(days=3), "security"), _analysis(2, NOW - timedelta(hours=2)),
                                           _analysis(3, NOW - timedelta(hours=2), "creative")])
    html = client.get("/").text
    panel = html.split("<aside")[1]
    assert 'href="#r2">o/new' in panel and 'href="#r3">o/art' in panel and "o/old" not in panel
    assert '<li class="repo" id="r2"' in html                     # 목록에서 누르면 순위의 그 행으로 간다
    assert "등장 4일째" in html and "오늘 첫 등장" in html
    assert "이번 주 순위에 새로 든 레포" not in html
    assert "o/art" not in client.get("/?field=dev-tools").text    # 분야로 거르면 목록도 그 분야만
    assert "최근 24시간에는 없어요" in client.get("/?field=security").text
    api = client.get("/api/trending").json()
    assert [i["entry_day"] for i in api["items"]] == [4, 1, 1] and api["week"]   # 분야 집계는 API에 남는다


def test_success_ratio_metric(monkeypatch):
    monkeypatch.setattr(main, "_now", lambda: NOW)
    assert "banjjak_analysis_success_ratio " not in generate_latest(REGISTRY).decode()   # 분석이 없으면 안 낸다
    _seed_and_analyze()                                                                   # 성공 2 · 실패 1
    assert "banjjak_analysis_success_ratio 0.666" in generate_latest(REGISTRY).decode()
