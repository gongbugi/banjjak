# 운영 AI가 쓸 Secret 두 개를 운영 클러스터 ops-ai 네임스페이스에 만든다. 값은 화면에 찍지 않는다 (길이만).
#   app-cluster  : 앱 클러스터의 읽기 전용 토큰 (banjjak/ops-ai-token) + CA + API 주소
#   slack-webhook: observe 네임스페이스의 Slack 주소를 그대로 복사 (진단 보고용)
# 앱 클러스터에 deploy/k8s를 먼저 올려야 토큰이 생긴다. 다시 돌리면 지우고 새로 만든다. (Windows PowerShell 5.1)
# 로컬 k3d: .\opsai-secrets.ps1 -App k3d-banjjak -Ops k3d-banjjak -Url https://kubernetes.default.svc
param(
    [string]$App = 'azure-banjjak',
    [string]$Ops = 'azure-banjjak-ops',
    [string]$Url = 'https://10.0.0.4:6443'   # 앱 VM 사설 IP. 운영 VM에서만 닿는다
)
$ErrorActionPreference = 'Stop'
# kubectl은 외부 프로그램이라 $ErrorActionPreference가 실패를 못 잡는다 → 종료 코드로 직접 멈춘다
function Kube { kubectl @args; if ($LASTEXITCODE) { throw "kubectl failed ($LASTEXITCODE)" } }

# base64 그대로 옮긴다 (풀지 않는다). apply는 값을 last-applied 주석에 한 벌 더 남기므로 지우고 create 한다
function Put($name, $data) {
    $fields = ($data.Keys | ForEach-Object { '"' + $_ + '":"' + $data[$_] + '"' }) -join ','
    $json = '{"apiVersion":"v1","kind":"Secret","type":"Opaque","metadata":{"name":"' + $name + '"},"data":{' + $fields + '}}'
    Kube --context $Ops -n ops-ai delete secret $name --ignore-not-found | Out-Null
    $json | kubectl --context $Ops -n ops-ai create -f - | Out-Null
    if ($LASTEXITCODE) { throw "kubectl create $name failed ($LASTEXITCODE)" }
}

$token = Kube --context $App -n banjjak get secret ops-ai-token -o 'jsonpath={.data.token}'
$ca = Kube --context $App -n banjjak get secret ops-ai-token -o 'jsonpath={.data.ca\.crt}'
if (-not $token -or -not $ca) { throw "ops-ai-token이 아직 비어 있다 ($App). deploy/k8s를 올리고 몇 초 뒤 다시" }
$url64 = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes($Url))
Put 'app-cluster' @{ url = $url64; token = $token; 'ca.crt' = $ca }
"app-cluster: $Ops/ops-ai <- $App 토큰 (base64 $($token.Length)자) · ca.crt · $Url"

$slack = Kube --context $Ops -n observe get secret slack-webhook --ignore-not-found -o 'jsonpath={.data.url}'
if ($slack) {
    Put 'slack-webhook' @{ url = $slack }
    "slack-webhook: $Ops/observe -> ops-ai (base64 $($slack.Length)자)"
} else { "slack-webhook: $Ops/observe에 없음 → 건너뜀 (진단은 기록만)" }
Remove-Variable token, ca, slack

# Slack 주소는 환경 변수라 파드를 새로 띄워야 들어간다 (app-cluster 파일은 저절로 바뀐다)
Kube --context $Ops -n ops-ai rollout restart deploy/ops-ai | Out-Null
Kube --context $Ops -n ops-ai rollout status deploy/ops-ai --timeout=120s | Out-Null
"ops-ai: restarted"
