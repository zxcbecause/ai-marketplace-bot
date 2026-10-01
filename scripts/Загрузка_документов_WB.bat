@echo off
chcp 65001 >nul
cd /d "%~dp0.."
echo Загрузка деклараций/сертификатов на WB из data\declarations\к_загрузке.xlsx
echo Сначала будет ПЛАН без отправки, потом вопрос.
echo.
".venv\Scripts\python.exe" tools\wb_docs_upload.py %*
echo.
pause
