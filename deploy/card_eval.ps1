# 카드 AI 평가(#19)를 돌린다: 키를 화면에 안 보이게 받아 이 프로세스의 환경 변수로만 넘긴다 (파일 · 명령 기록에 안 남는다). 돈이 든다.
# 레포 루트에서 돌린다 (Windows PowerShell 5.1)
$ErrorActionPreference = 'Stop'
$s = Read-Host -AsSecureString 'Anthropic 카드 AI 전용 키 (sk-ant-...)'
$env:ANTHROPIC_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($s))
try {
    & 'app\.venv\Scripts\python.exe' 'deploy\card_eval.py' @args
} finally {
    Remove-Item Env:ANTHROPIC_API_KEY -ErrorAction SilentlyContinue
}
