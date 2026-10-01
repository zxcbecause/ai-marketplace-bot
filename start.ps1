# Поднимает SearXNG (если ещё не запущен) и бота. Всё detached — окно PowerShell можно закрыть.

$searxngPort = 8888
$searxngDir = if ($env:SEARXNG_DIR) { $env:SEARXNG_DIR } else { "C:\searxng" }
$searxngLog = "$searxngDir\searxng.log"
$botDir = $PSScriptRoot
$dataDir = "$botDir\data"
$stdoutPath = "$dataDir\bot_stdout.log"
$stderrPath = "$dataDir\bot_stderr.log"

# Архивируем логи предыдущего запуска ПЕРЕД перезаписью (17.07.2026, по
# опыту второго ПК, отчёт report_20260716.md): -RedirectStandardError ниже
# затирает лог при каждом старте — если бот упал прямо перед рестартом,
# причина терялась безвозвратно. Храним последние 10 архивов.
if (Test-Path $stderrPath) {
    $timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
    Copy-Item $stderrPath "$dataDir\crash_${timestamp}_bot_stderr.log" -ErrorAction SilentlyContinue
    if (Test-Path $stdoutPath) {
        Copy-Item $stdoutPath "$dataDir\crash_${timestamp}_bot_stdout.log" -ErrorAction SilentlyContinue
    }
    Get-ChildItem "$dataDir\crash_*_bot_stderr.log" -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -Skip 10 | Remove-Item -ErrorAction SilentlyContinue
    Get-ChildItem "$dataDir\crash_*_bot_stdout.log" -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -Skip 10 | Remove-Item -ErrorAction SilentlyContinue
}

$portOpen = Test-NetConnection -ComputerName 127.0.0.1 -Port $searxngPort -InformationLevel Quiet -WarningAction SilentlyContinue

if (-not $portOpen) {
    Write-Host "SearXNG не отвечает на порту $searxngPort — запускаю..."
    Start-Process -FilePath "$searxngDir\.venv\Scripts\python.exe" `
        -ArgumentList "-m", "searx.webapp" `
        -WorkingDirectory $searxngDir `
        -WindowStyle Hidden `
        -RedirectStandardOutput $searxngLog `
        -RedirectStandardError "$searxngDir\searxng.err.log"
    Start-Sleep -Seconds 3
} else {
    Write-Host "SearXNG уже запущен на порту $searxngPort"
}

Write-Host "Проверяю, не запущен ли уже бот..."
Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
    Where-Object { $_.CommandLine -like '*main.py*' } |
    ForEach-Object {
        Write-Host "Убиваю старый процесс бота (PID $($_.ProcessId))"
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
Start-Sleep -Seconds 2

Write-Host "Запускаю бота..."
Start-Process -FilePath "$botDir\.venv\Scripts\python.exe" `
    -ArgumentList "main.py" `
    -WorkingDirectory $botDir `
    -WindowStyle Hidden `
    -RedirectStandardOutput $stdoutPath `
    -RedirectStandardError $stderrPath

Write-Host "Готово. SearXNG: http://127.0.0.1:$searxngPort  Бот: запущен detached."
