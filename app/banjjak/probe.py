"""합성 요청: 운영 VM에서 10초마다 사용자 화면 두 곳을 번갈아 부른다.

사람이 거의 없어도 웹 SLI(5xx 비율 · p95)가 잡히게 한다 (2026-10-22 실측: 5분 창의 77%가 사용자 요청 0건).
웹은 이 User-Agent로 합성(source="probe")과 실제(source="user")를 나눠 센다. 성공 · 실패는 웹 쪽 지표가 센다.
실행: PROBE_URL=http://20.41.96.42 python -m banjjak.probe
"""
import itertools
import os
import time
import urllib.error
import urllib.request

USER_AGENT = "banjjak-probe/1"
PATHS = ("/", "/api/trending")   # SLI 규칙이 보는 사용자 화면
EVERY = 10   # 초. 5분 창에 30건 → 5% 경보가 2건 실패로 울린다


def hit(base, path, timeout=5):
    """한 번 부르고 상태 코드를 돌려준다. 답이 없으면 None. 어떤 경우에도 예외를 내지 않는다 (다음 차례로 간다)."""
    req = urllib.request.Request(base + path, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception as e:
        print(f"probe {path}: {e}", flush=True)
        return None


if __name__ == "__main__":
    base = os.environ["PROBE_URL"]
    for path in itertools.cycle(PATHS):
        hit(base, path)
        time.sleep(EVERY)
