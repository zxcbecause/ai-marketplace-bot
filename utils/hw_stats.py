"""
Снимок RAM/GPU в виде чисел (не готовой строки) — для сохранения в БД при
каждой карточке/провале (см. utils.billing.save_wb_card_created /
save_wb_create_failure) и последующей агрегации в дневном отчёте
(utils/daily_xlsx_report.py). Текстовая версия для чек-инов в Telegram живёт
отдельно в handlers/wb.py::_health_snapshot — та же идея, тот же
nvidia-smi-приём, не объединяю ради минимального риска правок в файле,
который сегодня и так менялся несколько раз.
"""
import asyncio


async def snapshot() -> dict:
    import psutil
    vm = psutil.virtual_memory()
    result = {
        "ram_free_gb": round(vm.available / 1024 ** 3, 2),
        "ram_total_gb": round(vm.total / 1024 ** 3, 2),
        "gpu_used_mb": None,
        "gpu_total_mb": None,
        "gpu_util_pct": None,
    }
    try:
        proc = await asyncio.create_subprocess_exec(
            "nvidia-smi", "--query-gpu=memory.used,memory.total,utilization.gpu",
            "--format=csv,noheader,nounits",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        used, total, util = (x.strip() for x in out.decode().strip().split(","))
        result["gpu_used_mb"] = float(used)
        result["gpu_total_mb"] = float(total)
        result["gpu_util_pct"] = float(util)
    except Exception:
        pass
    return result
