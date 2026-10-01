@echo off
chcp 65001 >nul
title Что делает бот (живой лог)
echo Живой лог бота: новые строки появляются сами. Закрыть — просто закройте окно.
echo Технические строки (загрузка моделей, служебные запросы) скрыты.
echo.
powershell -NoProfile -Command "[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-Content -Path '%~dp0..\data\bot_stderr.log' -Encoding UTF8 -Tail 40 -Wait | Where-Object { $_ -notmatch 'huggingface|Loading weights|AFC is enabled|127.0.0.1:8888' }"
