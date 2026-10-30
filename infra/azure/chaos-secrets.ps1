# 장애 주입기(#10)가 쓸 Secret을 운영 클러스터 chaos 네임스페이스에 만든다. 값은 화면에 찍지 않는다 (길이만).
#   app-cluster: 앱 클러스터의 주입기 토큰 (banjjak/chaos-token) + CA + API 주소
#   heartbeats : (-Heartbeat를 주면) Better Stack 주입기 하트비트 주소. 입력은 Read-Host -AsSecureString
#   load-token : (-Load를 주면) 급증(#11)용 fine-grained 토큰. 저장소 gongbugi/banjjak-load 하나 · Actions 읽기/쓰기만 · 만료 12-01
# 앱 클러스터에 deploy/k8s를 먼저 올려야 토큰이 생긴다. 다시 돌리면 지우고 새로 만든다. (Windows PowerShell 5.1)
# 로컬 k3d: .\chaos-secrets.ps1 -App k3d-banjjak -Ops k3d-banjjak -Url https://kubernetes.default.svc
param(
    [string]$App = 'azure-banjjak',
    [string]$Ops = 'azure-banjjak-ops',
    [string]$Url = 'https://10.0.0.4:6443',   # 앱 VM 사설 IP. 운영 VM에서만 닿는다
    [switch]$Heartbeat,
    [switch]$Load
)
$ErrorActionPreference = 'Stop'
# kubectl은 외부 프로그램이라 $ErrorActionPreference가 실패를 못 잡는다 → 종료 코드로 직접 멈춘다
function Kube { kubectl @args; if ($LASTEXITCODE) { throw "kubectl failed ($LASTEXITCODE)" } }

# base64 그대로 옮긴다 (풀지 않는다). apply는 값을 last-applied 주석에 한 벌 더 남기므로 지우고 create 한다
function Put($name, $data) {
    $fields = ($data.Keys | ForEach-Object { '"' + $_ + '":"' + $data[$_] + '"' }) -join ','
    $json = '{"apiVersion":"v1","kind":"Secret","type":"Opaque","metadata":{"name":"' + $name + '"},"data":{' + $fields + '}}'
    Kube --context $Ops -n chaos delete secret $name --ignore-not-found | Out-Null
    $json | kubectl --context $Ops -n chaos create -f - | Out-Null
    if ($LASTEXITCODE) { throw "kubectl create $name failed ($LASTEXITCODE)" }
}

$token = Kube --context $App -n banjjak get secret chaos-token -o 'jsonpath={.data.token}'
$ca = Kube --context $App -n banjjak get secret chaos-token -o 'jsonpath={.data.ca\.crt}'
if (-not $token -or -not $ca) { throw "chaos-token이 아직 비어 있다 ($App). deploy/k8s를 올리고 몇 초 뒤 다시" }
$url64 = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes($Url))
Put 'app-cluster' @{ url = $url64; token = $token; 'ca.crt' = $ca }
"app-cluster: $Ops/chaos <- $App 주입기 토큰 (base64 $($token.Length)자) · ca.crt · $Url"
Remove-Variable token, ca

if ($Heartbeat) {
    $s = Read-Host -AsSecureString 'chaos(주입기) heartbeat URL'
    $v = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($s))
    if (-not $v.StartsWith('https://')) { throw 'https:// 로 시작하는 주소가 아니다 (값은 찍지 않음)' }
    Put 'heartbeats' @{ chaos = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes($v)) }
    "heartbeats: $Ops/chaos <- chaos ($($v.Length)자)"
    Remove-Variable v, s
}

if ($Load) {
    $s = Read-Host -AsSecureString 'banjjak-load fine-grained 토큰'
    $v = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($s))
    if (-not $v.StartsWith('github_pat_')) { throw 'github_pat_ 로 시작하는 fine-grained 토큰이 아니다 (값은 찍지 않음)' }
    Put 'load-token' @{ token = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes($v)) }
    "load-token: $Ops/chaos <- token ($($v.Length)자)"
    Remove-Variable v, s
}
# CronJob은 10분마다 새 파드라 재시작할 것이 없다
