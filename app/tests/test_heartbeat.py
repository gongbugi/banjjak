import urllib.request

from banjjak.heartbeat import ping


def test_no_url_means_no_request(monkeypatch):
    calls = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: calls.append(a))
    assert ping(None) is False and ping("") is False      # 로컬 · 시험에는 주소가 없다
    assert calls == []


def test_ping_failure_does_not_raise(monkeypatch):
    # 하트비트를 못 보내도 본 작업(수집 · 분석)은 성공이다. 끊긴 신호는 밖에서 알아챈다
    def boom(*a, **k):
        raise OSError("network down")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert ping("https://uptime.example/api/v1/heartbeat/x") is False


def test_ping_sends_get(monkeypatch):
    seen = []

    class Resp:
        def close(self):
            pass

    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout: seen.append((url, timeout)) or Resp())
    assert ping("https://uptime.example/api/v1/heartbeat/x") is True
    assert seen == [("https://uptime.example/api/v1/heartbeat/x", 10)]
