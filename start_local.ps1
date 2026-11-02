$ErrorActionPreference = 'Stop'
$projectDirectory = $PSScriptRoot
Set-Location -LiteralPath $projectDirectory
$profilePath = Join-Path $projectDirectory 'data/local-accounts.json'
if (-not (Test-Path -LiteralPath $profilePath)) {
    throw 'Local account configuration not found: data/local-accounts.json'
}
$accountProfile = Get-Content -LiteralPath $profilePath -Raw -Encoding UTF8 | ConvertFrom-Json
$env:ENABLE_USER_ACCOUNTS = 'true'
$env:REGISTRATION_MODE = 'invite'
$env:REGISTRATION_INVITE_CODE = $accountProfile.invite_code
# Keep this launcher on the local database even if a parent terminal has cloud variables.
$localDatabase = (Join-Path $projectDirectory 'data/chat.db').Replace('\', '/')
$env:DATABASE_URL = 'sqlite:///' + $localDatabase
& 'D:\Work\AI_Python\AI\.venv\Scripts\python.exe' -m uvicorn server:app --host 127.0.0.1 --port 8788
