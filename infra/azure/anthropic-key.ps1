# Anthropic API 키를 Secret "anthropic"에 넣는다. 값은 화면에 찍지 않는다 (길이만). 호출처마다 키를 따로 만든다.
#   운영 AI 진단(#20): 인자 없이 (운영 VM · ops-ai)    카드 AI(#19): azure-banjjak banjjak (앱 VM, 분석기 CronJob이 다음 실행부터 읽는다)
#   api_key: Console에서 만든 API 키 (sk-ant-...). 키 종류(개인 · 서비스 계정 · 옛 워크스페이스 키)마다 접두어가 달라 종류는 묻지 않는다.
#            관리자 키(sk-ant-admin...)와 워크스페이스 하나에 묶이지 않은 키는 받지 않는다 (둘 다 관리 API까지 된다)
# 넣기 전에 Anthropic에 한 번 묻는다 (모델 목록, 요금 없음): 키가 살아 있나 · 워크스페이스 하나에 묶였나. 돈의 마지막 상한은 선불 $20 · 자동 충전 끔
# 파드에는 #20 배포가 환경 변수로 잇는다 (그 배포가 파드를 새로 띄운다). 다시 돌리면 지우고 새로 만든다. (Windows PowerShell 5.1)
param([string]$Context = 'azure-banjjak-ops', [string]$Namespace = 'ops-ai')
$ErrorActionPreference = 'Stop'
# kubectl은 외부 프로그램이라 $ErrorActionPreference가 실패를 못 잡는다 → 종료 코드로 직접 멈춘다
function Kube { kubectl @args; if ($LASTEXITCODE) { throw "kubectl failed ($LASTEXITCODE)" } }

$s = Read-Host -AsSecureString 'Anthropic API key (sk-ant-...)'
$key = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($s))
if (-not $key.StartsWith('sk-ant-') -or $key.StartsWith('sk-ant-admin')) {
    throw 'sk-ant- 로 시작하는 API 키여야 하고, 관리자 키(sk-ant-admin)는 안 된다 (값은 찍지 않음)'
}

# 워크스페이스에 묶인 키는 응답 머리글에 그 워크스페이스가 온다. 묶이지 않은 키는 400이거나 머리글이 없다 (공식 문서 Authentication · API overview)
$unscoped = '워크스페이스 하나에 묶이지 않은 키다 (관리 API까지 된다). Console에서 워크스페이스를 하나 골라 다시 만든다 (값은 찍지 않음)'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
try {
    $r = Invoke-WebRequest 'https://api.anthropic.com/v1/models' -UseBasicParsing -Headers @{ 'x-api-key' = $key; 'anthropic-version' = '2023-06-01' }
} catch {
    if ($_.Exception.Response -and [int]$_.Exception.Response.StatusCode -eq 400) { throw $unscoped }
    throw "키 확인 실패: $($_.Exception.Message) (값은 찍지 않음)"
}
$workspace = $r.Headers['anthropic-workspace-id']
if (-not $workspace) { throw $unscoped }
"키 확인: 워크스페이스 $workspace · 모델 $((ConvertFrom-Json $r.Content).data.Count)개"

# 값을 명령줄 인자에 싣지 않고 표준 입력으로 넘긴다. apply는 값을 last-applied 주석에 한 벌 더 남기므로 지우고 create 한다
$b64 = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes($key))
Kube --context $Context -n $Namespace delete secret anthropic --ignore-not-found | Out-Null
'{"apiVersion":"v1","kind":"Secret","type":"Opaque","metadata":{"name":"anthropic"},"data":{"api_key":"' + $b64 + '"}}' |
    kubectl --context $Context -n $Namespace create -f - | Out-Null
if ($LASTEXITCODE) { throw "kubectl create anthropic failed ($LASTEXITCODE)" }
"anthropic: $Context/$Namespace <- api_key ($($key.Length)자)"
Remove-Variable key, b64, s
