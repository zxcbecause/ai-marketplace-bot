@echo off
chcp 65001 >nul
cd /d "%~dp0.."
net session >nul 2>&1
if errorlevel 1 (
    echo Нужны права администратора — сейчас Windows спросит разрешение...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0..\setup_autostart.ps1"
echo.
pause
