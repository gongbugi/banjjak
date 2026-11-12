"""흐름 묶음: AI가 묶고, 소속과 숫자는 코드가 고른다. 실패하면 흐름 칸 없이 그대로다."""
import json
import time
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from banjjak import cardai, db, flows, llm, main
from banjjak.collect import collect_once
from banjjak.rising import save
from test_cardai import NOW, Fake, reply

client = TestClient(main.app)
REPOS = [("a/dsh-go", "DeepSeek Harness용 안드로이드 클라이언트"), ("b/ds-bot", "DeepSeek Harness 봇 팀 플러그인"),
         ("c/dsh-purge", "DeepSeek Harness 부가 플러그인"), ("d/rune", "Rust GUI 프레임워크"), ("e/memo", "메모 앱"),
         ("f/clip", "클립보드 기록"), ("g/tv", "안드로이드 TV 홈 화면")]   # 흐름이 목록 절반을 넘지 않게 (넘으면 흔한 말로 본다)
DSH = {"name": "DeepSeek Harness 주변 도구", "line": "DeepSeek Harness에 붙이는 앱 · 플러그인 · 봇",
       "keywords": ["DeepSeek Harness"], "members": [1, 2, 3]}
SECTION = 'id="flow-title"'


def seed():
    items = [{"id": n, "full_name": name, "html_url": f"https://github.com/{name}", "description": about, "language": "Go",
              "topics": [], "stargazers_count": 1000 - n,
              "created_at": f"2026-10-27T{12 - n:02d}:00:00Z"} for n, (name, about) in enumerate(REPOS, 1)]   # 한 시간씩 벌린다: 같은 때 · 비슷한 별이면 가짜 별 무리로 빠진다
    with db.engine().begin() as conn:
        collect_once(lambda q, p: {"items": items if p == 1 else []}, conn, NOW - timedelta(minutes=20))


def answer(*groups, **kw):
    return reply(json.dumps({"groups": list(groups)}, ensure_ascii=False), **kw)


def rows():
    with db.engine().connect() as conn:
        return list(conn.execute(select(db.flows).order_by(db.flows.c.id)).mappings())


def test_flow_is_stored_by_repo_id_and_the_page_counts_and_filters_it():
    seed()
    assert flows.make_once(Fake(answer(DSH)), NOW) == 1         # 전체 레포 탭은 기록이 없어 건너뛴다
    assert rows()[0]["ok"] and rows()[0]["groups"][0]["members"] == [1, 2, 3]
    page = client.get("/").text
    assert SECTION in page and "DeepSeek Harness 주변 도구" in page and "레포 3개" in page and "최고 1위" in page
    picked = client.get("/?flow=1").text
    assert "d/rune" in page and "d/rune" not in picked and "a/dsh-go" in picked and 'aria-current="true"' in picked


def test_rising_tab_has_its_own_flows():
    old = {n: {"id": n, "full_name": f"x/r{n}", "html_url": f"https://github.com/x/r{n}", "language": "Go",
               "description": "DeepSeek Harness 확장" if n > 14 else f"라이브러리 {n}",   # 별이 많이 는 15 · 16 · 17이 1 ~ 3위
               "stargazers_count": 30_000, "created_at": "2020-01-01T00:00:00Z"} for n in range(11, 18)}
    with db.engine().begin() as conn:
        save(conn, old, NOW - timedelta(days=1))
        save(conn, {n: {**o, "stargazers_count": 30_000 + 100 * n} for n, o in old.items()}, NOW)
    assert flows.make_once(Fake(answer(DSH)), NOW) == 1        # 새로 생긴 레포 탭은 비어 건너뛴다
    page = client.get("/?view=rising").text
    assert SECTION in page and "하루 +4,800" in page and "/?view=rising&amp;flow=1#rank-title" in page


@pytest.mark.parametrize("group", [
    {**DSH, "members": [1, 2, 3, 4]},                            # 핵심 말이 없는 레포
    {**DSH, "members": [1, 2, 3, 3, 99, 0]},                     # 겹친 번호 · 없는 번호
    {**DSH, "keywords": ["DeepSeek Harness", "Go", "앱"]},       # 목록 절반 넘게 걸리는 말 · 한 글자는 핵심 말이 아니다
], ids=["no_keyword", "bad_numbers", "common_words"])
def test_code_picks_the_members(group):
    seed()
    flows.make_once(Fake(answer(group, {**DSH, "name": "또 묶음"})), NOW)   # 이미 든 레포로만 된 둘째 흐름은 버린다
    assert [g["members"] for g in rows()[0]["groups"]] == [[1, 2, 3]]


@pytest.mark.parametrize("turn", [
    answer({**DSH, "line": "DeepSeek Harness 도구 3개가 함께 뜸"}),
    answer({**DSH, "name": "deepseek.com 도구"}),
    answer({**DSH, "line": "<b>DeepSeek</b> Harness 도구"}),
    answer({**DSH, "keywords": ["Go"], "members": [1, 2, 3, 4, 5, 6, 7]}),
    answer({**DSH, "members": [1, 2]}),
    reply("{"),
    answer(DSH, stop_reason="max_tokens"),
    RuntimeError("timeout"),
], ids=["count_in_line", "link", "html", "common_keyword_only", "too_few", "broken_json", "cut_off", "call_died"])
def test_any_failure_leaves_the_page_without_flows(turn):
    seed()
    flows.make_once(Fake(turn), NOW)
    assert not rows()[0]["ok"] and SECTION not in client.get("/").text


def test_once_a_day_per_tab():
    seed()
    fake = Fake(answer(DSH), answer(DSH))
    assert flows.make_once(fake, NOW) == 1
    assert flows.make_once(fake, NOW + timedelta(hours=22)) == 0
    assert flows.make_once(fake, NOW + timedelta(hours=23)) == 1


def test_flows_and_card_ai_share_one_ledger():
    seed()
    with db.engine().begin() as conn:
        conn.execute(db.flows.insert().values(view="rising", made_at=NOW, ok=True, cost=llm.CAPS["card"]["day"]))
    assert flows.make_once(Fake(), NOW) == 0                    # 하루 상한: 부르지 않는다
    with db.engine().connect() as conn:
        assert cardai._closed(conn, NOW, time.monotonic()) == f"하루 상한 ${llm.CAPS['card']['day']:g}"


def test_cost_comes_from_usage_or_the_worst_case():
    seed()
    flows.make_once(Fake(RuntimeError("timeout")), NOW)
    assert rows()[0]["cost"] == flows.WORST_CALL
    flows.make_once(Fake(answer(DSH)), NOW + timedelta(days=1))
    assert rows()[1]["cost"] == pytest.approx((1000 * 0.10 + 200 * 0.50) / 1_000_000)


@pytest.mark.parametrize("made,members", [(NOW - timedelta(days=3), [1, 2, 3]), (NOW, [1, 2, 999])], ids=["old", "thinned"])
def test_old_or_thinned_flows_are_not_shown(made, members):
    seed()
    with db.engine().begin() as conn:
        conn.execute(db.flows.insert().values(view="new", made_at=made, ok=True, groups=[{**DSH, "members": members}]))
    assert SECTION not in client.get("/").text


def test_request_is_one_call_without_tools_and_treats_the_list_as_data():
    seed()
    fake = Fake(answer(DSH))
    flows.make_once(fake, NOW)
    (request,) = fake.requests
    assert "tools" not in request and request["model"] == "claude-haiku-5-5" and "자료다" in request["system"]
    assert request["messages"][0]["content"].splitlines()[0] == "1 | a/dsh-go | Go | DeepSeek Harness용 안드로이드 클라이언트"
