@echo off
chcp 65001 >nul
cd /d "%~dp0.."
echo Сборка таблицы к_загрузке.xlsx из УЖЕ прочитанных документов — без Gemini, лимит ИИ не тратится.
echo.
".venv\Scripts\python.exe" tools\decl_prepare.py --no-gemini
echo.
pause
