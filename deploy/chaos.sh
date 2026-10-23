#!/usr/bin/env bash
# 장애 주입기를 손으로 한 번 돌린다 (낮 시험 주입 · 정답 보기). CronJob과 같은 디스크 · 토큰 · 스위치를 쓴다.
# 사용: bash deploy/chaos.sh <kubectl 컨텍스트> inject <종류> | status
#   종류: web_pod_kill · db_pod_kill · latency · errors · bad_deploy · collector_suspend
# ⚠️ status는 정답 기록이다. 실험 중에는 #12 채점 전까지 보지 않는다
set -euo pipefail
ctx=${1:?kubectl context}; shift
name="chaos-manual-$(date +%s)"
tmp=$(mktemp)
kubectl --context "$ctx" -n chaos create job "$name" --from=cronjob/injector --dry-run=client -o yaml > "$tmp"
# command [python, -m, banjjak.chaos] 뒤에 인자를 붙인다 (같은 들여쓰기로)
awk -v extra="$*" '{print} /- banjjak\.chaos$/ {match($0, /^ */); ind = substr($0, 1, RLENGTH);
  n = split(extra, a, " "); for (i = 1; i <= n; i++) print ind "- " a[i]}' "$tmp" > "$tmp.job"
kubectl --context "$ctx" -n chaos create -f "$tmp.job" > /dev/null
kubectl --context "$ctx" -n chaos wait --for=condition=complete "job/$name" --timeout=120s > /dev/null || true
kubectl --context "$ctx" -n chaos logs "job/$name"
kubectl --context "$ctx" -n chaos delete job "$name" > /dev/null
rm -f "$tmp" "$tmp.job"
