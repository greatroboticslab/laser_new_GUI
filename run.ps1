#!/usr/bin/env pwsh
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# --- Paths ---
$ProjDir = (Split-Path -Parent $MyInvocation.MyCommand.Path) | Resolve-Path
Set-Location $ProjDir
$Venv   = Join-Path $ProjDir ".venv_umd2"
$Req    = Join-Path $ProjDir "requirements.txt"

# --- Make .\run (run.cmd) and re-exec through it on first run for immediate use ---
$RunCmd = Join-Path $ProjDir "run.cmd"
if (-not (Test-Path $RunCmd)) {
  @"
@echo off
setlocal
powershell -ExecutionPolicy Bypass -File "%~dp0run.ps1" %*
"@ | Out-File -FilePath $RunCmd -Encoding ASCII -Force
  # Re-exec through run.cmd once so user can use .\run from now on
  if ([IO.Path]::GetFileName($MyInvocation.MyCommand.Path) -eq "run.ps1" -and -not $env:RUN_REEXECED) {
    $env:RUN_REEXECED = "1"
    & $RunCmd @Args
    exit $LASTEXITCODE
  }
}

# --- Flags / Mode ---
$FORCE = $false
$MODE  = "gui"  # default

function Shift-Args {
  param([ref]$A)
  if ($A.Value.Count -gt 0) { $A.Value = $A.Value[1..($A.Value.Count-1)] }
  else { $A.Value = @() }
}

if ($Args.Count -gt 0 -and $Args[0] -eq "--force-install") { $FORCE = $true;  Shift-Args ([ref]$Args) }
if     ($Args.Count -gt 0 -and $Args[0] -eq "--backend")     { $MODE  = "backend"; Shift-Args ([ref]$Args) }
elseif ($Args.Count -gt 0 -and $Args[0] -eq "--gui")         { $MODE  = "gui";     Shift-Args ([ref]$Args) }

# --- venv (no activation needed) ---
if (-not (Test-Path $Venv)) {
  Write-Host "[RUN] Creating venv at $Venv"
  python -m venv "$Venv"
}

$Vpy = Join-Path $Venv "Scripts\python.exe"
if (-not (Test-Path $Vpy)) {
  throw "Python in venv not found at $Vpy"
}

# --- deps (install when requirements.txt changes or --force-install) ---
$ReqHashFile = Join-Path $Venv ".req_hash"
function Get-ReqHash($p) {
  if (Test-Path $p) { (Get-FileHash -Algorithm SHA256 -Path $p).Hash } else { "" }
}
$CurHash = Get-ReqHash $Req
$OldHash = (Test-Path $ReqHashFile) ? (Get-Content $ReqHashFile -ErrorAction SilentlyContinue | Select-Object -First 1) : ""

if (-not (Test-Path $Req)) {
  Write-Warning "[RUN] WARNING: requirements.txt not found; skipping installs."
}
elseif ($FORCE -or $CurHash -ne $OldHash) {
  Write-Host "[RUN] Installing/Updating deps from requirements.txt"
  & $Vpy -m pip install -U pip | Out-Null
  & "$Venv\Scripts\pip.exe" install -r "$Req"
  Set-Content -Path $ReqHashFile -Value $CurHash
}
else {
  Write-Host "[RUN] Deps up-to-date (requirements.txt unchanged) — skipping install"
}

# --- Launch ---
if ($MODE -eq "backend") {
  Write-Host "[RUN] Backend mode → python umd2.py $Args"
  & $Vpy (Join-Path $ProjDir "umd2.py") @Args
  exit $LASTEXITCODE
}
else {
  Write-Host "[RUN] GUI mode (default) → python gui.py"
  Write-Host "[RUN] Tips:"
  Write-Host "       • For backend: .\run --backend --serial COM3 --baud 921600 --out jsonl"
  Write-Host "       • Force reinstall deps: .\run --force-install"
  & $Vpy (Join-Path $ProjDir "gui.py") @Args
  exit $LASTEXITCODE
}
