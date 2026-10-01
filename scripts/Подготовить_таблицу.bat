@echo off
chcp 65001 >nul
cd /d "%~dp0.."
echo Подготовка таблицы data\declarations\к_загрузке.xlsx (карточки WB НЕ меняются)
echo.
".venv\Scripts\python.exe" tools\decl_prepare.py %*
echo.
pause
