# 재생(#20)을 돌린다: 키를 화면에 안 보이게 받아 이 프로세스의 환경 변수로만 넘긴다 (파일 · 명령 기록에 안 남는다). 돈이 든다.
# 인자는 deploy/replay.py로 그대로 간다. 레포 루트에서 돌린다 (Windows PowerShell 5.1)
$ErrorActionPreference = 'Stop'
function Ask($label) {
    $s = Read-Host -AsSecureString $label
    [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($s))
}
$env:ANTHROPIC_API_KEY = Ask 'Anthropic 재생 전용 키 (sk-ant-...)'
$env:TYPESAFE_API_KEY = Ask 'jev 키 (게이트를 안 볼 거면 그냥 Enter)'
try {
    & 'app\.venv\Scripts\python.exe' 'deploy\replay.py' @args
} finally {
    Remove-Item Env:ANTHROPIC_API_KEY, Env:TYPESAFE_API_KEY -ErrorAction SilentlyContinue
}
