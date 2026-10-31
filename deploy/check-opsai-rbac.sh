#!/usr/bin/env bash
# 테스트 문 5: 운영 AI 계정(banjjak/ops-ai)의 권한이 banjjak 읽기 + (실행을 켠 클러스터만) 조치 표의 쓰기뿐인지 확인한다.
# 사용: bash deploy/check-opsai-rbac.sh <kubectl 컨텍스트> [dry-run|execute]   (기본 dry-run: 쓰기가 하나도 없어야 한다)
#       로컬 k3d와 Azure 앱 VM은 execute (쓰기 필드를 승인 정책이 막는지도 본다)
set -uo pipefail
ctx=${1:?kubectl context}
mode=${2:-dry-run}
as=system:serviceaccount:banjjak:ops-ai
bad=0
check() {   # check <yes|no> <verb> <resource[/name]> <namespace> [kubectl 옵션]
  got=$(kubectl --context "$ctx" auth can-i "$2" "$3" -n "$4" --as="$as" "${@:5}" 2>/dev/null)
  [ "$got" = "$1" ] && echo "ok   $1 $2 $3 -n $4 ${*:5}" || { echo "FAIL $2 $3 -n $4 ${*:5}: $got (기대 $1)"; bad=1; }
}
for r in pods pods/log deployments.apps replicasets.apps jobs.batch cronjobs.batch horizontalpodautoscalers.autoscaling; do check yes get "$r" banjjak; check yes list "$r" banjjak; done
check no get secrets banjjak
check no delete pods banjjak
check no create jobs.batch banjjak
check no patch deployments.apps/postgres banjjak
check no patch cronjobs.batch/backup banjjak
check no patch horizontalpodautoscalers.autoscaling/web banjjak   # HPA는 읽기만 (#11)
# 조치 표의 쓰기: execute 클러스터에서만 yes
act=no; [ "$mode" = execute ] && act=yes
check $act patch deployments.apps/web banjjak
check no patch deployments.apps/web banjjak --subresource=scale   # 레플리카는 HPA가 맡는다 (#11)
check $act patch cronjobs.batch/collector banjjak
check $act patch cronjobs.batch/analyzer banjjak
# 필드는 승인 정책(ValidatingAdmissionPolicy)이 막는다: 서버 dry-run으로 보내 본다. 실제로 바뀌는 것은 없다
admit() {   # admit <yes|no> <대상> <JSON patch>
  out=$(kubectl --context "$ctx" -n banjjak patch "$2" --as="$as" --dry-run=server --type=json -p "$3" 2>&1)
  case "$out" in *"denied request"*) got=no ;; *patched*) got=yes ;; *) got="오류: $out" ;; esac
  [ "$got" = "$1" ] && echo "ok   $1 $2 $3" || { echo "FAIL $2 $3: $got (기대 $1)"; bad=1; }
}
if [ "$mode" = execute ]; then
  web=/spec/template/spec
  own=$(kubectl --context "$ctx" -n banjjak get deploy web -o jsonpath='{.spec.template.spec.containers[0].image}')
  admit yes deployment/web "[{\"op\":\"replace\",\"path\":\"$web/containers/0/image\",\"value\":\"${own%:*}:other\"}]"
  admit yes deployment/web "[{\"op\":\"add\",\"path\":\"$web/containers/0/env/-\",\"value\":{\"name\":\"PLAIN\",\"value\":\"1\"}}]"
  admit no deployment/web "[{\"op\":\"replace\",\"path\":\"$web/containers/0/image\",\"value\":\"evil.example/banjjak:1\"}]"
  admit no deployment/web "[{\"op\":\"replace\",\"path\":\"$web/initContainers/0/image\",\"value\":\"busybox:1\"}]"
  admit no deployment/web "[{\"op\":\"add\",\"path\":\"$web/containers/0/command\",\"value\":[\"sh\",\"-c\",\"true\"]}]"
  admit no deployment/web "[{\"op\":\"add\",\"path\":\"$web/containers/0/env/-\",\"value\":{\"name\":\"X\",\"valueFrom\":{\"secretKeyRef\":{\"name\":\"github-token\",\"key\":\"token\"}}}}]"
  admit no deployment/web "[{\"op\":\"add\",\"path\":\"$web/serviceAccountName\",\"value\":\"ops-ai\"}]"
  admit no deployment/web "[{\"op\":\"add\",\"path\":\"$web/volumes/-\",\"value\":{\"name\":\"x\",\"emptyDir\":{}}}]"
  admit no deployment/web "[{\"op\":\"add\",\"path\":\"$web/hostNetwork\",\"value\":true}]"
  admit no deployment/web "[{\"op\":\"add\",\"path\":\"$web/containers/0/lifecycle\",\"value\":{\"postStart\":{\"exec\":{\"command\":[\"sh\",\"-c\",\"true\"]}}}}]"
  admit no deployment/web "[{\"op\":\"add\",\"path\":\"$web/containers/0/startupProbe\",\"value\":{\"exec\":{\"command\":[\"sh\",\"-c\",\"true\"]}}}]"
  admit no deployment/web "[{\"op\":\"add\",\"path\":\"$web/hostAliases\",\"value\":[{\"ip\":\"10.0.0.9\",\"hostnames\":[\"postgres\"]}]}]"
  admit no deployment/web "[{\"op\":\"add\",\"path\":\"$web/containers/0/env/-\",\"value\":{\"name\":\"DATABASE_URL\",\"value\":\"x\"}}]"
  admit yes cronjob/collector '[{"op":"add","path":"/spec/suspend","value":false}]'
  admit no cronjob/collector '[{"op":"add","path":"/spec/suspend","value":true}]'
  admit no cronjob/analyzer '[{"op":"replace","path":"/spec/schedule","value":"* * * * *"}]'
fi
# 다른 네임스페이스 전부 (장애 주입 chaos는 #10 전이라 없어도 확인한다)
for ns in $( (kubectl --context "$ctx" get ns -o jsonpath='{.items[*].metadata.name}'; echo " chaos") | tr ' ' '\n' | sort -u); do
  [ "$ns" = banjjak ] || { check no list pods "$ns"; check no patch deployments.apps "$ns"; }
done
exit $bad
