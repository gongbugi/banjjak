"""앱 클러스터 API에 붙는 얇은 문 (운영 AI · 장애 주입기가 같이 쓴다).

주소 · 토큰 · CA는 /etc/app-cluster(Secret app-cluster)에서 읽는다. 누가 쓰느냐에 따라 토큰이 다르다:
운영 AI는 banjjak/ops-ai(읽기 + 허용 조치), 주입기는 chaos/chaos(주입에 필요한 것만). 할 수 있는 일은 RBAC가 정한다.
파일은 매번 읽는다 → Secret을 바꾸면 재시작 없이 따라간다.
"""
import json
import os
import ssl
import urllib.request

APP_CLUSTER = "/etc/app-cluster"


def _connection():
    def read(name):
        with open(os.path.join(APP_CLUSTER, name)) as f:
            return f.read().strip()
    return read("url"), {"Authorization": "Bearer " + read("token")}, \
        ssl.create_default_context(cafile=os.path.join(APP_CLUSTER, "ca.crt"))


def _call(path, method="GET", body=None, content_type=None):
    url, headers, tls = _connection()
    data = None if body is None else json.dumps(body).encode()
    if content_type:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(url + path, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=10, context=tls) as r:
        text = r.read().decode()
        return json.loads(text) if r.headers.get_content_type() == "application/json" else text


def get(path):
    """읽기 (GET). JSON이면 dict, 로그면 str."""
    return _call(path)


def patch(path, body, content_type):
    """쓰기 (PATCH)."""
    return _call(path, "PATCH", body, content_type)


def delete(path):
    return _call(path, "DELETE")
