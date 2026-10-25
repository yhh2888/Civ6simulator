Set-Location $PSScriptRoot
Write-Host "Civ VI City Studio - MAP EDITOR v0.3.0" -ForegroundColor Green
Write-Host "Project folder: $PWD"
if (-not (Test-Path ".venv/Scripts/python.exe")) { py -m venv .venv }
& .venv/Scripts/python.exe -m pip install -r requirements.txt
& .venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8010
