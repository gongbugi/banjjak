from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from banjjak import db
from banjjak.analyze import METHOD, NotFound, analyze_once, categorize, extract_notes, extract_usage
from banjjak.collect import collect_once

NOW = datetime(2026, 10, 21, 12, 20, tzinfo=timezone.utc)

README = """# rocket

[![build](https://img.shields.io/badge/x.svg)](https://x)

Fast rockets for your terminal.

## Features

```python
# 이건 기능 예시라 사용법이 아니다
rocket.fly()
```

## Installation

Requires Python 3.12.

```bash
# 주석 줄은 제목이 아니다
pip install rocket
```

## Telemetry

Rocket collects anonymous usage statistics by default. Set ROCKET_NO_TELEMETRY=1 to opt out.

## License

Free for non-commercial use only.
"""


# --- 사용법: 설치 · 사용법 제목 아래 첫 코드 블록을 글자 그대로

def test_usage_is_first_code_block_under_install_heading():
    assert extract_usage(README) == "# 주석 줄은 제목이 아니다\npip install rocket"


def test_usage_none_when_no_install_section():
    assert extract_usage("# x\n\nJust words.\n\n```\nnot under install\n```\n") is None


def test_usage_ignores_code_after_the_section_ends():
    readme = "## Install\n\nSee the docs.\n\n## Other\n\n```\nnope\n```\n"
    assert extract_usage(readme) is None


def test_usage_heading_in_korean_and_chinese():
    assert extract_usage("## 설치\n\n```sh\nbrew install a\n```\n") == "brew install a"
    assert extract_usage("## 快速开始\n\n~~~\nnpx b\n~~~\n") == "npx b"


# --- 분야: 토픽 · 이름 · 설명 키워드 → 큰 분야 9개 (모르면 미분류)

def test_category_from_topics_and_keywords():
    assert categorize("a/skills", "Agent skills for Claude Code", []) == "ai-coding"
    assert categorize("a/scan", "", ["security"]) == "security"
    assert categorize("a/motion", "Motion graphics in the browser", []) == "creative"
    assert categorize("a/x", "", []) == "unclassified"


def test_category_ignores_what_it_was_built_with():
    # "Claude Code로 만든" 앱은 AI 코딩 도구가 아니다
    assert categorize("a/tv", "A fast home screen for Google TV, built with Claude Code", []) != "ai-coding"


# --- 알아둘 점: 정해진 키워드가 든 README 문장을 그대로 인용

def test_notes_quote_readme_sentences():
    notes = {n["kind"]: n["quote"] for n in extract_notes(README)}
    assert notes["telemetry"] == "Rocket collects anonymous usage statistics by default."
    assert notes["license"] == "Free for non-commercial use only."
    for quote in notes.values():
        assert quote in README          # 인용은 README 원문에 그대로 있다


def test_notes_skip_what_is_not_a_warning():
    # 실측(운영 첫 분석 50개)에서 많던 오탐: 부정문 · 보안 신고 안내 · "proprietary" · 목차 링크
    readme = ("It collects no usage data and sends no telemetry.\n\n"
              "To report a security issue, see SECURITY.md.\n\n"
              "No proprietary code or assets.\n\n"
              "[Unauthorized commercial use](#unauthorized-commercial-use)\n")
    assert extract_notes(readme) == []


def test_notes_skip_code_and_empty_when_nothing():
    assert extract_notes("```\ntelemetry = off\n```\n\nNothing to see.") == []


# --- 분석 1회 실행 (테스트 문 3)

def _seed(full_name="x/rocket", repo_id=7, topics=("cli",)):
    item = {"id": repo_id, "full_name": full_name, "html_url": f"https://github.com/{full_name}",
            "description": "Fast rockets for your terminal", "language": "Python", "topics": list(topics),
            "stargazers_count": 420, "created_at": "2026-10-17T12:00:00Z"}
    with db.engine().begin() as conn:
        collect_once(lambda q, p: {"items": [item] if p == 1 else []}, conn, NOW - timedelta(minutes=20))


def fake_fetch(readmes):
    calls = []

    def fetch(full_name):
        calls.append(full_name)
        r = readmes.get(full_name)
        if r is None:
            raise NotFound(full_name)
        return r

    fetch.calls = calls
    return fetch


def _rows():
    with db.engine().connect() as conn:
        return conn.execute(select(db.analyses).order_by(db.analyses.c.id)).mappings().all()


def test_analyze_new_repo_and_keep_readme():
    _seed()
    fetch = fake_fetch({"x/rocket": {"sha": "s1", "content": README}})
    with db.engine().begin() as conn:
        assert analyze_once(fetch, conn, NOW) == (1, 0)
    [row] = _rows()
    assert (row["method"], row["ok"], row["readme_sha"], row["category"]) == (METHOD, True, "s1", "dev-tools")
    assert row["usage"] == "# 주석 줄은 제목이 아니다\npip install rocket"
    with db.engine().connect() as conn:
        assert conn.execute(select(db.readmes.c.content)).scalar() == README   # AI판도 같은 원문으로 돌린다


def test_recheck_readme_at_most_once_a_day():
    _seed()
    fetch = fake_fetch({"x/rocket": {"sha": "s1", "content": README}})
    with db.engine().begin() as conn:
        analyze_once(fetch, conn, NOW)
        assert analyze_once(fetch, conn, NOW + timedelta(hours=1)) == (0, 0)      # 하루 안: 다시 안 본다
        assert analyze_once(fetch, conn, NOW + timedelta(days=1)) == (0, 0)       # 하루 뒤: 봤지만 그대로
    assert len(fetch.calls) == 2 and len(_rows()) == 1
    assert _rows()[0]["checked_at"] == NOW + timedelta(days=1)

    changed = fake_fetch({"x/rocket": {"sha": "s2", "content": README + "\n## Usage\n"}})
    with db.engine().begin() as conn:
        assert analyze_once(changed, conn, NOW + timedelta(days=2, hours=1)) == (1, 0)   # 바뀌었으면 새 줄
    assert [r["readme_sha"] for r in _rows()] == ["s1", "s2"]


def test_missing_readme_is_a_failure_not_a_card():
    _seed()
    with db.engine().begin() as conn:
        assert analyze_once(fake_fetch({}), conn, NOW) == (0, 1)
    [row] = _rows()
    assert (row["ok"], row["category"], row["readme_sha"]) == (False, None, None)
    assert "README" in row["error"]


def test_only_ranked_repos_are_analyzed():
    with db.engine().begin() as conn:
        assert analyze_once(fake_fetch({}), conn, NOW) == (0, 0)    # 순위가 비면 할 일이 없다
        assert conn.execute(select(func.count()).select_from(db.analyses)).scalar() == 0
