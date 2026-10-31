"""#20 Sonnet 진단기 · 비용 원장 · jev 게이트. 진짜 API는 부르지 않는다 (가짜 클라이언트가 정해 둔 답을 준다)."""
import json
from contextlib import closing
from datetime import timedelta
from types import SimpleNamespace

import anthropic
import httpx2
import pytest
from fastapi.testclient import TestClient
from test_opsai import NOW, Ops, cluster, fake_kube, fake_prom, firing

from banjjak import checklist, llm
from banjjak.actions import ACTIONS, connect
from banjjak.opsai import KINDS, app, handle

USAGE = {"input_tokens": 1000, "output_tokens": 100, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
ALL_TOOLS = [("deployment", {}), ("pods", {"app": "web"}), ("pods", {"app": "postgres"}), ("cronjob", {"name": "collector"}),
             ("job", {"name": "collector"}), ("hpa", {}), ("traffic", {}), ("metrics_arrival", {})]


class Fake:
    """가짜 Anthropic 클라이언트: 정해 둔 응답을 차례로 주고, 그때 받은 요청을 그대로 남긴다."""

    def __init__(self, *turns):
        self.turns, self.requests, self.messages = list(turns), [], self

    def create(self, **request):
        self.requests.append(json.loads(json.dumps(request, default=vars)))
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        return turn


def reply(stop_reason, *blocks, **usage):
    return SimpleNamespace(stop_reason=stop_reason, content=list(blocks), usage=SimpleNamespace(**{**USAGE, **usage}))


def asks(*calls):
    """도구를 부르는 턴. calls: (도구, 인자) 여러 개."""
    return reply("tool_use", *[SimpleNamespace(type="tool_use", id=f"t{i}", name=name, input=args)
                               for i, (name, args) in enumerate(calls)])


def answer(cause, evidence=(), ask_human=False):
    text = json.dumps({"cause": cause, "evidence": [{"look": look, "saw": saw} for look, saw in evidence],
                       "ask_human": ask_human}, ensure_ascii=False)
    return reply("end_turn", SimpleNamespace(type="text", text=text))


def ops_with(tmp_path, fake, kube=None, gate=None):
    ops = Ops(str(tmp_path / "opsai.db"), kube or fake_kube(cluster()))
    ops.hands = ops.hands._replace(llm=lambda conn, alert, tape, now: llm.judge(conn, alert, tape, now, fake), gate=gate)
    return ops


def tape_of(kube=None, prom=None):
    """경보 순간의 테이프 한 장 (운영 AI가 녹화하는 그대로)."""
    tape, kube_, prom_ = checklist.recorder(kube or fake_kube(cluster()), prom or fake_prom())
    checklist.sweep(kube_, prom_, NOW)
    return tape


@pytest.fixture
def conn(tmp_path):
    with closing(connect(str(tmp_path / "opsai.db"))) as c:
        yield c


SUSPENDED = [asks(("cronjob", {"name": "collector"})), answer("collector_suspended", [("cronjob", "켜져 있음")])]


# --- 판단 · 기록 · 보고 ---

def test_llm_judgment_picks_the_action_and_both_judgments_are_kept(tmp_path):
    ops = ops_with(tmp_path, Fake(*SUSPENDED), fake_kube(cluster(last_success="2026-10-21T13:00:20Z")))
    result = ops.alert("DataStale")
    assert (result["cause"], result["action"], result["tier"]) == ("collector_suspended", "resume_collector", "T2")   # 점검표는 못 찾았다
    assert result["diagnoser"] == llm.NAME
    assert result["checklist"]["cause"] is None and result["llm"]["cause"] == "collector_suspended"
    assert result["evidence"] == [{"look": "cronjob", "saw": "켜져 있음"}]
    assert "점검표: 판단 못 함" in ops.sent[0]


def test_llm_saying_unknown_goes_to_a_human_even_if_the_checklist_found_a_cause(tmp_path):
    ops = ops_with(tmp_path, Fake(answer("unknown")), fake_kube(cluster(suspend=True)))
    result = ops.alert("DataStale")
    assert (result["cause"], result["action"], result["state"]) == (None, None, "human")
    assert result["checklist"]["cause"] == "collector_suspended"


def test_alert_nobody_classified_does_not_spend_money(tmp_path):
    fake = Fake(answer("unknown"))
    result = ops_with(tmp_path, fake).alert("SomethingNew")
    assert fake.requests == [] and "llm" not in result


def test_llm_can_ask_for_approval_but_not_lower_a_tier(tmp_path):
    ops = ops_with(tmp_path, Fake(asks(("cronjob", {"name": "collector"})),
                                  answer("collector_suspended", [("cronjob", "켜져 있음")], ask_human=True)))
    assert ops.alert("DataStale")["tier"] == "T2"
    assert ops.states() == [("resume_collector", "T2", "waiting")]


@pytest.mark.parametrize("turns", [
    [answer("collector_exploded", [("cronjob", "켜져 있음")])],                            # 목록 밖 원인
    [asks(("cronjob", {"name": "collector"})), answer("collector_suspended", [("cronjob", "지어낸 문장")])],   # 근거 대조 실패
    [asks(("cronjob", {"name": "collector"})), answer("collector_suspended", [("hpa", "켜져 있음")])],       # 안 부른 도구
    [asks(("cronjob", {"name": "collector"})), answer("collector_suspended")],             # 근거 없음
    [reply("end_turn", SimpleNamespace(type="text", text="JSON이 아니다"))],
    [reply("refusal")],
    [reply("max_tokens")],
    [ConnectionError("끊김")],
], ids=["out_of_list", "unquoted", "uncalled_tool", "no_evidence", "not_json", "refusal", "max_tokens", "api_error"])
def test_every_llm_failure_falls_back_to_the_checklist(tmp_path, turns):
    ops = ops_with(tmp_path, Fake(*turns), fake_kube(cluster(suspend=True)))
    result = ops.alert("DataStale")
    assert (result["cause"], result["diagnoser"]) == ("collector_suspended", checklist.NAME)
    assert result["llm"]["failed"]
    assert "LLM 판단 없음 (점검표로)" in ops.sent[0]
    assert result["llm"]["failed"] not in ops.sent[0]   # 이유는 기록에만


def test_too_many_turns_stops_the_loop(conn):
    fake = Fake(*[asks(("hpa", {})) for _ in range(30)])
    assert llm.judge(conn, firing("DataStale"), tape_of(), NOW, fake)["failed"]
    assert len(fake.requests) == llm.MAX_TURNS


def test_deadline_stops_the_loop(conn, monkeypatch):
    monkeypatch.setattr(llm, "DEADLINE", -1)
    fake = Fake(answer("unknown"))
    assert llm.judge(conn, firing("DataStale"), tape_of(), NOW, fake)["failed"]
    assert fake.requests == []


def test_a_read_missing_from_the_tape_is_a_tool_error_not_a_crash(conn):
    fake = Fake(asks(("pods", {"app": "web"})), answer("unknown"))
    assert llm.judge(conn, firing("WebErrorRateHigh"), {"kube": {}, "prom": {}}, NOW, fake)["cause"] is None
    result = fake.requests[1]["messages"][-1]["content"][0]
    assert result["is_error"] and "녹화에 없는 읽기" in result["content"]


def test_results_of_parallel_tool_calls_go_back_together(conn):
    fake = Fake(asks(("hpa", {}), ("deployment", {})), answer("unknown"))
    llm.judge(conn, firing("DataStale"), tape_of(), NOW, fake)
    assert [r["tool_use_id"] for r in fake.requests[1]["messages"][-1]["content"]] == ["t0", "t1"]


def test_request_keeps_the_decided_settings(conn):
    fake = Fake(answer("unknown"))
    llm.judge(conn, firing("DataStale"), tape_of(), NOW, fake)
    sent = fake.requests[0]
    assert (sent["model"], sent["max_tokens"], sent["output_config"]["effort"]) == ("claude-sonnet-5-5", 4000, "medium")
    assert sent["output_config"]["format"]["schema"]["properties"]["cause"]["enum"] == [*checklist.CAUSES, "unknown"]
    assert all(t["strict"] for t in sent["tools"])
    assert "temperature" not in sent and "fallbacks" not in sent


def test_real_sdk_builds_the_requests_and_reads_the_answers(conn):
    """진짜 anthropic SDK로 왕복한다 (네트워크 없이): 가짜 클라이언트가 숨길 수 있는 직렬화 · 응답 모양 차이를 잡는다."""
    sent = []
    usage = {"input_tokens": 1000, "output_tokens": 100, "cache_creation_input_tokens": 300, "cache_read_input_tokens": 0}
    turns = [
        {"stop_reason": "tool_use", "content": [
            {"type": "thinking", "thinking": "요약", "signature": "sig"},
            {"type": "tool_use", "id": "toolu_1", "name": "cronjob", "input": {"name": "collector"}}]},
        {"stop_reason": "end_turn", "content": [{"type": "text", "text": json.dumps(
            {"cause": "collector_late", "evidence": [{"look": "cronjob", "saw": "켜져 있음"}], "ask_human": False})}]}]

    def api(request):
        sent.append(json.loads(request.content))
        return httpx2.Response(200, json={"id": "msg_1", "type": "message", "role": "assistant", "model": llm.MODEL,
                                          "stop_sequence": None, "usage": usage, **turns.pop(0)})
    real = anthropic.Anthropic(api_key="x", http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(api)))
    result = llm.judge(conn, firing("DataStale"), tape_of(), NOW, real)
    assert result["cause"] == "collector_late"
    assert sent[0]["cache_control"] == {"type": "ephemeral"} and sent[0]["output_config"]["effort"] == "medium"
    assert sent[1]["messages"][1]["content"][0]["type"] == "thinking"   # 생각 블록은 그대로 돌려준다
    assert sent[1]["messages"][2]["content"][0]["tool_use_id"] == "toolu_1"
    assert llm.spent(conn, "live") == pytest.approx(2 * (0.002 + 0.00075 + 0.001))


def test_thinking_setting_is_sent_only_when_asked(conn):
    fake = Fake(answer("unknown"))
    llm.judge(conn, firing("DataStale"), tape_of(), NOW, fake, ledger="replay", thinking={"type": "between_tools"})
    assert fake.requests[0]["thinking"] == {"type": "between_tools"}


# --- 정답 숨기기 ---

FAULTY = cluster(env=[{"name": "FAULT", "value": '{"error_rate": 0.3}'}], deployed="2026-10-21T13:40:00Z")


def test_answer_on_the_tape_never_reaches_the_llm(conn):
    FAULTY["/apis/apps/v1/namespaces/banjjak/deployments/web"]["spec"]["template"]["spec"]["volumes"] = [
        {"name": "web-fault", "configMap": {"name": "web-fault", "items": [{"key": "fault.json"}]}}]
    fake = Fake(asks(*ALL_TOOLS), answer("unknown"))
    assert "failed" not in llm.judge(conn, firing("WebErrorRateHigh"), tape_of(fake_kube(FAULTY)), NOW, fake)
    sent = json.dumps(fake.requests, ensure_ascii=False)
    assert not [w for w in llm.FORBIDDEN if w in sent]
    assert "deploy a169" not in sent   # 배포는 판 · 이미지 · 시각 · 준비 수만


def test_forbidden_word_in_free_text_stops_before_it_is_sent(tmp_path):
    kube = fake_kube(cluster(collect_ok=False, collect_log='RuntimeError: FAULT={"delay": 2}\n'))
    fake = Fake(asks(("job", {"name": "collector"})), answer("unknown"))
    ops = ops_with(tmp_path, fake, kube)
    result = ops.alert("WebErrorRateHigh")
    assert len(fake.requests) == 1 and "FAULT" not in json.dumps(fake.requests, ensure_ascii=False)
    assert result["diagnoser"] == checklist.NAME and result["llm"]["failed"]
    assert "LLM 판단 없음 (점검표로)" in ops.sent[0] and "금지어" not in ops.sent[0] and "FAULT" not in ops.sent[0]


# --- 비용 원장 ---

def seed(conn, ledger, cost, at=NOW):
    conn.execute("INSERT INTO llm_calls (at, ledger, kind, cost) VALUES (?, ?, 'call', ?)", (at.timestamp(), ledger, cost))
    conn.commit()


def test_cost_counts_cache_and_output_at_their_own_prices(conn):
    llm.judge(conn, firing("DataStale"), tape_of(), NOW, Fake(reply(
        "end_turn", SimpleNamespace(type="text", text=json.dumps({"cause": "unknown", "evidence": [], "ask_human": False})),
        input_tokens=1000, cache_creation_input_tokens=2000, cache_read_input_tokens=10000, output_tokens=500)))
    assert llm.spent(conn, "live") == pytest.approx(0.002 + 0.005 + 0.001 + 0.005)


def test_haiku_is_charged_at_its_own_price(conn):
    llm.judge(conn, firing("DataStale"), tape_of(), NOW, Fake(answer("unknown")), ledger="replay", model="claude-haiku-5-5")
    assert llm.spent(conn, "replay") == pytest.approx(0.0001 + 0.00005)


def test_repeat_webhook_of_one_alert_uses_the_llm_once(tmp_path):
    fake = Fake(answer("unknown"))
    ops = ops_with(tmp_path, fake)
    ops.alert("DataStale")
    assert ops.alert("DataStale", NOW + timedelta(hours=4))["llm"]["failed"]
    assert len(fake.requests) == 1


def test_a_new_firing_of_the_same_rule_is_a_new_alert(conn):
    fake = Fake(answer("unknown"), answer("unknown"))
    alert = firing("DataStale")
    llm.judge(conn, alert, tape_of(), NOW, fake)
    llm.judge(conn, {**alert, "startsAt": "2026-10-21T15:00:00Z"}, tape_of(), NOW, fake)
    assert len(fake.requests) == 2


def test_no_more_than_six_llm_diagnoses_an_hour(conn):
    fake = Fake(*[answer("unknown") for _ in range(8)])
    results = [llm.judge(conn, {**firing(kind), "fingerprint": f"f{i}"}, tape_of(), NOW, fake) for i, kind in enumerate(KINDS)]
    assert ["failed" in r for r in results] == [False] * 6 + [True]
    later = NOW + timedelta(hours=1, seconds=1)
    assert "failed" not in llm.judge(conn, {**firing("DataStale"), "fingerprint": "f9"}, tape_of(), later, fake)


def test_daily_cap_turns_the_llm_off_until_kst_midnight(conn):
    seed(conn, "live", llm.CAPS["live"]["day"])
    fake = Fake(answer("unknown"))
    assert llm.judge(conn, firing("DataStale"), tape_of(), NOW, fake)["failed"]
    assert fake.requests == []
    next_day = NOW.replace(hour=15, minute=1)   # 15:00 UTC = KST 0시
    assert "failed" not in llm.judge(conn, firing("DataStale"), tape_of(), next_day, fake)


def test_total_cap_does_not_come_back_by_itself(conn):
    for day in range(1, 4):
        seed(conn, "live", 1.0, NOW - timedelta(days=day))
    assert llm.judge(conn, firing("DataStale"), tape_of(), NOW, Fake(answer("unknown")))["failed"]
    assert llm.judge(conn, firing("DataStale"), tape_of(), NOW + timedelta(days=30), Fake(answer("unknown")))["failed"]


def test_cap_is_checked_before_every_call_not_just_the_first(conn):
    seed(conn, "live", llm.CAPS["live"]["day"] - 0.001)
    fake = Fake(reply("tool_use", SimpleNamespace(type="tool_use", id="t", name="hpa", input={}), output_tokens=10_000),
                answer("unknown"))
    assert llm.judge(conn, firing("DataStale"), tape_of(), NOW, fake)["failed"]
    assert len(fake.requests) == 1


def test_ledgers_are_separate(conn):
    seed(conn, "live", llm.CAPS["live"]["total"])
    assert "failed" not in llm.judge(conn, firing("DataStale"), tape_of(), NOW, Fake(answer("unknown")), ledger="replay")
    seed(conn, "replay", llm.CAPS["replay"]["total"])
    assert llm.judge(conn, firing("DataStale"), tape_of(), NOW, Fake(answer("unknown")), ledger="replay")["failed"]


def test_a_client_that_cannot_start_still_diagnoses_by_checklist(tmp_path):
    def broken(conn, alert, tape, now):
        raise ImportError("anthropic")
    ops = ops_with(tmp_path, Fake(), fake_kube(cluster(suspend=True)))
    ops.hands = ops.hands._replace(llm=broken)
    result = ops.alert("DataStale")
    assert (result["cause"], result["diagnoser"]) == ("collector_suspended", checklist.NAME)
    assert "ImportError" in result["llm"]["failed"]
    assert ops.states() == [("resume_collector", "T1", "waiting")]


def test_cap_tells_slack_once_a_day(tmp_path):
    ops = ops_with(tmp_path, Fake())
    with closing(connect(ops.db)) as c:
        seed(c, "live", llm.CAPS["live"]["day"])
    ops.alert("DataStale")
    ops.alert("WebErrorRateHigh")
    assert sum("LLM 상한" in m for m in ops.sent) == 1


def test_rediagnosis_stays_with_the_checklist(tmp_path):
    fake = Fake(*SUSPENDED)
    ops = ops_with(tmp_path, fake, fake_kube(cluster(suspend=True)))
    first = ops.alert("DataStale")
    with closing(connect(ops.db)) as c:
        again = handle(firing("DataStale"), ops.prom, NOW + timedelta(minutes=80), c, ops.hands,
                       after=(first, "70분 안에 풀리지 않았다"))
    assert again["diagnoser"] == checklist.NAME and "llm" not in again
    assert len(fake.requests) == 2


def test_one_alert_kind_cannot_spend_the_hourly_pool(conn):
    # 입구 거절 경보는 공개 요청으로 다시 울릴 수 있다 → 한 종류가 진짜 장애의 LLM 몫을 다 쓰지 못하게 한다
    fake = Fake(*[answer("unknown") for _ in range(4)])
    flood = [llm.judge(conn, {**firing("WebRequestsRejected"), "fingerprint": f"f{i}"}, tape_of(), NOW, fake) for i in range(3)]
    assert ["failed" in r for r in flood] == [False, False, True]
    assert "failed" not in llm.judge(conn, firing("DataStale"), tape_of(), NOW, fake)


# --- jev 게이트 ---

def jev(p):
    calls = []

    def post(body):
        calls.append(body)
        return {"model": "jev-1.14.0", "answers": {"points": {"type": "noul", "noul": p}},
                "usage": {"input_tokens": 200, "output_tokens": 12}}
    post.calls = calls
    return post


def gated(tmp_path, post, kube=None):
    fake = Fake(*SUSPENDED)
    gate = lambda conn, record, action, now: llm.gate(conn, record, action, now, post)
    return ops_with(tmp_path, fake, kube or fake_kube(cluster(suspend=True)), gate)


def test_gate_high_keeps_t1(tmp_path):
    ops = gated(tmp_path, jev(0.9))
    assert ops.alert("DataStale")["tier"] == "T1"
    assert ops.states() == [("resume_collector", "T1", "waiting")]


def test_gate_approve_raises_to_t2(tmp_path):
    fake = Fake(*SUSPENDED)
    ops = ops_with(tmp_path, fake, fake_kube(cluster(suspend=True)), lambda *a: {"p": 0.5, "say": "approve"})
    ops.alert("DataStale")
    assert ops.states() == [("resume_collector", "T2", "waiting")]
    assert "T2로 올림" in ops.sent[0]


@pytest.mark.parametrize("action, p, say", [
    ("rollback_web", 0.8, "keep"), ("rollback_web", 0.79, "approve"), ("rollback_web", 0.3, "approve"), ("rollback_web", 0.29, "hold"),
    ("resume_collector", 0.3, "keep"), ("resume_collector", 0.29, "hold"), ("resume_analyzer", 0.3, "keep"),
])
def test_gate_threshold_follows_the_risk_of_the_action(conn, action, p, say):
    record = {"alertname": "DataStale", "sli": None, "verdict": "x", "evidence": []}
    assert llm.gate(conn, record, action, NOW, jev(p))["say"] == say


def test_gate_low_hands_it_to_a_human(tmp_path):
    ops = gated(tmp_path, jev(0.1))
    ops.alert("DataStale")
    assert ops.states() == [(None, None, "human")]


def test_gate_failure_leaves_the_tier_as_it_was_and_is_recorded(tmp_path):
    def broken(body):
        raise TimeoutError("느림")
    ops = gated(tmp_path, broken)
    ops.alert("DataStale")
    assert ops.states() == [("resume_collector", "T1", "waiting")]
    with closing(connect(ops.db)) as c:
        record = json.loads(c.execute("SELECT diagnosis FROM records").fetchone()["diagnosis"])
    assert record["gate"]["failed"]


def test_gate_is_not_asked_when_a_human_already_decides(tmp_path):
    post = jev(0.9)
    ops = gated(tmp_path, post)
    ops.hands = ops.hands._replace(llm=None)
    with closing(connect(ops.db)) as c:
        handle(firing("DataStale"), fake_prom(), NOW, c, ops.hands._replace(kube=fake_kube({})))   # 앱 VM 재시작 = T2
    assert post.calls == []


def test_gate_gets_time_as_words_and_no_deploy_note(conn):
    post = jev(0.9)
    record = {"alertname": "WebErrorRateHigh", "sli": "가용성 99%", "verdict": checklist.CAUSES["recent_deploy"], "evidence": [
        {"look": "웹 최근 배포", "saw": "12분 전 · deploy a169f9e04c1b at 2026-10-21T13:48:00Z"}]}
    assert llm.gate(conn, record, "rollback_web", NOW, post)["say"] == "keep"
    sent = json.dumps(post.calls[0], ensure_ascii=False)
    assert "12분 전" in sent and "deploy a169" not in sent
    assert ACTIONS["rollback_web"][1] in sent
    assert post.calls[0]["model"] == "jev-1.14.0"


def test_gate_will_not_send_a_forbidden_word(conn):
    post = jev(0.9)
    record = {"alertname": "DataStale", "sli": None, "verdict": "x", "evidence": [{"look": "로그", "saw": "web-fault 없음"}]}
    assert llm.gate(conn, record, "resume_collector", NOW, post)["failed"]
    assert post.calls == []


def test_gate_usage_goes_on_the_live_ledger(conn):
    record = {"alertname": "DataStale", "sli": None, "verdict": "x", "evidence": []}
    llm.gate(conn, record, "resume_collector", NOW, jev(0.9))
    assert llm.spent(conn, "live") == pytest.approx(200 * 0.042 / 1_000_000)


@pytest.mark.parametrize("usage", [{"input_tokens": -71_000_000}, {"input_tokens": 10**12}, {"input_tokens": "200"}, {}, None])
def test_gate_does_not_trust_the_usage_it_is_told(conn, usage):
    seed(conn, "live", 3.0)
    post = lambda body: {"answers": {"points": {"noul": 0.1}}, "usage": usage}
    record = {"alertname": "DataStale", "sli": None, "verdict": "x", "evidence": []}
    assert llm.gate(conn, record, "rollback_web", NOW, post)["say"] == "hold"   # 점수는 그대로 쓴다
    assert llm._over(conn, "live", NOW)   # 상한이 다시 열리지 않는다


def test_llm_disagreeing_with_the_checklist_asks_first(tmp_path):
    ops = ops_with(tmp_path, Fake(), fake_kube(cluster()))
    ops.hands = ops.hands._replace(llm=lambda conn, alert, tape, now: {
        "diagnoser": llm.NAME, "cause": "collector_suspended", "verdict": "x", "evidence": [], "ask_human": False})
    row = ops.alert("DataStale")
    assert (row["action"], row["tier"]) == ("resume_collector", "T2")
    assert "LLM과 점검표의 원인이 다르다" in ops.sent[-1]


# --- 지표 ---

def test_llm_results_show_up_on_the_metrics_page(tmp_path):
    ops_with(tmp_path, Fake(answer("unknown"))).alert("DataStale")
    page = TestClient(app).get("/metrics").text
    assert 'banjjak_llm_diagnoses_total{result="ok"}' in page
    assert "banjjak_llm_cost_dollars_total" in page
    assert 'banjjak_llm_diagnosis_seconds_bucket{le="30.0"}' in page
    assert 'banjjak_llm_tokens_total{kind="input",model="claude-sonnet-5-5"}' in page
