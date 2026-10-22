#!/usr/bin/env bash
# 배포(웹 파드 교체) 동안 요청이 끊기지 않는지: 요청을 계속 보내며 web을 rollout restart 하고, 200이 아닌 응답을 센다.
# 사용: bash deploy/check-rollout.sh <kubectl 컨텍스트> <웹 주소>
#   로컬: bash deploy/check-rollout.sh k3d-banjjak http://localhost:8080
#   운영: 웹 파드가 한 번 바뀐다 (배포와 같다). 배포하면서 보려면 RESTART=no로 요청만 보내고 그동안 deploy/apply.sh를 돌린다
set -uo pipefail
ctx=${1:?kubectl context}; url=${2:?web url}
out=$(mktemp)
# /healthz: 입구(Traefik) → 웹 파드까지 같은 길이고 DB를 건드리지 않는다
( end=$((SECONDS + ${DURATION:-60})); while [ $SECONDS -lt $end ]; do
    curl -s -o /dev/null -w "%{http_code}\n" --max-time 3 "$url/healthz" >> "$out"; done ) &
loop=$!
if [ "${RESTART:-yes}" = yes ]; then
  kubectl --context "$ctx" -n banjjak rollout restart deploy/web > /dev/null
  kubectl --context "$ctx" -n banjjak rollout status deploy/web --timeout=180s > /dev/null
fi
wait $loop
echo "요청 $(wc -l < "$out")건:"; sort "$out" | uniq -c
bad=$(grep -vc '^200$' "$out"); rm -f "$out"
[ "$bad" -eq 0 ]
