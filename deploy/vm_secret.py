"""앱 VM의 비밀 파일 하나를 넣거나 바꾼다. 본인이 PC에서 돌린다 (터널 토큰 · 12-31 전 백업 SAS 갈기 등).

사용 (레포 루트, PowerShell): app\\.venv\\Scripts\\python deploy\\vm_secret.py cloudflare-tunnel-token
- 이름은 deploy/compose/compose.yaml의 secrets: 에 있는 것만. 값은 getpass라 화면 · 명령 인자 · 로그에 안 남는다
- 이미 떠 있는 컨테이너는 옛 파일을 계속 본다. 바꾼 뒤 그 서비스를 다시 만든다 (up -d --force-recreate <서비스>)
"""
import getpass
import os
import re
import sys

from grafana_cloud import HERE, SECRETS, put_secret, ssh

names = re.findall(r"^  ([a-z0-9-]+): \{file: ", open(os.path.join(HERE, "compose", "compose.yaml"), encoding="utf-8").read(), re.M)
name = sys.argv[1] if len(sys.argv) == 2 else ""
if name not in names:
    raise SystemExit(f"이름은 compose secrets 중 하나: {names}")
value = getpass.getpass(f"{name} 값 (화면에 안 찍힘): ").strip()
if not value:
    raise SystemExit("빈 값이라 그만둔다")
put_secret(name, value)
print(ssh(f"sudo wc -c {SECRETS}/{name}"), end="")
