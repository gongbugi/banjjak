"""채점 자료 내보내기 (#12): 끄기 전에 운영 · 앱 클러스터의 기록을 PC로 받는다. 받기만 하고 내용은 화면에 찍지 않는다.

사용 (레포 루트, 본인 PowerShell): app\\.venv\\Scripts\\python deploy\\export.py [--ops 컨텍스트] [--app 컨텍스트] [--out 폴더]
- 기본값은 Azure (azure-banjjak-ops · azure-banjjak), 받는 곳은 레포 밖 ..\\banjjak-export\\<시각> (git에 안 들어간다)
- 받는 것: chaos.db (주입 정답) · opsai.db (운영 AI 판단 · LLM 장부) · prometheus.tar.gz (지표 전부) · banjjak.dump (앱 DB)
- 화면과 MANIFEST.txt에는 파일 이름 · 크기 · sha256만 남는다 (#12 전에 정답을 보지 않는다)
- 클러스터의 기록은 바꾸지 않는다. 주입기는 CronJob이라 PVC를 읽기 전용으로 붙인 임시 파드를 잠깐 띄우고 지운다
- --kubectl: kubectl 대신 쓸 명령 (로컬 리허설에서 API 포트가 막히면 "docker exec -i k3d-banjjak-server-0 kubectl")
"""
import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
from datetime import datetime

# SQLite는 읽기 전용으로 열어 통째로 내보낸다 (복사 중에 쓰여도 깨진 파일이 나오지 않는다)
DUMP_SQLITE = "import sqlite3,sys; sys.stdout.buffer.write(sqlite3.connect('file:%s?mode=ro', uri=True).serialize())"
HELPER = "grade-export"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--ops", default="azure-banjjak-ops")
    p.add_argument("--app", default="azure-banjjak")
    p.add_argument("--out", default=os.path.join("..", "banjjak-export", datetime.now().strftime("%m%d-%H%M")))
    p.add_argument("--kubectl", default="kubectl")
    a = p.parse_args()
    kubectl = shlex.split(a.kubectl)
    os.makedirs(a.out, exist_ok=False)   # 앞선 내보내기를 덮어쓰지 않는다

    def kube(ctx, *args, out=None):
        """out이 있으면 표준 출력을 그 파일로 바로 쓴다 (내용이 화면 · 메모리를 거치지 않는다)."""
        cmd = kubectl + ([] if a.kubectl != "kubectl" else ["--context", ctx]) + list(args)
        if out is None:
            return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
        with open(os.path.join(a.out, out), "wb") as f:
            subprocess.run(cmd, check=True, stdout=f)

    # 주입 정답: 주입기 이미지로 임시 파드를 띄워 PVC를 읽기 전용으로 붙인다 (주입기 Job을 만들면 주입이 돈다)
    image = kube(a.ops, "-n", "chaos", "get", "cronjob", "injector",
                 "-o", "jsonpath={.spec.jobTemplate.spec.template.spec.containers[0].image}")
    pod = {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": HELPER}, "spec": {
        "restartPolicy": "Never", "automountServiceAccountToken": False,
        "securityContext": {"runAsNonRoot": True, "fsGroup": 10001},
        "containers": [{"name": "read", "image": image, "imagePullPolicy": "IfNotPresent", "command": ["sleep", "600"],
                        "volumeMounts": [{"name": "data", "mountPath": "/data", "readOnly": True}],
                        "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True}}],
        "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "chaos", "readOnly": True}}]}}
    kube(a.ops, "-n", "chaos", "run", HELPER, "--image", image, "--restart", "Never", "--overrides", json.dumps(pod))
    try:
        kube(a.ops, "-n", "chaos", "wait", "--for=condition=Ready", f"pod/{HELPER}", "--timeout=180s")
        kube(a.ops, "-n", "chaos", "exec", HELPER, "--", "python", "-c", DUMP_SQLITE % "/data/chaos.db", out="chaos.db")
    finally:
        kube(a.ops, "-n", "chaos", "delete", "pod", HELPER, "--wait=false")
    kube(a.ops, "-n", "ops-ai", "exec", "deploy/ops-ai", "--", "python", "-c", DUMP_SQLITE % "/data/opsai.db", out="opsai.db")
    # 🟡 도는 중인 TSDB를 그대로 묶는다. 마지막 몇 분은 덜 묶일 수 있다 (WAL). 실험이 끝난 뒤에 받으면 문제없다
    kube(a.ops, "-n", "observe", "exec", "deploy/prometheus", "--", "tar", "czf", "-", "-C", "/prometheus", ".", out="prometheus.tar.gz")
    kube(a.app, "-n", "banjjak", "exec", "deploy/postgres", "--", "pg_dump", "-U", "banjjak", "-d", "banjjak", "-Fc", out="banjjak.dump")

    lines = []
    for name in sorted(os.listdir(a.out)):
        data = open(os.path.join(a.out, name), "rb").read()
        lines.append(f"{name}  {len(data):,} B  sha256 {hashlib.sha256(data).hexdigest()}")
    open(os.path.join(a.out, "MANIFEST.txt"), "w", encoding="utf-8").write(
        f"{datetime.now().isoformat(timespec='seconds')} ops={a.ops} app={a.app}\n" + "\n".join(lines) + "\n")
    sys.stdout.reconfigure(encoding="utf-8")
    print(f"받은 곳: {os.path.abspath(a.out)}\n" + "\n".join(lines))


if __name__ == "__main__":
    main()
