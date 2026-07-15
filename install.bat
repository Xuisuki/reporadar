@echo off
REM RepoRadar — двойной клик под Windows: запускает install.ps1.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
pause
