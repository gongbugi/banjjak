#!/usr/bin/env bash
# 사용: deploy/apply.sh <kubectl 컨텍스트> [이미지 태그=현재 커밋 SHA]
#       로컬 k3d 시험: IMAGE=banjjak deploy/apply.sh k3d-banjjak local  (k3d image import로 넣은 이미지)
# deploy/clusters/<컨텍스트>(없으면 deploy/k8s)를 쓰고 이미지 태그만 고정한 임시 오버레이로 적용한다.
# 운영 VM 클러스터는 이 스크립트가 아니라 deploy/ops로 따로 올린다.
# ponytail: CD 없이 손으로 배포한다. 태그의 이미지가 GHCR에 있는지(CI가 끝났는지)는 확인하지 않는다 → 나중에 CD로
set -euo pipefail
ctx=${1:?kubectl context}
tag=${2:-$(git rev-parse HEAD)}
here=$(cd "$(dirname "$0")" && pwd)
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
kubectl --context "$ctx" -n banjjak annotate deploy/web --overwrite \
  kubernetes.io/change-cause="deploy ${tag:0:12} at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
kubectl --context "$ctx" -n banjjak rollout status deploy/web --timeout=300s
