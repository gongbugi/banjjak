# banjjak DB 백업 저장소: Azure Blob 컨테이너 하나 + 7일 지나면 자동 삭제 + 쓰기 전용 SAS를 k8s Secret으로.
# create.ps1 다음에 돌린다. 이미 있으면 그대로 두고 Secret만 다시 만든다. (Windows PowerShell 5.1, az login 이후)
# SAS 값은 화면에 찍지 않고 바로 kubectl로 넘긴다.
$ErrorActionPreference = 'Stop'
# az · kubectl은 외부 프로그램이라 $ErrorActionPreference가 실패를 못 잡는다 → 종료 코드로 직접 멈춘다
function Az { & $az @args; if ($LASTEXITCODE) { throw "az failed ($LASTEXITCODE): az $args" } }
function Kube { kubectl @args; if ($LASTEXITCODE) { throw "kubectl failed ($LASTEXITCODE)" } }

$az        = "C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin\az.cmd"
$Rg        = 'rg-banjjak'
$Loc       = 'koreacentral'
$Container = 'pg-backups'
$Context   = 'azure-banjjak'
$Expiry    = '2026-12-31T00:00Z'      # 운영 기간(~11-20)보다 넉넉히

# 저장 계정 이름은 전 세계에서 유일해야 한다 → 구독 id 앞 8자리를 붙인다
$SubId   = (Az account show --query id -o tsv).Replace('-', '')
$Account = ('banjjakbk' + $SubId.Substring(0, 8)).ToLower()

if (-not (Az storage account list -g $Rg --query "[?name=='$Account'].name" -o tsv)) {
    Az storage account create -g $Rg -n $Account -l $Loc --sku Standard_LRS --kind StorageV2 `
        --min-tls-version TLS1_2 --allow-blob-public-access false -o none
}
$Key = Az storage account keys list -g $Rg -n $Account --query "[0].value" -o tsv
Az storage container create --account-name $Account --account-key $Key -n $Container -o none

# 7일이 지난 백업은 Azure가 지운다 (지우는 코드를 짜지 않는다)
$policy = Join-Path $env:TEMP 'banjjak-backup-lifecycle.json'
@"
{"rules":[{"enabled":true,"name":"delete-after-7-days","type":"Lifecycle","definition":{
  "actions":{"baseBlob":{"delete":{"daysAfterModificationGreaterThan":7}}},
  "filters":{"blobTypes":["blockBlob"],"prefixMatch":["$Container/"]}}}]}
"@ | Set-Content -Path $policy -Encoding ascii
Az storage account management-policy create -g $Rg --account-name $Account --policy "@$policy" -o none

# 백업 작업용 SAS: 만들기 · 쓰기만 (읽기 · 목록 · 지우기 없음) → 서버가 털려도 기존 백업을 못 지운다
$Sas = Az storage container generate-sas --account-name $Account --account-key $Key -n $Container `
    --permissions cw --expiry $Expiry --https-only -o tsv
$Url = "https://$Account.blob.core.windows.net/$Container"

Kube --context $Context -n banjjak delete secret backup-blob --ignore-not-found | Out-Null
Kube --context $Context -n banjjak create secret generic backup-blob `
    --from-literal=container_url=$Url --from-literal=sas=$Sas | Out-Null
Remove-Variable Key, Sas

"backup storage: $Url  (lifecycle: delete after 7 days, job SAS: create+write only, expires $Expiry)"
