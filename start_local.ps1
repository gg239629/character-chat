$ErrorActionPreference = 'Stop'
$projectDirectory = $PSScriptRoot
Set-Location -LiteralPath $projectDirectory
# Isolate dependencies from other AI projects that may require MCP 2.x.
$projectPython = Join-Path $projectDirectory '.venv/Scripts/python.exe'
$lockFile = Join-Path $projectDirectory 'requirements.lock'
$lockStamp = Join-Path $projectDirectory '.venv/requirements.sha256'
$lockHasher = [System.Security.Cryptography.SHA256]::Create()
try { $lockHash = [System.BitConverter]::ToString($lockHasher.ComputeHash([System.IO.File]::ReadAllBytes($lockFile))).Replace('-', '') } finally { $lockHasher.Dispose() }
if (-not (Test-Path -LiteralPath $projectPython) -or -not (Test-Path -LiteralPath $lockStamp) -or (Get-Content -LiteralPath $lockStamp -Raw).Trim() -ne $lockHash) {
    $uvCommand = Get-Command uv -ErrorAction SilentlyContinue
    if (-not $uvCommand) { throw 'uv is required to prepare the project environment. Install uv and run this launcher again.' }
    if (-not (Test-Path -LiteralPath $projectPython)) {
        & $uvCommand.Source venv (Join-Path $projectDirectory '.venv') --python 3.14
        if ($LASTEXITCODE -ne 0) { throw 'Project Python environment creation failed' }
    }
    & $uvCommand.Source pip install --python $projectPython -r $lockFile
    if ($LASTEXITCODE -ne 0) { throw 'Project dependency installation failed' }
    Set-Content -LiteralPath $lockStamp -Value $lockHash -Encoding ASCII
}
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
$env:OPENBLAS_NUM_THREADS = '1'
$env:OMP_NUM_THREADS = '1'
$frontendEntry = Join-Path $projectDirectory 'static/web/index.html'
$sourceFiles = Get-ChildItem -LiteralPath (Join-Path $projectDirectory 'frontend/src') -Recurse -File
$latestSource = ($sourceFiles | Measure-Object -Property LastWriteTime -Maximum).Maximum
if (-not (Test-Path -LiteralPath $frontendEntry) -or $latestSource -gt (Get-Item -LiteralPath $frontendEntry).LastWriteTime) {
    Push-Location -LiteralPath (Join-Path $projectDirectory 'frontend')
    try {
        & npm.cmd ci
        if ($LASTEXITCODE -ne 0) { throw 'Frontend dependency installation failed' }
        & npm.cmd run build
        if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed' }
    } finally { Pop-Location }
}
& $projectPython -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8788
