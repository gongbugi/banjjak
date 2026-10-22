#!/usr/bin/env bash
# 테스트 문 5: 운영 AI 계정(banjjak/ops-ai)의 권한이 banjjak 읽기뿐인지 확인한다. 하나라도 어긋나면 실패.
# 사용: bash deploy/check-opsai-rbac.sh <kubectl 컨텍스트>   (앱 클러스터 · 로컬 k3d)
set -uo pipefail
ctx=${1:?kubectl context}
as=system:serviceaccount:banjjak:ops-ai
bad=0
check() {   # check <yes|no> <verb> <resource> <namespace>
  got=$(kubectl --context "$ctx" auth can-i "$2" "$3" -n "$4" --as="$as" 2>/dev/null)
  [ "$got" = "$1" ] && echo "ok   $1 $2 $3 -n $4" || { echo "FAIL $2 $3 -n $4: $got (기대 $1)"; bad=1; }
}
for r in pods pods/log deployments.apps jobs.batch cronjobs.batch; do check yes get "$r" banjjak; check yes list "$r" banjjak; done
check no get secrets banjjak
check no delete pods banjjak
check no patch deployments.apps banjjak
check no create jobs.batch banjjak
# 다른 네임스페이스 전부 (장애 주입 chaos는 #10 전이라 없어도 확인한다)
for ns in $( (kubectl --context "$ctx" get ns -o jsonpath='{.items[*].metadata.name}'; echo " chaos") | tr ' ' '\n' | sort -u); do
  [ "$ns" = banjjak ] || check no list pods "$ns"
done
exit $bad
