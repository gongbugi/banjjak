#!/usr/bin/env bash
# ladder.sh 멈춤 규칙 시험 (#11 측정 조건): 가짜 docker가 계단마다 정해 둔 요약 한 줄을 쓴다. 하나라도 어긋나면 실패
# 사용: bash deploy/load/ladder_test.sh
set -uo pipefail
here=$(cd "$(dirname "$0")" && pwd)
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
cat > "$tmp/docker" <<'EOF'
#!/usr/bin/env bash
# 가짜 k6. 요약 = p50 p95(ms) 보낸 수 실패 못 보냄
for a in "$@"; do
  case $a in RATE=*) rate=${a#RATE=};; OUT=/out/*) name=${a#OUT=/out/};; *:/out) host=${a%:/out};; esac
done
case $rate in
  5)  line="100 300 600 0 0" ;;      # 통과
  10) line="100 1900 1200 59 0" ;;   # 통과: p95 2초 이하 · 오류 59/1200 = 4.9%
  20) line="100 400 2400 100 30" ;;  # 멈춤: 오류 (100+30)/(2400+30) = 5.3% (못 보낸 요청도 오류)
  40) line="100 2500 4800 0 0" ;;    # 멈춤: p95 2초 초과
  7)  exit 1 ;;                      # k6가 요약 없이 죽음
esac
echo "$line" > "$host/$name"
EOF
chmod +x "$tmp/docker"
bad=0
ladder() { rm -rf "$tmp/out"; PATH="$tmp:$PATH" OUT_DIR="$tmp/out" bash "$here/ladder.sh" http://x 1 "$@" > "$tmp/log" 2>&1; }
expect() {   # expect <설명> <기대 계단> <기대 멈춤 줄>
  got=$(tail -n +2 "$tmp/out/stages.tsv" | cut -f1 | tr '\n' ' ')
  if [ "$got" = "$2" ] && grep -q "$3" "$tmp/log"; then echo "ok   $1"; else echo "FAIL $1: 계단 [$got] (기대 [$2])"; cat "$tmp/log"; bad=1; fi
}
ladder 5 10 20 30; expect "오류 5% 초과에서 멈춤 (못 보낸 요청 포함)" "5 10 20 " "멈춤: 초당 20"
ladder 5 40 50;    expect "p95 2초 초과에서 멈춤" "5 40 " "멈춤: 초당 40"
ladder 5 7 10
if [ $? -ne 0 ] && grep -q "요약이 없다" "$tmp/log"; then echo "ok   요약 없으면 실패로 끝남"; else echo "FAIL 요약 없음"; cat "$tmp/log"; bad=1; fi
ladder 5 '10;x'
if [ $? -ne 0 ] && grep -q "숫자가 아니다" "$tmp/log"; then echo "ok   숫자가 아닌 입력은 거절"; else echo "FAIL 입력 검사"; cat "$tmp/log"; bad=1; fi
exit $bad
