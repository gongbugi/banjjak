#!/bin/sh
# 11-20 한 번: 앱 VM을 k3s에서 compose로 옮긴다. PC에서 az vm run-command로 VM 안에서 root로 돌린다:
#   az vm run-command invoke -g rg-banjjak -n banjjak-vm --command-id RunShellScript \
#     --scripts @deploy/compose/from-k3s.sh --parameters <단계> [커밋 SHA 40자] --query 'value[0].message' -o tsv
# 단계 (4GB에서 확인한 뒤에 VM 크기를 줄인다, 본인 11-20):
#   prep <SHA>  k3s가 서비스하는 동안 준비: Docker 설치 · compose 파일 · 비밀값 파일 · 이미지 받기. 끊김 없음
#   switch      끊김 몇 분: CronJob 멈춤 → DB 덤프 → k3s 끄기 → compose 올리기 → 덤프 복원 → timer 켜기
#   status      메모리 · 컨테이너 · timer · 비밀 파일을 비루트가 읽는지 (크기만)
#   back        switch 뒤 문제가 있으면 k3s로 되돌린다. k3s는 지우지 않고 꺼 두기만 했다 (compose 동안 쌓인 기록은 버린다)
# 마지막 줄 "<단계> ok"가 성공 표시다 (run-command는 스크립트가 실패해도 성공을 돌려준다). 출력이 4KB에서 잘려 긴 출력은 로그 파일로.
# 비밀값은 화면에 찍지 않는다: k8s Secret에서 파일로 바로 옮기고 크기만 본다.
# sh로 돈다 (on-vm.sh와 같다): bash 전용 문법을 쓰지 않는다.
set -eu
export HOME=/root PATH="${PATH:-/usr/bin:/bin}:/usr/local/bin"
log=/root/from-k3s.log
trap 'rc=$?; [ $rc -eq 0 ] || { echo "실패 ($rc), 로그 끝:"; tail -n 25 $log; }' EXIT
C="docker compose -f /opt/banjjak/compose.yaml"
K="k3s kubectl -n banjjak"
S=/etc/banjjak/secrets
JOBS="collector analyzer rising backup"
TIMERS="banjjak-collector.timer banjjak-analyzer.timer banjjak-rising.timer banjjak-backup.timer"
DUMP=/root/banjjak-move.dump
[ ! -f /opt/banjjak/deployed.env ] || { . /opt/banjjak/deployed.env; export BANJJAK_SHA; }

put() {   # put <Secret> <키> <파일>: 값은 파이프로만 지난다. 키가 없으면 빈 파일 (compose의 선택 비밀값)
  $K get secret "$1" -o "jsonpath={.data.$2}" | base64 -d > "$S/$3"
  chmod 444 "$S/$3"
}

case $1 in
prep)
  sha=$2
  echo "== $(date -u +%FT%TZ) prep $sha" >> $log
  # Ubuntu 저장소 패키지 (VM에 이미 있는 apt 출처). k3s의 containerd는 /run/k3s에 따로 있어 겹치지 않는다
  # 잠금은 5분까지 기다린다 (unattended-upgrades). needrestart는 목록만 (k3s 같은 서비스를 저절로 다시 띄우지 않게)
  apt-get -o DPkg::Lock::Timeout=300 update -q >> $log 2>&1
  DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l \
    apt-get -o DPkg::Lock::Timeout=300 install -y -q docker.io docker-compose-v2 >> $log 2>&1
  docker --version; docker compose version
  src=/root/from-k3s.src; rm -rf $src; mkdir $src
  curl -fsSL "https://github.com/gongbugi/banjjak/archive/$sha.tar.gz" | tar xzf - --strip-components=1 -C $src
  install -d -m 755 /opt/banjjak
  install -m 644 $src/deploy/compose/compose.yaml /opt/banjjak/compose.yaml
  install -m 644 $src/deploy/compose/systemd/* /etc/systemd/system/
  rm -rf $src
  echo "BANJJAK_SHA=$sha" > /opt/banjjak/deployed.env
  export BANJJAK_SHA=$sha
  install -d -m 700 $S
  put db url db-url
  put db password db-password
  put github-token token github-token
  put anthropic api_key anthropic-api-key
  put heartbeats collector heartbeats-collector
  put heartbeats analyzer heartbeats-analyzer
  put heartbeats backup heartbeats-backup
  put backup-blob container_url backup-blob-container-url
  put backup-blob sas backup-blob-sas
  for f in db-url db-password github-token; do [ -s $S/$f ] || { echo "$f 비었다"; exit 1; }; done
  wc -c $S/* | sed "s#$S/##"
  # -n ... p: 모양이 다르면 아무것도 찍지 않는다 (통째로 찍히면 비밀번호가 나온다)
  echo "db-url 호스트: $(sed -nE 's#^[a-z0-9+]*://.*@([^/:@]*).*#\1#p' $S/db-url) (postgres여야 한다)"
  $C config -q
  $C --profile job pull -q >> $log 2>&1
  docker image ls --format '{{.Repository}}:{{.Tag}} {{.Size}}' | cut -c1-90
  echo "prep ok"
  ;;
switch)
  echo "== $(date -u +%FT%TZ) switch" >> $log
  for j in $JOBS; do $K patch cronjob $j -p '{"spec":{"suspend":true}}' > /dev/null; done
  # 도는 작업이 있으면 끝날 때까지 기다린다 (최대 10분 = 작업 시간 제한). 넘으면 멈춤만 남기고 끝낸다 → 다시 switch
  i=0
  while [ -n "$($K get pods -l job-name --field-selector=status.phase!=Succeeded,status.phase!=Failed -o name)" ]; do
    i=$((i + 1)); [ $i -le 60 ] || { echo "작업이 아직 돈다. 잠시 뒤 다시 switch"; exit 1; }; sleep 10
  done
  $K exec deploy/postgres -- pg_dump -U banjjak -d banjjak -Fc > $DUMP
  [ -s $DUMP ]; echo "덤프 $(wc -c < $DUMP) B"
  # 여기부터 끊김: k3s를 끄고(지우지 않음) 80번을 비운다
  systemctl disable --now k3s >> $log 2>&1
  /usr/local/bin/k3s-killall.sh >> $log 2>&1
  $C up -d --wait postgres >> $log 2>&1
  # 빈 DB에 복원한 뒤 나머지를 올린다 (먼저 올리면 migrate가 빈 표를 만들어 복원과 부딪친다)
  $C exec -T postgres pg_restore -U banjjak -d banjjak --no-owner --exit-on-error < $DUMP
  # --wait는 끝나고 사라지는 migrate를 compose 버전에 따라 실패로 볼 수 있어 web 준비를 직접 기다린다 (web은 migrate가 성공해야 뜬다)
  $C up -d >> $log 2>&1
  i=0
  until curl -fsS -m 3 -o /dev/null http://127.0.0.1/readyz; do
    i=$((i + 1)); [ $i -le 30 ] || { echo "web이 준비 안 됨"; exit 1; }; sleep 2
  done
  systemctl daemon-reload
  systemctl enable --now $TIMERS >> $log 2>&1
  curl -fsS -m 10 -o /dev/null -w 'healthz %{http_code}\n' http://127.0.0.1/healthz
  $C ps --format '{{.Service}} {{.Status}}'
  echo "switch ok"
  ;;
status)
  free -m | head -2
  $C ps --format '{{.Service}} {{.Status}}'
  docker stats --no-stream --format '{{.Name}} {{.MemUsage}}'
  systemctl list-timers 'banjjak-*' --no-pager | head -6
  echo "k3s: $(systemctl is-enabled k3s 2>/dev/null || true) / $(systemctl is-active k3s 2>/dev/null || true)"
  # 🟡 11-09 확인 못 한 것: 444 파일을 컨테이너의 비루트 사용자가 읽는가 (값이 아니라 크기만)
  $C run --rm --no-deps --entrypoint sh analyzer -c 'echo "uid $(id -u)"; wc -c /run/secrets/*' 2>> $log
  curl -fsS -m 10 -o /dev/null -w 'healthz %{http_code}\n' http://127.0.0.1/healthz
  echo "status ok"
  ;;
back)
  echo "== $(date -u +%FT%TZ) back" >> $log
  systemctl disable --now $TIMERS >> $log 2>&1 || true
  $C down >> $log 2>&1 || true   # 볼륨은 남긴다
  systemctl enable --now k3s >> $log 2>&1
  i=0
  until $K rollout status deploy/web --timeout=10s > /dev/null 2>&1; do
    i=$((i + 1)); [ $i -le 30 ] || { echo "web이 안 뜬다"; exit 1; }; sleep 10
  done
  for j in $JOBS; do $K patch cronjob $j -p '{"spec":{"suspend":false}}' > /dev/null; done
  echo "back ok"
  ;;
*)
  echo "단계: prep <SHA> | switch | status | back"; exit 2
  ;;
esac
