# 운영 AI 조치 게이트(#20)가 쓸 jev(TypeSafe) API 키를 운영 VM ops-ai 네임스페이스 Secret "typesafe"에 넣는다. 값은 화면에 찍지 않는다 (길이만).
#   api_key: console.typesafe.ai/keys 에서 만든 키. 접두어는 문서에 없어 검사하지 않는다
# 넣기 전에 모델 목록을 한 번 물어 키가 살아 있나 본다 (입력 토큰이 없는 호출)
# 파드에는 #20 배포가 환경 변수로 잇는다 (그 배포가 파드를 새로 띄운다). 다시 돌리면 지우고 새로 만든다. (Windows PowerShell 5.1)
param([string]$Context = 'azure-banjjak-ops')
$ErrorActionPreference = 'Stop'
# kubectl은 외부 프로그램이라 $ErrorActionPreference가 실패를 못 잡는다 → 종료 코드로 직접 멈춘다
function Kube { kubectl @args; if ($LASTEXITCODE) { throw "kubectl failed ($LASTEXITCODE)" } }

$s = Read-Host -AsSecureString 'jev (TypeSafe) API key'
$key = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($s))
if (-not $key) { throw '키가 비어 있다' }

[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
try {
    $r = Invoke-WebRequest 'https://api.typesafe.ai/v1/models' -UseBasicParsing -Headers @{ Authorization = "Bearer $key" }
} catch {
    throw "키 확인 실패: $($_.Exception.Message) (값은 찍지 않음)"
}
"키 확인: 모델 $(((ConvertFrom-Json $r.Content).models.name) -join ' · ')"

# 값을 명령줄 인자에 싣지 않고 표준 입력으로 넘긴다. apply는 값을 last-applied 주석에 한 벌 더 남기므로 지우고 create 한다
$b64 = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes($key))
Kube --context $Context -n ops-ai delete secret typesafe --ignore-not-found | Out-Null
'{"apiVersion":"v1","kind":"Secret","type":"Opaque","metadata":{"name":"typesafe"},"data":{"api_key":"' + $b64 + '"}}' |
    kubectl --context $Context -n ops-ai create -f - | Out-Null
if ($LASTEXITCODE) { throw "kubectl create typesafe failed ($LASTEXITCODE)" }
"typesafe: $Context/ops-ai <- api_key ($($key.Length)자)"
Remove-Variable key, b64, s
