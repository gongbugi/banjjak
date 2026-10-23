#!/usr/bin/env bash
# 테스트 문 5: 장애 주입기 계정(banjjak/chaos)은 주입에 필요한 것만 할 수 있고,
# 운영 AI 계정(banjjak/ops-ai)은 장애 스위치(ConfigMap web-fault, 정답)를 못 읽는지 확인한다. 하나라도 어긋나면 실패.
# 사용: bash deploy/check-chaos-rbac.sh <kubectl 컨텍스트>   (앱 클러스터 · 로컬 k3d)
set -uo pipefail
ctx=${1:?kubectl context}
bad=0
check() {   # check <계정> <yes|no> <verb> <resource[/name]> <namespace>
  got=$(kubectl --context "$ctx" auth can-i "$3" "$4" -n "$5" --as="system:serviceaccount:banjjak:$1" 2>/dev/null)
  [ "$got" = "$2" ] && echo "ok   $1: $2 $3 $4 -n $5" || { echo "FAIL $1: $3 $4 -n $5: $got (기대 $2)"; bad=1; }
}
# 주입기: 넣을 수 있는 것
check chaos yes list pods banjjak
check chaos yes delete pods banjjak
check chaos yes get deployments.apps/web banjjak
check chaos yes patch deployments.apps/web banjjak
check chaos yes patch cronjobs.batch/collector banjjak
check chaos yes patch configmaps/web-fault banjjak
# 주입기: 그 밖은 없다
check chaos no get secrets banjjak
check chaos no patch deployments.apps/postgres banjjak
check chaos no delete deployments.apps/web banjjak
check chaos no patch cronjobs.batch/backup banjjak
check chaos no patch configmaps/other banjjak
check chaos no create pods banjjak
check chaos no create jobs.batch banjjak
for ns in $(kubectl --context "$ctx" get ns -o jsonpath='{.items[*].metadata.name}'); do
  [ "$ns" = banjjak ] || check chaos no delete pods "$ns"
done
# 운영 AI는 스위치 내용(정답)을 못 읽는다
check ops-ai no get configmaps/web-fault banjjak
check ops-ai no list configmaps banjjak
exit $bad
