"""Grafana Cloud 연결 (11-20): 앱 VM 에이전트의 비밀값 + Grafana 쪽 경보 규칙 · Slack · 대시보드. 본인이 PC에서 돌린다.

사용 (레포 루트, PowerShell): app\\.venv\\Scripts\\python deploy\\grafana_cloud.py
- 값은 물어서 받는다. 토큰 · 웹훅은 getpass라 화면에 안 찍히고, 명령 인자 · 파일 · 로그에 남지 않는다
- 빈칸으로 엔터를 치면 그 단계를 건너뛴다 (규칙만 다시 올릴 때: 에이전트 토큰을 비운다)
1. 앱 VM: /etc/banjjak/secrets/grafana-cloud-{url,user,token} (SSH 표준 입력으로, 444). compose의 agent가 읽는다
2. Grafana (서비스 계정 토큰): Slack 연락처 · 경보 규칙 · 대시보드
   경보 규칙은 deploy/ops/rules.yml의 sli 그룹을 Grafana 규칙 가져오기 API에 YAML 그대로 보낸다 (규칙의 원본은 rules.yml 하나).
   새 무료 계정에는 Grafana Cloud Alertmanager가 없어 (Grafana 상태 페이지 2026-04) 경보는 Grafana 자체 경보로 돈다.
   입구(Traefik) · Watchdog 그룹은 운영 VM 것이라 뺀다. 에이전트 · VM이 멈추면 sli의 DataAgeMissing이 운다
"""
import getpass
import json
import os
import re
import subprocess
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
VM = "azureuser@20.41.96.42"
KEY = os.path.expanduser("~/.ssh/banjjak_azure_ed25519")   # infra/azure/create.ps1이 만든 키 (SSH는 운영자 IP에만 열려 있다)
SECRETS = "/etc/banjjak/secrets"
FOLDER = "banjjak"         # 규칙 가져오기에서 namespace = Grafana 폴더 이름
CONTACT = "banjjak-slack"


def sli_group(text):
    """rules.yml에서 sli 그룹 하나를 YAML로 떼어 낸다: 주석 줄은 빼고, 그룹 안 들여쓰기 4칸을 벗긴다."""
    m = re.search(r"^  - name: sli\n(.*?)(?=^  - name: |\Z)", text, re.S | re.M)
    if not m:
        raise SystemExit("rules.yml에 sli 그룹이 없다")
    lines = [line[4:] for line in m.group(1).splitlines() if not line.lstrip().startswith("#")]
    return "name: sli\n" + "\n".join(lines).rstrip() + "\n"


def ssh(cmd, value=None):
    return subprocess.run(["ssh", "-i", KEY, "-o", "BatchMode=yes", VM, cmd], input=value and value.encode(),
                          capture_output=True, check=True).stdout.decode()


def put_secret(name, value):
    """값은 SSH 표준 입력으로만 지난다. 새 파일에 다 쓴 뒤 444로 바꿔 옮긴다 (compose의 비루트 사용자가 읽는다)"""
    f = f"{SECRETS}/{name}"
    ssh(f"sudo sh -c 'umask 077; cat > {f}.new && chmod 444 {f}.new && mv {f}.new {f}'", value)


def vm_secrets():
    token = getpass.getpass("에이전트 토큰 (access policy, metrics:write만. 비우면 VM 단계를 건너뜀): ").strip()
    if not token:
        print("VM 건너뜀")
        return
    url = input("Prometheus remote_write 주소 (https://.../api/prom/push): ").strip()
    user = input("Prometheus 사용자 (숫자 ID): ").strip()
    if not (url.startswith("https://") and url.endswith("/api/prom/push")):
        raise SystemExit("remote_write 주소 모양이 다르다 (https:// 로 시작하고 /api/prom/push로 끝난다)")
    if not user.isdigit():
        raise SystemExit("사용자는 숫자 ID다")
    for name, value in (("grafana-cloud-url", url), ("grafana-cloud-user", user), ("grafana-cloud-token", token)):
        put_secret(name, value)
    print(ssh(f"sudo wc -c {SECRETS}/grafana-cloud-url {SECRETS}/grafana-cloud-user {SECRETS}/grafana-cloud-token"), end="")


def api(base, token, method, path, body=None, headers=None, ctype="application/json"):
    data = body if isinstance(body, bytes) or body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, method=method, headers={
        "Authorization": f"Bearer {token}", "Content-Type": ctype, "Accept": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{method} {path} → {e.code} {e.read()[:300].decode(errors='replace')}")
    return json.loads(raw) if raw else None


def grafana():
    base = input("Grafana 주소 (https://<스택>.grafana.net, 비우면 건너뜀): ").strip().rstrip("/")
    if not base:
        print("Grafana 건너뜀")
        return
    token = getpass.getpass("서비스 계정 토큰 (Admin): ").strip()
    ds = [d for d in api(base, token, "GET", "/api/datasources")
          if d["type"] == "prometheus" and re.fullmatch(r"grafanacloud-.+-prom", d["name"])]
    if len(ds) != 1:
        raise SystemExit(f"Prometheus 데이터 소스를 하나로 못 골랐다: {[d['name'] for d in ds]}")
    uid = ds[0]["uid"]
    print("데이터 소스", ds[0]["name"])

    have = [c for c in api(base, token, "GET", "/api/v1/provisioning/contact-points") if c["name"] == CONTACT]
    hook = getpass.getpass("Slack Incoming Webhook 주소 (비우면 있는 연락처 그대로): ").strip()
    if hook:
        if not hook.startswith("https://hooks.slack.com/"):
            raise SystemExit("Slack 웹훅 주소 모양이 아니다 (값은 찍지 않음)")
        cp = {"name": CONTACT, "type": "slack", "settings": {"url": hook}}
        if have:
            api(base, token, "PUT", f"/api/v1/provisioning/contact-points/{have[0]['uid']}", cp)
        else:
            api(base, token, "POST", "/api/v1/provisioning/contact-points", cp)
        print("연락처", CONTACT)
    elif not have:
        raise SystemExit("연락처가 아직 없는데 웹훅도 비었다")

    # 다시 올리면 같은 그룹을 고친다. 알림은 정책을 거치지 않고 바로 Slack 연락처로
    group = sli_group(open(os.path.join(HERE, "ops", "rules.yml"), encoding="utf-8").read())
    api(base, token, "POST", f"/api/convert/prometheus/config/v1/rules/{FOLDER}", group.encode(), {
        "X-Grafana-Alerting-Datasource-UID": uid,
        "X-Grafana-Alerting-Notification-Settings": json.dumps({"receiver": CONTACT})}, ctype="application/yaml")
    print("경보 규칙", re.findall(r"- alert: (\w+)", group))

    folder = [f["uid"] for f in api(base, token, "GET", "/api/folders") if f["title"] == FOLDER]
    dash = open(os.path.join(HERE, "grafana", "banjjak.json"), encoding="utf-8").read()
    dash = json.loads(dash.replace('"uid": "prom"}', f'"uid": "{uid}"}}'))
    api(base, token, "POST", "/api/dashboards/db", {"dashboard": {**dash, "id": None}, "folderUid": folder[0], "overwrite": True})
    print("대시보드", dash.get("title"))


if __name__ == "__main__":
    vm_secrets()
    grafana()
    print("끝. Grafana → Alerting → Contact points → banjjak-slack → Test 로 Slack에 한 번 보내 본다")
