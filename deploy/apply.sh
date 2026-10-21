#!/usr/bin/env bash
# 사용: deploy/apply.sh <kubectl 컨텍스트> [이미지 태그=현재 커밋 SHA]
#       로컬 k3d 시험: IMAGE=banjjak deploy/apply.sh k3d-banjjak local  (k3d image import로 넣은 이미지)
# deploy/k8s를 그대로 쓰고 이미지 태그만 고정한 임시 오버레이로 적용한다.
# ponytail: CD 없이 손으로 배포한다. 태그의 이미지가 GHCR에 있는지(CI가 끝났는지)는 확인하지 않는다 → 나중에 CD로
set -euo pipefail
ctx=${1:?kubectl context}
tag=${2:-$(git rev-parse HEAD)}
here=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$here/.pin"
cat > "$here/.pin/kustomization.yaml" <<EOF
resources: [../k8s]
images:
  - name: ghcr.io/gongbugi/banjjak
    newName: "${IMAGE:-ghcr.io/gongbugi/banjjak}"
    newTag: "$tag"
EOF
kubectl --context "$ctx" apply -k "$here/.pin"
kubectl --context "$ctx" -n banjjak rollout status deploy/web --timeout=300s
