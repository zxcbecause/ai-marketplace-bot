# Проверяет, жив ли процесс бота; если нет — поднимает через start.ps1.
# Предназначен для запуска по расписанию (Task Scheduler, каждые 5 минут).

# Pause file: if present, watchdog does nothing (deliberate bot shutdown
# for maintenance). Delete the file to re-enable auto-restart.
if (Test-Path "$PSScriptRoot\data\watchdog_pause") {
    exit 0
}

$running = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
    Where-Object { $_.CommandLine -like '*main.py*' }

if (-not $running) {
    $logPath = "$PSScriptRoot\data\watchdog.log"
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $logPath -Value "$timestamp - Бот не найден, перезапускаю..."
    powershell -ExecutionPolicy Bypass -File "$PSScriptRoot\start.ps1" *>> $logPath
}
