# Однократная настройка Windows: после включения/перезагрузки ПК бот и SearXNG поднимаются сами.
# Запускать через scripts\Настроить_автозапуск.bat (сам попросит права администратора).
# Повторный запуск безопасен: задачи просто пересоздаются.

$ErrorActionPreference = "Continue"
$Bot = $PSScriptRoot
$User = "$env:USERDOMAIN\$env:USERNAME"
function Ok($m)   { Write-Host "  [OK] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "  [!]  $m" -ForegroundColor Yellow }

Write-Host "`n=== Настройка автозапуска AI-Bot ($Bot) ===`n"

if (-not (Test-Path "$Bot\start.ps1") -or -not (Test-Path "$Bot\watchdog.ps1")) {
    Write-Host "Не нашёл start.ps1 / watchdog.ps1 рядом со скриптом. Положите файл в папку бота." -ForegroundColor Red
    exit 1
}

# 1. Питание: никогда не засыпать, не гибернировать, выключить быстрый запуск
Write-Host "1. Питание"
powercfg /change standby-timeout-ac 0
powercfg /change standby-timeout-dc 0
powercfg /change hibernate-timeout-ac 0
powercfg /change hibernate-timeout-dc 0
Ok "Сон и гибернация отключены (экран по-прежнему гаснет по своему таймеру)"
powercfg /hibernate off | Out-Null
Ok "Гибернация и «быстрый запуск» выключены (нужно для автовключения из BIOS и Wake-on-LAN)"

# Wi-Fi/сетевые адаптеры: не отключать ради экономии энергии
try {
    Get-NetAdapter -Physical | Where-Object Status -eq "Up" | ForEach-Object {
        Set-NetAdapterPowerManagement -Name $_.Name -AllowComputerToTurnOffDevice Disabled -ErrorAction Stop
        Ok "Адаптер «$($_.Name)»: Windows больше не отключает его для экономии энергии"
    }
} catch { Warn "Не удалось поменять энергосбережение сетевого адаптера: $($_.Exception.Message)" }

# 2. Пауза сторожа
Write-Host "`n2. Сторож"
$pause = "$Bot\data\watchdog_pause"
if (Test-Path $pause) { Remove-Item $pause -Force; Ok "Удалён data\watchdog_pause — сторож снова поднимает бота" }
else { Ok "Пауза сторожа не стоит" }

# 3. Задачи планировщика
Write-Host "`n3. Задачи планировщика"
$ps = "powershell.exe"
$principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited
$settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
             -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -MultipleInstances IgnoreNew

# 3a. Запуск бота + SearXNG через минуту после входа в Windows
$t1 = New-ScheduledTaskTrigger -AtLogOn -User $User
$t1.Delay = "PT1M"
$a1 = New-ScheduledTaskAction -Execute $ps -WorkingDirectory $Bot `
      -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Bot\start.ps1`""
Register-ScheduledTask -TaskName "AI-Bot Автозапуск" -Action $a1 -Trigger $t1 -Principal $principal `
    -Settings $settings -Description "Поднимает бота и SearXNG после входа в Windows" -Force | Out-Null
Ok "«AI-Bot Автозапуск»: бот и SearXNG стартуют через 1 мин после входа в Windows"

# 3b. Сторож каждые 5 минут (если бот упал — поднимет)
$t2 = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
      -RepetitionInterval (New-TimeSpan -Minutes 5)
$a2 = New-ScheduledTaskAction -Execute $ps -WorkingDirectory $Bot `
      -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Bot\watchdog.ps1`""
Register-ScheduledTask -TaskName "AI-Bot Сторож" -Action $a2 -Trigger @($t2, (New-ScheduledTaskTrigger -AtLogOn -User $User)) `
    -Principal $principal -Settings $settings -Description "Каждые 5 минут проверяет, жив ли бот" -Force | Out-Null
Ok "«AI-Bot Сторож»: проверка каждые 5 минут"

# 4. Итог и то, что скрипт сделать не может
Write-Host "`n=== Готово ===`n"
Write-Host "Проверка: откройте «Планировщик заданий» — там задачи «AI-Bot Автозапуск» и «AI-Bot Сторож»."
Write-Host ""
Warn "Осталось вручную (скрипт этого не делает специально):"
Write-Host "  - Автовход в Windows: Win+R -> netplwiz -> снять «Требовать ввод имени пользователя и пароля»."
Write-Host "    Без этого после перезагрузки ПК будет ждать пароль, и задачи не запустятся."
Write-Host "  - Автовключение ПК: в BIOS включить «Restore on AC Power Loss = Power On»"
Write-Host "    и/или «Power On By RTC» (например, каждый день в 08:55)."
