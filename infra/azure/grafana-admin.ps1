# Grafana 관리자 비밀번호: 무작위로 만들어 운영 클러스터 Secret에 바로 넣는다 (화면에 찍지 않는다). 이미 있으면 그대로 둔다.
# 로그인할 때(사용자 admin) 비밀번호를 클립보드로 꺼내기:
#   kubectl --context azure-banjjak-ops -n observe get secret grafana-admin -o jsonpath='{.data.password}' | % { [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($_)) } | Set-Clipboard
param([string]$Context = 'azure-banjjak-ops')
$ErrorActionPreference = 'Stop'
# kubectl은 외부 프로그램이라 $ErrorActionPreference가 실패를 못 잡는다 → 종료 코드로 직접 멈춘다
function Kube { kubectl @args; if ($LASTEXITCODE) { throw "kubectl failed ($LASTEXITCODE)" } }

if (-not (Kube --context $Context get namespace observe --ignore-not-found -o name)) {
    Kube --context $Context create namespace observe | Out-Null
}
if (Kube --context $Context -n observe get secret grafana-admin --ignore-not-found -o name) {
    "grafana-admin: already exists (kept)"; return
}
$bytes = New-Object byte[] 24
[Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
$pw = [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
Kube --context $Context -n observe create secret generic grafana-admin --from-literal=password=$pw | Out-Null
Remove-Variable pw, bytes
"grafana-admin: created (user admin, $Context)"
