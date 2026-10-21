#!/usr/bin/env bash
# 사용: deploy/apply.sh <kubectl 컨텍스트> [이미지 태그=현재 커밋 SHA]
#       로컬 k3d 시험: IMAGE=banjjak deploy/apply.sh k3d-banjjak local  (k3d image import로 넣은 이미지)
# deploy/clusters/<컨텍스트>(없으면 deploy/k8s)를 쓰고 이미지 태그만 고정한 임시 오버레이로 적용한다.
# 앱 VM(azure-banjjak) · 운영 VM(azure-banjjak-ops) · 로컬(k3d-banjjak) 모두 이것으로 올린다.
# ponytail: CD 없이 손으로 배포한다 → 나중에 CD로
set -euo pipefail
ctx=${1:?kubectl context}
tag=${2:-$(git rev-parse HEAD)}
here=$(cd "$(dirname "$0")" && pwd)

# GHCR 이미지를 올릴 때는 그 커밋의 CI 작업이 전부 "끝남 + 성공"이어야 한다.
# run 전체 결론만 보면 안 된다: 작업이 도는 중인데 run이 success로 보인 적이 있다 (2026-10-21)
if [ -z "${IMAGE:-}" ]; then
  run=$(gh run list --commit "$tag" --limit 1 --json databaseId -q '.[0].databaseId')
  [ -n "$run" ] || { echo "CI run 없음: ${tag:0:7} (방금 push했으면 잠시 뒤 다시)"; exit 1; }
  bad=$(gh run view "$run" --json jobs -q '.jobs[] | select(.status != "completed" or .conclusion != "success") | "\(.name) \(.status) \(.conclusion)"')
  [ -z "$bad" ] || { echo "CI가 아직 초록이 아니다 (${tag:0:7}):"; echo "$bad"; exit 1; }
fi
src=k8s
[ -d "$here/clusters/$ctx" ] && src="clusters/$ctx"   # 클러스터마다 다른 값(지표를 보낼 곳)
mkdir -p "$here/.pin"
cat > "$here/.pin/kustomization.yaml" <<EOF
resources: [../$src]
images:
  - name: ghcr.io/gongbugi/banjjak
    newName: "${IMAGE:-ghcr.io/gongbugi/banjjak}"
    newTag: "$tag"
EOF
kubectl --context "$ctx" apply -k "$here/.pin"
# 배포 기록: 언제 어떤 커밋을 올렸나를 배포 이력(rollout history)에 남긴다. 운영 AI의 "최근 배포" 증거가 된다
# 대상은 우리 이미지를 쓰는 Deployment 전부 (앱 VM은 web, 운영 VM은 ops-ai)
image="${IMAGE:-ghcr.io/gongbugi/banjjak}:$tag"
kubectl --context "$ctx" get deploy -A \
  -o jsonpath='{range .items[*]}{.metadata.namespace} {.metadata.name} {.spec.template.spec.containers[0].image}{"\n"}{end}' |
  while read -r ns name img; do
    [ "$img" = "$image" ] || continue
    kubectl --context "$ctx" -n "$ns" annotate "deploy/$name" --overwrite \
      kubernetes.io/change-cause="deploy ${tag:0:12} at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    kubectl --context "$ctx" -n "$ns" rollout status "deploy/$name" --timeout=300s
  done
