Set-Location $PSScriptRoot
$Port = 8010
$Url = "http://127.0.0.1:$Port/"
Write-Host "Civ VI City Studio - CITY SIMULATOR v0.5.2" -ForegroundColor Green
Write-Host "Project folder: $PWD"

# Already running? Just open the browser.
try {
  $health = Invoke-RestMethod -Uri "$($Url)health" -TimeoutSec 2
  if ($health.version -eq "0.5.2") { Write-Host "Server already running - opening $Url"; Start-Process $Url; exit 0 }
} catch { }

if (-not (Test-Path ".venv/Scripts/python.exe")) { py -m venv .venv }

# Install packages only when requirements.txt changed since the last install.
$stamp = ".venv/requirements.installed"
if (-not (Test-Path $stamp) -or (Get-FileHash requirements.txt).Hash -ne (Get-FileHash $stamp).Hash) {
  Write-Host "Installing packages..."
  & .venv/Scripts/python.exe -m pip install -q -r requirements.txt
  if ($LASTEXITCODE -eq 0) { Copy-Item requirements.txt $stamp -Force }
}

# Open the browser about 3 seconds after the server starts.
Start-Job -ScriptBlock { param($u) Start-Sleep -Seconds 3; Start-Process $u } -ArgumentList $Url | Out-Null
Write-Host "Starting server at $Url  (close this window to stop)"
& .venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port $Port
