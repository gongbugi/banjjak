#!/usr/bin/env bash
# 테스트 문 5: 운영 AI 계정(banjjak/ops-ai)의 권한이 banjjak 읽기 + (실행을 켠 클러스터만) 조치 표의 쓰기뿐인지 확인한다.
# 사용: bash deploy/check-opsai-rbac.sh <kubectl 컨텍스트> [dry-run|execute]   (기본 dry-run: 쓰기가 하나도 없어야 한다)
#       로컬 k3d는 execute, Azure 앱 VM은 Slack 버튼(#8 2단계) 전까지 dry-run
set -uo pipefail
ctx=${1:?kubectl context}
mode=${2:-dry-run}
as=system:serviceaccount:banjjak:ops-ai
bad=0
check() {   # check <yes|no> <verb> <resource[/name]> <namespace> [kubectl 옵션]
  got=$(kubectl --context "$ctx" auth can-i "$2" "$3" -n "$4" --as="$as" "${@:5}" 2>/dev/null)
  [ "$got" = "$1" ] && echo "ok   $1 $2 $3 -n $4 ${*:5}" || { echo "FAIL $2 $3 -n $4 ${*:5}: $got (기대 $1)"; bad=1; }
}
for r in pods pods/log deployments.apps replicasets.apps jobs.batch cronjobs.batch; do check yes get "$r" banjjak; check yes list "$r" banjjak; done
check no get secrets banjjak
check no delete pods banjjak
check no create jobs.batch banjjak
check no patch deployments.apps/postgres banjjak
check no patch cronjobs.batch/backup banjjak
# 조치 표의 쓰기: execute 클러스터에서만 yes
act=no; [ "$mode" = execute ] && act=yes
check $act patch deployments.apps/web banjjak
check $act patch deployments.apps/web banjjak --subresource=scale
check $act patch cronjobs.batch/collector banjjak
check $act patch cronjobs.batch/analyzer banjjak
# 다른 네임스페이스 전부 (장애 주입 chaos는 #10 전이라 없어도 확인한다)
for ns in $( (kubectl --context "$ctx" get ns -o jsonpath='{.items[*].metadata.name}'; echo " chaos") | tr ' ' '\n' | sort -u); do
  [ "$ns" = banjjak ] || { check no list pods "$ns"; check no patch deployments.apps "$ns"; }
done
exit $bad
