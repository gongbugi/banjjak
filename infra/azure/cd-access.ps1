# CD가 쓸 Azure 권한과 GitHub 환경을 만든다. 다시 돌려도 된다. (Windows PowerShell 5.1, az login · gh auth login 이후)
# 오래 쓰는 키가 없다: Azure는 GitHub이 실행마다 주는 토큰을, 이 저장소의 이 환경에서 왔을 때만 믿는다.
# 허용은 rg-banjjak의 VM에 Run Command를 보내는 것뿐이다. 다만 Run Command는 VM 안에서 root로 돌므로 환경의 승인이 마지막 문이다.
$ErrorActionPreference = 'Stop'
# az · gh는 외부 프로그램이라 $ErrorActionPreference가 실패를 못 잡는다 → 종료 코드로 직접 멈춘다
function Az { & $az @args; if ($LASTEXITCODE) { throw "az failed ($LASTEXITCODE): az $args" } }
# PowerShell 함수 이름은 대소문자를 안 가린다 → gh만 쓰면 이 함수 자신을 부르므로 gh.exe로 쓴다
function Gh { gh.exe @args; if ($LASTEXITCODE) { throw "gh failed ($LASTEXITCODE): gh $args" } }

$az      = "C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin\az.cmd"
$Rg      = 'rg-banjjak'
$Repo    = 'gongbugi/banjjak'
$Name    = 'banjjak-cd'
$Role    = 'banjjak-cd-run-command'
$Targets = 'azure-banjjak', 'azure-banjjak-ops'   # 환경 이름. deploy.yml의 target과 같다

$Sub    = Az account show --query id -o tsv
$Tenant = Az account show --query tenantId -o tsv
$Scope  = "/subscriptions/$Sub/resourceGroups/$Rg"
# 이 저장소의 토큰 subject는 이름 대신 바뀌지 않는 ID를 쓴다 (repo:소유자@ID/레포@ID:...). 형식은 GitHub에 묻는다
$Prefix = Gh api "repos/$Repo/actions/oidc/customization/sub" --jq .sub_claim_prefix

Az identity create -g $Rg -n $Name -o none
$Client = Az identity show -g $Rg -n $Name --query clientId -o tsv
$Object = Az identity show -g $Rg -n $Name --query principalId -o tsv
foreach ($t in $Targets) {
    Az identity federated-credential create -g $Rg --identity-name $Name -n $t `
        --issuer 'https://token.actions.githubusercontent.com' `
        --subject "${Prefix}:environment:$t" --audiences 'api://AzureADTokenExchange' -o none
}

# 기본 제공 역할(Virtual Machine Contributor)은 VM 삭제까지 되므로 Run Command만 담은 역할을 따로 만든다
$def = Join-Path $env:TEMP 'banjjak-cd-role.json'
@{
    Name             = $Role
    Description      = 'banjjak CD: Run Command on the VMs in this resource group'
    Actions          = @('Microsoft.Compute/virtualMachines/read',
                         'Microsoft.Compute/virtualMachines/runCommand/action',
                         'Microsoft.Compute/virtualMachines/runCommands/write')
    AssignableScopes = @($Scope)
} | ConvertTo-Json | Set-Content -Path $def -Encoding ascii
$verb = if (Az role definition list --name $Role --custom-role-only true --scope $Scope --query '[0].roleName' -o tsv) { 'update' } else { 'create' }
Az role definition $verb --role-definition "@$def" -o none

# 새 역할이 퍼지는 데 시간이 걸린다 → 30초씩 기다려 최대 5분
foreach ($i in 1..10) {
    & $az role assignment create --assignee-object-id $Object --assignee-principal-type ServicePrincipal --role $Role --scope $Scope -o none
    if (-not $LASTEXITCODE) { break }
    if ($i -eq 10) { throw "role assignment failed ($LASTEXITCODE)" }
    "역할이 아직 안 퍼졌다, 30초 뒤 다시 ($i/10)"
    Start-Sleep -Seconds 30
}

# 본인이 승인해야 돌고, main에서만 돈다. 혼자 쓰는 레포라 자기 승인은 막지 않는다
$Me = Gh api user --jq .id
foreach ($t in $Targets) {
    '{"reviewers":[{"type":"User","id":' + $Me + '}],"deployment_branch_policy":{"protected_branches":false,"custom_branch_policies":true}}' |
        gh.exe api -X PUT "repos/$Repo/environments/$t" --input - | Out-Null
    if ($LASTEXITCODE) { throw "gh environment failed ($LASTEXITCODE): $t" }
    if ('main' -notin @(Gh api "repos/$Repo/environments/$t/deployment-branch-policies" --jq '.branch_policies[].name')) {
        Gh api -X POST "repos/$Repo/environments/$t/deployment-branch-policies" -f name=main | Out-Null
    }
}

# 비밀번호가 아니라 식별자다. 공개 로그에 안 찍히게 secret에 둔다
Gh secret set AZURE_CLIENT_ID -R $Repo --body $Client
Gh secret set AZURE_TENANT_ID -R $Repo --body $Tenant
Gh secret set AZURE_SUBSCRIPTION_ID -R $Repo --body $Sub
"cd: $Name · 역할 $Role · 환경 $($Targets -join ', ') (승인 필요, main만)"
