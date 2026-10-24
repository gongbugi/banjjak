#!/usr/bin/env bash
# 공급 실측 (#11 측정 조건 댓글): 계단마다 k6를 따로 실행한다. 그 계단의 클라이언트 p95가 2초를 넘거나
# 오류(실패 + 못 보낸 요청)가 5%를 넘으면 다음 계단을 하지 않는다. 판정(p95 ≤ 500ms)은 서버 쪽 지표로 따로 한다.
# VM 밖(GitHub Actions 러너)에서 돈다 → 비공개 레포의 load 워크플로가 이 커밋을 받아 부른다
# 사용: bash deploy/load/ladder.sh <대상 URL> <계단 초> <초당 수...>
# 결과: $OUT_DIR/stages.tsv (계단 시작 · 끝은 epoch 초. 서버 쪽 판정 창을 여기서 잡는다)
set -uo pipefail
target=${1:?대상 URL} seconds=${2:?계단 초}; shift 2
for n in "$seconds" "$@"; do [[ $n =~ ^[0-9]+$ ]] || { echo "숫자가 아니다: $n"; exit 2; }; done
out=${OUT_DIR:-out}; mkdir -p "$out"; out=$(cd "$out" && pwd)
here=$(cd "$(dirname "$0")" && pwd)
# 러너 CPU: 바쁜 시간 · 쉰 시간 (/proc/stat). 80%를 넘은 계단은 러너가 한도라 무효로 본다
cpu() { awk '/^cpu /{print $2+$3+$4+$7+$8+$9, $5+$6}' /proc/stat; }
printf 'rate\tstart\tend\tp50_ms\tp95_ms\tsent\tfailed\tdropped\trunner_cpu\n' > "$out/stages.tsv"
for rate in "$@"; do
  rm -f "$out/$rate.txt"
  read -r busy0 idle0 < <(cpu); start=$(date +%s)
  docker run --rm --user "$(id -u)" -v "$here:/s:ro" -v "$out:/out" grafana/k6:2.3.0 run -q \
    -e TARGET="$target" -e RATE="$rate" -e STEP_SECONDS="$seconds" -e OUT="/out/$rate.txt" /s/step.js
  end=$(date +%s); read -r busy1 idle1 < <(cpu)
  if ! read -r p50 p95 sent failed dropped < "$out/$rate.txt" 2>/dev/null; then
    echo "멈춤: 초당 $rate 요약이 없다 (k6가 끝까지 못 돌았다)"; exit 1
  fi
  total=$(( busy1 - busy0 + idle1 - idle0 ))
  printf '%s\t%s\t%s\t%.0f\t%.0f\t%s\t%s\t%s\t%s\n' "$rate" "$start" "$end" "$p50" "$p95" "$sent" "$failed" "$dropped" \
    "$(( total > 0 ? 100 * (busy1 - busy0) / total : 0 ))" | tee -a "$out/stages.tsv"
  if awk -v p="$p95" -v f="$failed" -v d="$dropped" -v s="$sent" 'BEGIN { exit !(p > 2000 || f + d > 0.05 * (s + d)) }'; then
    echo "멈춤: 초당 $rate (p95 ${p95}ms · 실패 $failed · 못 보냄 $dropped / 보냄 $sent)"; break
  fi
done
