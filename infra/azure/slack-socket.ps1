# Slack 승인 버튼(#8 2단계)용 Secret "slack-socket"을 운영 VM ops-ai 네임스페이스에 만든다. 값은 화면에 찍지 않는다 (길이만).
#   app_token: Socket Mode 앱 토큰 (xapp-, 범위 connections:write) · approver: 버튼을 누를 수 있는 사람의 Slack 멤버 ID (U...)
# Socket Mode는 연결이 여러 개면 클릭이 그중 하나에만 간다 → 이 Secret은 운영 VM 한 곳에만 넣는다 (로컬 k3d에는 넣지 않는다)
# 다시 돌리면 지우고 새로 만든다. (Windows PowerShell 5.1)
param([string]$Context = 'azure-banjjak-ops')
$ErrorActionPreference = 'Stop'
# kubectl은 외부 프로그램이라 $ErrorActionPreference가 실패를 못 잡는다 → 종료 코드로 직접 멈춘다
function Kube { kubectl @args; if ($LASTEXITCODE) { throw "kubectl failed ($LASTEXITCODE)" } }

$s = Read-Host -AsSecureString 'Slack app-level token (xapp-...)'
$token = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($s))
if (-not $token.StartsWith('xapp-')) { throw 'xapp- 로 시작하는 앱 토큰이 아니다 (값은 찍지 않음)' }
$approver = (Read-Host 'Slack 멤버 ID (U...)').Trim()
if ($approver -notmatch '^[UW][A-Z0-9]{6,}$') { throw '멤버 ID 모양이 아니다 (U 또는 W로 시작하는 대문자 · 숫자)' }

Kube --context $Context -n ops-ai delete secret slack-socket --ignore-not-found | Out-Null
Kube --context $Context -n ops-ai create secret generic slack-socket "--from-literal=app_token=$token" "--from-literal=approver=$approver" | Out-Null
"slack-socket: $Context/ops-ai <- app_token ($($token.Length)자) · approver ($($approver.Length)자)"
Remove-Variable token, s

# 환경 변수로 읽으므로 파드를 새로 띄워야 들어간다
Kube --context $Context -n ops-ai rollout restart deploy/ops-ai | Out-Null
Kube --context $Context -n ops-ai rollout status deploy/ops-ai --timeout=180s | Out-Null
"ops-ai: restarted"
