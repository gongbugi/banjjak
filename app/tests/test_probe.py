"""프로브: 진짜 웹 앱을 HTTP로 띄우고 실제로 부른다 (우리 코드끼리는 가짜로 바꾸지 않는다)."""
import socket
import threading
import time

import pytest
import uvicorn
from prometheus_client import REGISTRY

from banjjak import probe
from banjjak.main import app


@pytest.fixture
def web():
    with socket.socket() as s:   # 빈 포트 하나
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(10)


def probe_count(route):
    return REGISTRY.get_sample_value("banjjak_http_request_duration_seconds_count",
                                     {"route": route, "method": "GET", "status": "200", "source": "probe"}) or 0


def test_probe_calls_the_user_pages_and_is_counted_as_probe(web):
    assert probe.PATHS == ("/", "/api/trending")   # SLI 규칙이 보는 사용자 화면 두 곳
    before = [probe_count(p) for p in probe.PATHS]
    assert [probe.hit(web, p) for p in probe.PATHS] == [200, 200]
    assert [probe_count(p) for p in probe.PATHS] == [n + 1 for n in before]


def test_probe_keeps_going_when_the_web_errors_or_is_gone(web):
    assert probe.hit(web, "/no-such-page") == 404       # 오류 응답도 상태 코드로 돌려준다
    assert probe.hit("http://127.0.0.1:9", "/") is None  # 아무도 안 받는 포트: 예외 없이 None
