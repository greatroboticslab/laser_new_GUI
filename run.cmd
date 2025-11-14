@echo off
setlocal

REM Resolve absolute path to this folder
set "SELF=%~f0"
set "ROOT=%~dp0"

REM Use Windows PowerShell (v1.0 path exists on all supported Windows)
set "PS=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"

REM If PowerShell 7 (pwsh) exists and you prefer it, uncomment next 3 lines:
REM for %%I in (pwsh.exe) do set "HAS_PWSH=%%~$PATH:I"
REM if defined HAS_PWSH (
REM   set "PS=pwsh.exe"
REM )

REM Run the PowerShell driver without echoing the command
"%PS%" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%ROOT%run.ps1" %*
exit /b %ERRORLEVEL%

