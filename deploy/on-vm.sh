#!/bin/sh
# CD가 az vm run-command로 VM 안에서 root로 돌린다: on-vm.sh <kubectl 컨텍스트> <커밋 SHA>  (.github/workflows/deploy.yml)
# 그 커밋의 deploy/apply.sh로 이 VM의 k3s에 올린다. 손으로 배포할 때와 같은 스크립트라 클러스터에 남는 기록도 같다.
# 마지막 줄 "deployed <SHA>"가 성공 표시다: run-command는 스크립트가 실패해도 성공을 돌려준다.
# sh로 돌 수 있어 bash 전용 문법을 쓰지 않는다. curl이 실패하면 tar가 빈 입력으로 실패한다.
set -eu
ctx=$1 sha=$2
export HOME=/root PATH="${PATH:-/usr/bin:/bin}:/usr/local/bin"
# IMAGE를 주면 apply.sh가 gh로 CI를 확인하지 않는다 (VM에는 gh가 없고, 이미지가 있는지는 워크플로가 확인했다)
export IMAGE="${IMAGE:-ghcr.io/gongbugi/banjjak}"
dir=$(mktemp -d)
trap 'rm -rf "$dir"' EXIT
curl -fsSL "https://github.com/gongbugi/banjjak/archive/$sha.tar.gz" | tar xzf - --strip-components=1 -C "$dir"
export KUBECONFIG="$dir/kubeconfig"
cp "${K3S_CONFIG:-/etc/rancher/k3s/k3s.yaml}" "$KUBECONFIG"
kubectl config rename-context default "$ctx" > /dev/null
bash "$dir/deploy/apply.sh" "$ctx" "$sha"
echo "deployed $sha"
