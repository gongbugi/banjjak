#!/bin/sh
# CD가 az vm run-command로 앱 VM 안에서 root로 돌린다: on-vm.sh <커밋 SHA>  (.github/workflows/deploy.yml)
# 11-20부터 앱 VM은 k3s 대신 compose다 (deploy/compose/). 그 커밋의 compose 파일 · systemd unit을 깔고 이미지를 받아 올린다.
# compose는 k8s처럼 새 web이 뜰 때까지 옛 것을 두지 않는다 → 새 web이 준비 안 되면 앞 커밋으로 되돌린다.
# ponytail: 스키마를 바꾼 커밋이 migrate 뒤에 실패하면 옛 코드가 새 스키마로 돈다 (downgrade는 자동으로 안 한다)
# 마지막 줄 "deployed <SHA>"가 성공 표시다: run-command는 스크립트가 실패해도 성공을 돌려준다.
# sh로 돌 수 있어 bash 전용 문법을 쓰지 않는다. curl이 실패하면 tar가 빈 입력으로 실패한다.
# OPT · UNITS는 시험(on-vm_test.sh)이 깔 곳을 바꿀 때만 쓴다
set -eu
sha=$1
export HOME=/root PATH="${PATH:-/usr/bin:/bin}:/usr/local/bin"
opt=${OPT:-/opt/banjjak} units=${UNITS:-/etc/systemd/system}
C="docker compose -f $opt/compose.yaml"
old=$(sed -n 's/^BANJJAK_SHA=//p' "$opt/deployed.env")
dir=$(mktemp -d)
trap 'rm -rf "$dir"' EXIT
curl -fsSL "https://github.com/gongbugi/banjjak/archive/$sha.tar.gz" | tar xzf - --strip-components=1 -C "$dir"
# 받기는 도는 것을 건드리지 않는다: 여기서 실패하면 옛 것이 그대로 돈다
BANJJAK_SHA=$sha docker compose -f "$dir/deploy/compose/compose.yaml" --profile job pull -q
cp "$opt/compose.yaml" "$dir/compose.yaml.old"
install -m 644 "$dir/deploy/compose/compose.yaml" "$opt/compose.yaml"
install -m 644 "$dir"/deploy/compose/systemd/* "$units/"
systemctl daemon-reload

ready() {   # up이 끝나면 web은 새 컨테이너다 (migrate가 성공해야 뜬다). 그 web이 /readyz(DB까지)에 답할 때까지
  BANJJAK_SHA=$1 $C up -d || return 1
  i=0
  until curl -fsS -m 3 -o /dev/null http://127.0.0.1/readyz; do
    i=$((i + 1)); [ $i -le 30 ] || return 1; sleep 2
  done
}
if ! ready "$sha"; then
  echo "새 버전이 안 뜸 (compose up 실패 또는 web 준비 안 됨) → ${old:-?}로 되돌린다"
  install -m 644 "$dir/compose.yaml.old" "$opt/compose.yaml"
  [ -n "$old" ] && ready "$old" && echo "되돌림 ok" || echo "되돌림도 실패"
  exit 1
fi
echo "BANJJAK_SHA=$sha" > "$opt/deployed.env"   # 작업 timer가 이 커밋으로 돈다
# 옛 커밋 이미지는 배포마다 하나씩 쌓인다. 지우다 실패해도 배포는 성공이다
docker image ls ghcr.io/gongbugi/banjjak --format '{{.Repository}}:{{.Tag}}' | grep -v ":$sha\$" |
  xargs -r docker image rm > /dev/null 2>&1 || true
echo "deployed $sha"
