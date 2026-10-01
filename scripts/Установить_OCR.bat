@echo off
chcp 65001 >nul
cd /d "%~dp0.."
echo Установка распознавания сканов на этом компьютере (EasyOCR) — один раз, 2-5 минут.
echo.
".venv\Scripts\python.exe" -m pip install easyocr
echo.
echo Готово. Теперь Подготовить_таблицу.bat будет читать сканы и без Gemini.
pause
