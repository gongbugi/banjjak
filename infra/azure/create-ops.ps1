# banjjak 운영 VM: 앱 VM과 같은 사설 네트워크에 따로 둔다 (관측 · 알림 · 운영 AI · 장애 주입).
# 앱 VM이 통째로 죽어도 운영 쪽은 살아서 탐지하고 진단한다 (CJONE "AIOps 플레인은 관측 대상과 운명을 같이하지 않는다").
# create.ps1(앱 VM) 다음에 돌린다. Windows PowerShell 5.1, az login 이후.
$ErrorActionPreference = 'Stop'
# az · kubectl은 외부 프로그램이라 $ErrorActionPreference가 실패를 못 잡는다 → 종료 코드로 직접 멈춘다
function Az { & $az @args; if ($LASTEXITCODE) { throw "az failed ($LASTEXITCODE): az $args" } }
function Kube { kubectl @args; if ($LASTEXITCODE) { throw "kubectl failed ($LASTEXITCODE)" } }

$az     = "C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin\az.cmd"
$Rg     = 'rg-banjjak'
$Vm     = 'banjjak-ops'
$Size   = 'Standard_B2als_v2'                       # 2 vCPU · 4GB. 크레딧 뒤 예산에 따라 B1ms(2GB)로 줄일 수 있다
$Image  = 'Canonical:ubuntu-24_04-lts:server:latest'
$Admin  = 'azureuser'
$Key    = Join-Path $HOME '.ssh\banjjak_azure_ed25519'
$Vnet   = 'banjjak-vmVNET'                          # 앱 VM이 만든 사설 네트워크 (10.0.0.0/16)
$Subnet = 'banjjak-vmSubnet'
$MyIp   = (Invoke-RestMethod -Uri 'https://api.ipify.org').Trim()

Az network public-ip create -g $Rg -n "$Vm-ip" --sku Standard --allocation-method Static -o none
$Ip = Az network public-ip show -g $Rg -n "$Vm-ip" --query ipAddress -o tsv

# 밖에서 들어오는 것은 운영자 IP만: SSH · k8s API · Grafana. 웹 공개 포트는 없다.
# 앱 VM ↔ 운영 VM 사이는 Azure 기본 규칙(같은 가상 네트워크 허용)으로 통한다.
Az network nsg create -g $Rg -n "$Vm-nsg" -o none
Az network nsg rule create -g $Rg --nsg-name "$Vm-nsg" -n ssh     --priority 110 --protocol Tcp --destination-port-ranges 22   --source-address-prefixes "$MyIp/32" -o none
Az network nsg rule create -g $Rg --nsg-name "$Vm-nsg" -n k8s-api --priority 120 --protocol Tcp --destination-port-ranges 6443 --source-address-prefixes "$MyIp/32" -o none
Az network nsg rule create -g $Rg --nsg-name "$Vm-nsg" -n grafana --priority 130 --protocol Tcp --destination-port-ranges 3000 --source-address-prefixes "$MyIp/32" -o none

$init = Join-Path $env:TEMP 'banjjak-ops-cloud-init.yaml'
(Get-Content (Join-Path $PSScriptRoot 'cloud-init.yaml') -Raw).Replace('__PUBLIC_IP__', $Ip) | Set-Content -Path $init -Encoding ascii

Az vm create -g $Rg -n $Vm --image $Image --size $Size `
    --admin-username $Admin --ssh-key-values "$Key.pub" `
    --vnet-name $Vnet --subnet $Subnet `
    --public-ip-address "$Vm-ip" --nsg "$Vm-nsg" `
    --os-disk-size-gb 30 --storage-sku StandardSSD_LRS `
    --custom-data $init -o none

$Private = Az vm list-ip-addresses -g $Rg -n $Vm --query "[0].virtualMachine.network.privateIpAddresses[0]" -o tsv
"ops ip: $Ip  private: $Private  (ssh: ssh -i $Key $Admin@$Ip)"
