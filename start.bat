@echo off
rem Double-click launcher. All logic lives in start.ps1.
rem Keep this file ASCII-only with CRLF line endings: cmd.exe misparses LF-only or UTF-8 batch files.
setlocal
rem A PSModulePath inherited from PowerShell 7 breaks cmdlet auto-loading in Windows PowerShell 5.1;
rem clearing it makes powershell.exe fall back to its default module path.
set "PSModulePath="
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
exit /b %errorlevel%
