"""하트비트: 일을 마쳤다고 밖(Better Stack)에 알린다. 신호가 끊기면 밖에서 알아챈다 (VM이 통째로 죽어도).

주소가 없으면(로컬 · 시험) 아무것도 안 한다. 보내기가 실패해도 본 작업은 성공으로 둔다.
"""
import urllib.request


def ping(url, timeout=10):
    if not url:
        return False
    try:
        urllib.request.urlopen(url, timeout=timeout).close()
        return True
    except Exception as e:
        print(f"heartbeat failed: {e}")
        return False
