from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from prometheus_client import REGISTRY, generate_latest
from sqlalchemy.dialects.postgresql import insert

from banjjak import db, main
from banjjak.analyze import METHOD, NotFound, analyze_once
from banjjak.cards import weekly_tally
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
    readmes = {"a/rocket": {"sha": "s1", "content": ROCKET}, "b/drill": {"sha": "s2", "content": "# drill\nwords\n"}}

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


def _analysis(repo_id, at, category="dev-tools", ok=True):
    return {"repo_id": repo_id, "method": METHOD, "analyzed_at": at, "checked_at": at, "ok": ok,
            "category": category if ok else None, "readme_sha": f"s{repo_id}" if ok else None}


def test_weekly_tally_counts_new_entrants_against_last_week():
    items = [_item(i, f"o/r{i}", [], 100) for i in range(1, 5)]
    with db.engine().begin() as conn:
        collect_once(lambda q, p: {"items": items if p == 1 else []}, conn, NOW)
        conn.execute(insert(db.analyses), [
            _analysis(1, NOW - timedelta(days=1)),                       # 이번 주 (월요일 0시 한국 시간 뒤)
            _analysis(2, NOW - timedelta(days=2), "creative"),           # 이번 주
            _analysis(3, NOW - timedelta(days=5)),                       # 지난주
            _analysis(4, NOW - timedelta(hours=1), ok=False),            # 실패는 분야가 없다
        ])
        tally = {t["slug"]: (t["this_week"], t["last_week"]) for t in weekly_tally(conn, NOW)}
    assert tally == {"dev-tools": (1, 1), "creative": (1, 0)}


def test_success_ratio_metric(monkeypatch):
    monkeypatch.setattr(main, "_now", lambda: NOW)
    assert "banjjak_analysis_success_ratio " not in generate_latest(REGISTRY).decode()   # 분석이 없으면 안 낸다
    _seed_and_analyze()                                                                   # 성공 2 · 실패 1
    assert "banjjak_analysis_success_ratio 0.666" in generate_latest(REGISTRY).decode()
