#!/usr/bin/env bash
# on-vm.sh 시험: 가짜 curl · docker · systemctl로 돌린다. 실패하면 "deployed" 줄이 없어야 한다 (CD는 그 줄로만 성공을 판단한다)
# 사용: bash deploy/on-vm_test.sh
set -uo pipefail
here=$(cd "$(dirname "$0")" && pwd)
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/bin" "$tmp/src/x/deploy/compose/systemd"
echo new > "$tmp/src/x/deploy/compose/compose.yaml"
echo unit > "$tmp/src/x/deploy/compose/systemd/banjjak-x.timer"
tar czf "$tmp/repo.tar.gz" -C "$tmp/src" x
sha=abc123def456 old=old999

# readyz: FAIL=ready면 새 커밋 web만 준비 안 됨 (되돌린 옛 커밋은 답한다)
cat > "$tmp/bin/curl" <<EOF
#!/usr/bin/env bash
case "\$*" in
  *archive*) [ "\${FAIL:-}" = curl ] && exit 22; cat "$tmp/repo.tar.gz" ;;
  *readyz*) [ "\${FAIL:-}" = ready ] && [ "\$(cat "$tmp/up.sha")" = "$sha" ] && exit 7; exit 0 ;;
esac
EOF
cat > "$tmp/bin/docker" <<EOF
#!/usr/bin/env bash
echo "docker \$* SHA=\${BANJJAK_SHA:-}" >> "$tmp/calls"
case "\$*" in
  *" pull "*) [ "\${FAIL:-}" != pull ] ;;
  *" up -d"*) echo "\$BANJJAK_SHA" > "$tmp/up.sha"; [ "\${FAIL:-}" != up ] || [ "\$BANJJAK_SHA" != "$sha" ] ;;
  "image ls "*) printf 'ghcr.io/gongbugi/banjjak:%s\n' "$old" "$sha" ;;
esac
EOF
printf '#!/usr/bin/env bash\necho "systemctl $*" >> "%s/calls"\n' "$tmp" > "$tmp/bin/systemctl"
printf '#!/usr/bin/env bash\n' > "$tmp/bin/sleep"
chmod +x "$tmp/bin/"*

run() {
  rm -rf "$tmp/calls" "$tmp/up.sha" "$tmp/opt" "$tmp/units"; mkdir -p "$tmp/opt" "$tmp/units"
  echo old > "$tmp/opt/compose.yaml"; echo "BANJJAK_SHA=$old" > "$tmp/opt/deployed.env"; touch "$tmp/calls"
  FAIL=$1 PATH="$tmp/bin:$PATH" OPT="$tmp/opt" UNITS="$tmp/units" sh "$here/on-vm.sh" "$sha" > "$tmp/log" 2>&1
}
bad=0
no() { echo "FAIL $1"; cat "$tmp/log"; bad=1; }
kept() { [ "$(cat "$tmp/opt/deployed.env")" = "BANJJAK_SHA=$old" ] && [ "$(cat "$tmp/opt/compose.yaml")" = old ]; }

run ""
if [ $? -eq 0 ] && [ "$(tail -n 1 "$tmp/log")" = "deployed $sha" ]; then echo "ok   성공하면 마지막 줄이 deployed <SHA>"; else no "성공 줄"; fi
[ "$(cat "$tmp/opt/deployed.env")" = "BANJJAK_SHA=$sha" ] && [ "$(cat "$tmp/opt/compose.yaml")" = new ] && [ -f "$tmp/units/banjjak-x.timer" ] &&
  echo "ok   그 커밋의 compose · unit을 깔고 작업도 그 커밋으로" || no "깔기"
[ "$(grep -n ' pull ' "$tmp/calls" | cut -d: -f1)" -lt "$(grep -n ' up -d' "$tmp/calls" | cut -d: -f1)" ] && grep -q " pull .*SHA=$sha" "$tmp/calls" &&
  echo "ok   올리기 전에 그 커밋 이미지를 받음" || no "받기 순서"
grep -q "image rm ghcr.io/gongbugi/banjjak:$old" "$tmp/calls" && ! grep -q "image rm .*:$sha" "$tmp/calls" &&
  echo "ok   옛 커밋 이미지만 지움" || no "이미지 지우기"
grep -q "systemctl daemon-reload" "$tmp/calls" && echo "ok   systemd가 새 unit을 다시 읽음" || no "daemon-reload"

for step in curl pull; do
  run "$step"
  if [ $? -ne 0 ] && ! grep -q "^deployed" "$tmp/log" && ! grep -q " up -d" "$tmp/calls" && kept; then
    echo "ok   $step 실패면 도는 것을 건드리지 않고 deployed 줄 없음"; else no "$step 실패"; fi
done
for step in up ready; do
  run "$step"
  if [ $? -ne 0 ] && ! grep -q "^deployed" "$tmp/log" && [ "$(cat "$tmp/up.sha")" = "$old" ] && kept && grep -q "되돌림 ok" "$tmp/log"; then
    echo "ok   $step 실패면 앞 커밋으로 되돌리고 deployed 줄 없음"; else no "$step 실패"; fi
done
exit $bad
