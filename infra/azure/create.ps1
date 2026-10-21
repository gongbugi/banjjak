# banjjak 서버 한 대를 만든다: Azure Korea Central, 4GB VM, k3s, 방화벽.
# 다시 만들 때도 이 스크립트를 그대로 돌린다 (Windows PowerShell 5.1, az login 이후).
# ponytail: Terraform 대신 az CLI 스크립트. 재현은 되지만 상태 추적(drift)은 없다 → 나중에 Terraform으로 옮긴다.
$ErrorActionPreference = 'Stop'

$az     = "C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin\az.cmd"
$Rg     = 'rg-banjjak'
$Loc    = 'koreacentral'
$Vm     = 'banjjak-vm'
$Size   = 'Standard_B2als_v2'                       # 2 vCPU · 4GB
$Image  = 'Canonical:ubuntu-24_04-lts:server:latest'
$Admin  = 'azureuser'
$Key    = Join-Path $HOME '.ssh\banjjak_azure_ed25519'
$MyIp   = (Invoke-RestMethod -Uri 'https://api.ipify.org').Trim()   # SSH · k8s API는 이 IP만

if (-not (Test-Path $Key)) { ssh-keygen -q -t ed25519 -f $Key -N '""' -C 'banjjak-azure' }

& $az group create -n $Rg -l $Loc -o none
& $az network public-ip create -g $Rg -n "$Vm-ip" --sku Standard --allocation-method Static -o none
$Ip = & $az network public-ip show -g $Rg -n "$Vm-ip" --query ipAddress -o tsv

& $az network nsg create -g $Rg -n "$Vm-nsg" -o none
& $az network nsg rule create -g $Rg --nsg-name "$Vm-nsg" -n http    --priority 100 --protocol Tcp --destination-port-ranges 80   --source-address-prefixes Internet   -o none
& $az network nsg rule create -g $Rg --nsg-name "$Vm-nsg" -n ssh     --priority 110 --protocol Tcp --destination-port-ranges 22   --source-address-prefixes "$MyIp/32" -o none
& $az network nsg rule create -g $Rg --nsg-name "$Vm-nsg" -n k8s-api --priority 120 --protocol Tcp --destination-port-ranges 6443 --source-address-prefixes "$MyIp/32" -o none

$init = Join-Path $env:TEMP 'banjjak-cloud-init.yaml'
(Get-Content (Join-Path $PSScriptRoot 'cloud-init.yaml') -Raw).Replace('__PUBLIC_IP__', $Ip) | Set-Content -Path $init -Encoding ascii

& $az vm create -g $Rg -n $Vm --image $Image --size $Size `
    --admin-username $Admin --ssh-key-values "$Key.pub" `
    --public-ip-address "$Vm-ip" --nsg "$Vm-nsg" `
    --os-disk-size-gb 30 --storage-sku StandardSSD_LRS `
    --custom-data $init -o none

"server ip: $Ip  (ssh: ssh -i $Key $Admin@$Ip)"
