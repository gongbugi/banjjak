# Better Stack 하트비트 주소를 k8s Secret "heartbeats"로 넣는다. 주소는 화면에 찍지 않는다 (Read-Host -AsSecureString).
# 빈칸으로 두고 엔터를 치면 그 하트비트는 건너뛴다. 다시 돌리면 Secret을 지우고 새로 만든다. (Windows PowerShell 5.1)
$ErrorActionPreference = 'Stop'
# kubectl은 외부 프로그램이라 $ErrorActionPreference가 실패를 못 잡는다 → 종료 코드로 직접 멈춘다
function Kube { kubectl @args; if ($LASTEXITCODE) { throw "kubectl failed ($LASTEXITCODE)" } }

function Ask($label) {
    $s = Read-Host -AsSecureString "$label heartbeat URL (없으면 엔터)"
    $v = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($s))
    if ($v -and -not $v.StartsWith('https://')) { throw "$label : https:// 로 시작하는 주소가 아니다 (값은 찍지 않음)" }
    $v
}

function Put($ctx, $ns, $values) {
    $lits = @()
    foreach ($k in $values.Keys) { if ($values[$k]) { $lits += "--from-literal=$k=$($values[$k])" } }
    if (-not $lits) { "skip $ctx/$ns (주소 없음)"; return }
    Kube --context $ctx -n $ns delete secret heartbeats --ignore-not-found | Out-Null
    Kube --context $ctx -n $ns create secret generic heartbeats @lits | Out-Null
    "heartbeats: $ctx/$ns <- " + (($values.Keys | Where-Object { $values[$_] }) -join ', ')
}

$app     = [ordered]@{ collector = (Ask 'collector'); analyzer = (Ask 'analyzer'); backup = (Ask 'backup') }
$opsai   = [ordered]@{ ops_ai = (Ask 'ops-ai') }
$observe = [ordered]@{ watchdog = (Ask 'ops VM watchdog') }
Put 'azure-banjjak'     'banjjak' $app
Put 'azure-banjjak-ops' 'ops-ai'  $opsai
Put 'azure-banjjak-ops' 'observe' $observe
Remove-Variable app, opsai, observe
