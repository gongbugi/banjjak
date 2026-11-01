#!/usr/bin/env bash
# on-vm.sh 시험: 가짜 curl · kubectl · gh로 돌린다. 실패하면 "deployed" 줄이 없어야 한다 (CD는 그 줄로만 성공을 판단한다)
# 사용: bash deploy/on-vm_test.sh
set -uo pipefail
here=$(cd "$(dirname "$0")" && pwd)
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/bin" "$tmp/src/x/deploy"
cp "$here/apply.sh" "$tmp/src/x/deploy/"
tar czf "$tmp/repo.tar.gz" -C "$tmp/src" x
echo 'contexts: []' > "$tmp/k3s.yaml"
sha=abc123def456

cat > "$tmp/bin/curl" <<EOF
#!/usr/bin/env bash
[ "\${FAIL:-}" = curl ] && exit 22
cat "$tmp/repo.tar.gz"
EOF
cat > "$tmp/bin/kubectl" <<EOF
#!/usr/bin/env bash
echo "kubectl \$*" >> "$tmp/calls"
case "\$*" in
  "config rename-context default "*) cp "\$KUBECONFIG" "$tmp/kubeconfig.seen"; dirname "\$KUBECONFIG" > "$tmp/dir" ;;
  *" apply -k "*) cp "\${@: -1}/kustomization.yaml" "$tmp/pin.seen"; [ "\${FAIL:-}" != apply ] ;;
  *" get deploy "*) echo "banjjak web ghcr.io/gongbugi/banjjak:$sha" ;;
  *" rollout status "*) [ "\${FAIL:-}" != rollout ] ;;
esac
EOF
printf '#!/usr/bin/env bash\necho "gh $*" >> "%s/calls"\nexit 1\n' "$tmp" > "$tmp/bin/gh"
chmod +x "$tmp/bin/"*

run() { rm -f "$tmp/calls"; FAIL=$1 PATH="$tmp/bin:$PATH" K3S_CONFIG="$tmp/k3s.yaml" sh "$here/on-vm.sh" azure-banjjak "$sha" > "$tmp/log" 2>&1; }
bad=0
no() { echo "FAIL $1"; cat "$tmp/log"; bad=1; }

run ""
if [ $? -eq 0 ] && [ "$(tail -n 1 "$tmp/log")" = "deployed $sha" ]; then echo "ok   성공하면 마지막 줄이 deployed <SHA>"; else no "성공 줄"; fi
grep -q "newTag: \"$sha\"" "$tmp/pin.seen" && grep -q -- "--context azure-banjjak apply -k" "$tmp/calls" && echo "ok   그 커밋으로 고정해 이 클러스터에 적용" || no "고정 적용"
grep -q "^gh " "$tmp/calls" && no "VM에서 gh를 불렀다" || echo "ok   VM에서는 gh를 부르지 않음"
grep -q "change-cause=deploy ${sha:0:12} at " "$tmp/calls" && echo "ok   손 배포와 같은 배포 기록" || no "배포 기록"
[ -s "$tmp/dir" ] && [ ! -e "$(cat "$tmp/dir")" ] && echo "ok   kubeconfig 복사본을 지움" || no "복사본이 남았다"

for step in curl apply rollout; do
  run "$step"
  if [ $? -ne 0 ] && ! grep -q "^deployed" "$tmp/log"; then echo "ok   $step 실패면 deployed 줄 없음"; else no "$step 실패"; fi
done
exit $bad
