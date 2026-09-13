@echo off
setlocal
cd /d "%~dp0"
set PORT=8787

netstat -ano | findstr ":%PORT% " | findstr LISTENING >nul
if %errorlevel% neq 0 (
    start "Obsidian Dashboard Server" /min cmd /k python -m http.server %PORT%
    timeout /t 2 /nobreak >nul
)

start "" http://localhost:%PORT%/dashboard.html
