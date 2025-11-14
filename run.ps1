#!/usr/bin/env pwsh
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

Write-Host "[RUN] PowerShell $($PSVersionTable.PSVersion) on $([Environment]::OSVersion.VersionString)"

# --- Paths ---
$ProjDir = (Split-Path -Parent $MyInvocation.MyCommand.Path) | Resolve-Path
Set-Location $ProjDir
$Venv   = Join-Path $ProjDir ".venv_umd2"
$Req    = Join-Path $ProjDir "requirements.txt"

# --- Create .\run.cmd on first run (so you can just do: .\run) ---
$RunCmd = Join-Path $ProjDir "run.cmd"
if (-not (Test-Path $RunCmd)) {
@"
@echo off
setlocal
set "PS=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
"%PS%" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" %*
exit /b %ERRORLEVEL%
"@ | Out-File -FilePath $RunCmd -Encoding ASCII -Force
}

# --- Flags / Mode ---
$FORCE = $false
$MODE  = "gui"  # default
function Shift-Args { param([ref]$A) if ($A.Value.Count -gt 0) { $A.Value = $A.Value[1..($A.Value.Count-1)] } else { $A.Value = @() } }
if     ($Args.Count -gt 0 -and $Args[0] -eq "--force-install") { $FORCE = $true;  Shift-Args ([ref]$Args) }
if     ($Args.Count -gt 0 -and $Args[0] -eq "--backend")       { $MODE  = "backend"; Shift-Args ([ref]$Args) }
elseif ($Args.Count -gt 0 -and $Args[0] -eq "--gui")           { $MODE  = "gui";     Shift-Args ([ref]$Args) }

# --- Locate system Python ---
$Py = $null
try { $Py = (Get-Command py -ErrorAction Stop).Source } catch { }
if (-not $Py) { try { $Py = (Get-Command python -ErrorAction Stop).Source } catch { } }
if (-not $Py) { throw "[RUN] Python not found. Install Python 3 or add 'py'/'python' to PATH." }

# --- venv (no activation needed) ---
if (-not (Test-Path $Venv)) {
  Write-Host "[RUN] Creating venv at $Venv"
  try { & $Py -3 -m venv "$Venv" } catch { & $Py -m venv "$Venv" }
}
$Vpy  = Join-Path $Venv "Scripts\python.exe"
$Vpip = Join-Path $Venv "Scripts\pip.exe"
if (-not (Test-Path $Vpy))  { throw "[RUN] Python in venv not found at $Vpy" }
if (-not (Test-Path $Vpip)) { & $Vpy -m ensurepip --upgrade | Out-Null }

# --- deps ---
$ReqHashFile = Join-Path $Venv ".req_hash"
function Get-ReqHash([string]$p) { if (Test-Path $p) { (Get-FileHash -Algorithm SHA256 -Path $p).Hash } else { "" } }
$CurHash = Get-ReqHash $Req
$OldHash = if (Test-Path $ReqHashFile) { (Get-Content $ReqHashFile -ErrorAction SilentlyContinue | Select-Object -First 1) } else { "" }

if (-not (Test-Path $Req)) {
  Write-Warning "[RUN] WARNING: requirements.txt not found; skipping installs."
}
elseif ($FORCE -or ($CurHash -ne $OldHash)) {
  Write-Host "[RUN] Installing/Updating deps from requirements.txt"
  & $Vpy -m pip install -U pip | Out-Null
  & $Vpip install -r "$Req"
  Set-Content -Path $ReqHashFile -Value $CurHash
}
else {
  Write-Host "[RUN] Deps up-to-date (requirements.txt unchanged) — skipping install"
}

# --- Launch ---
if ($MODE -eq "backend") {
  Write-Host "[RUN] Backend mode → python umd2.py $Args"
  & $Vpy (Join-Path $ProjDir "umd2.py") @Args
} else {
  Write-Host "[RUN] GUI mode (default) → python gui.py"
  Write-Host "[RUN] Tips:"
  Write-Host "       • For backend: .\run --backend --serial COM3 --baud 921600 --out jsonl"
  Write-Host "       • Force reinstall deps: .\run --force-install"
  & $Vpy (Join-Path $ProjDir "gui.py") @Args
}
exit $LASTEXITCODE

