"""
Дневной xlsx-отчёт по /wb_create — формат из ручного отчёта 23.07.2026
(3 листа: Созданные / Отклонённые / Не успели), плюс новый 4-й лист —
нагрузка на ПК по категориям (RAM/GPU/время на карточку), чтобы было что
анализировать для оптимизации кода (см. utils/hw_stats.py — снимок
пишется в БД при каждой карточке/провале, см. utils/billing.py).

Собирается и отправляется вечерним планировщиком (main.py::_send_hello_goodbye)
через bot.send_document — рядом с коротким текстовым резюме, не вместо него.
"""
import datetime
import glob
import io
import json
import statistics as _stats

import openpyxl
from openpyxl.styles import Font

from utils.billing import get_wb_cards_today_full, get_wb_create_failures_today_full

_HEADER_FONT = Font(bold=True)
_INFOGRAPHIC_REGISTRY = "data/infographic_project_uploaded.json"
_INFOGRAPHIC_AUDIT = "data/_audit_02_09_result.jsonl"


def _autosize(ws):
    for col in ws.columns:
        length = max((len(str(c.value)) if c.value is not None else 0) for c in col)
        ws.column_dimensions[col[0].column_letter].width = min(max(length + 2, 10), 80)


def _write_header(ws, headers: list[str]):
    ws.append(headers)
    for cell in ws[1]:
        cell.font = _HEADER_FONT


def _today_restarts(data_dir: str = "data") -> list[str]:
    """Времена перезапусков бота сегодня (по имени архивного crash_*.log —
    start.ps1 архивирует лог при каждом старте, см. [[ai_bot_v2_project]]).
    Не даёт точного списка «не успевших» позиций — Excel-файл батча нигде не
    сохраняется на диск (bot.download() читает прямо в память, см. находку
    24.07.2026), поэтому лист «Не успели» честно предупреждает о перезапусках
    вместо того чтобы делать вид, что список полный."""
    today_compact = datetime.datetime.now().strftime("%Y%m%d")
    times = []
    for path in sorted(glob.glob(f"{data_dir}/crash_{today_compact}_*_bot_stderr.log")):
        parts = path.split("_")
        stamp = parts[-4] + "_" + parts[-3]
        try:
            t = datetime.datetime.strptime(stamp, "%Y%m%d_%H%M%S")
            times.append(t.strftime("%H:%M:%S"))
        except ValueError:
            continue
    return times


def _infographic_project_data(today_str: str) -> dict:
    """Читает реестр проекта 'инфографика для 119 аудированных карточек'
    (data/infographic_project_uploaded.json — единственный источник правды,
    пополняется ТОЛЬКО после реальной заливки на живую карточку, см.
    [[ai_bot_v2_audit_119_articles]]) и обогащает названием/категорией
    из аудита, если статья там есть."""
    try:
        with open(_INFOGRAPHIC_REGISTRY, encoding="utf-8") as f:
            reg = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"uploaded_today": [], "pending": [], "deferred": [], "unavailable": []}

    meta: dict[str, dict] = {}
    try:
        with open(_INFOGRAPHIC_AUDIT, encoding="utf-8") as f:
            for line in f:
                d = json.loads(line)
                meta[d["article"]] = d
    except FileNotFoundError:
        pass

    def enrich(article: str) -> tuple[str, str]:
        d = meta.get(article)
        if not d:
            return "", ""
        return d.get("title") or "", d.get("category") or ""

    uploaded_today = []
    for article, date in (reg.get("uploaded") or {}).items():
        if date == today_str:
            title, category = enrich(article)
            uploaded_today.append((article, title, category, date))

    pending = []
    for article, date in (reg.get("generated_pending_upload") or {}).items():
        title, category = enrich(article)
        pending.append((article, title, category, date))

    deferred = [(article, reason) for article, reason in (reg.get("deferred") or {}).items()]
    unavailable = [(article, reason) for article, reason in (reg.get("unavailable") or {}).items()]

    return {
        "uploaded_today": uploaded_today,
        "pending": pending,
        "deferred": deferred,
        "unavailable": unavailable,
    }


def _hw_row(d: dict) -> str:
    parts = []
    if d.get("ram_free_gb") is not None:
        parts.append(f"RAM своб. {d['ram_free_gb']:.1f}ГБ")
    if d.get("gpu_used_mb") is not None and d.get("gpu_total_mb") is not None:
        parts.append(f"GPU {d['gpu_used_mb']:.0f}/{d['gpu_total_mb']:.0f}МБ")
    if d.get("gpu_util_pct") is not None:
        parts.append(f"util {d['gpu_util_pct']:.0f}%")
    return ", ".join(parts)


async def build_daily_xlsx_report() -> tuple[bytes, dict]:
    """Возвращает (xlsx_bytes, summary) — summary содержит числа для
    сопроводительного текстового сообщения (created/failed/restarts)."""
    created = await get_wb_cards_today_full()
    failed = await get_wb_create_failures_today_full()
    restarts = _today_restarts()
    today_str = datetime.datetime.now().strftime("%Y-%m-%d")
    infographic = _infographic_project_data(today_str)

    wb = openpyxl.Workbook()

    ws1 = wb.active
    ws1.title = "Созданные"
    _write_header(ws1, ["Артикул", "Название", "Категория", "На карточке (nmID)"])
    for c in created:
        ws1.append([c["article"], c["title"], c["subject"] or "", c["nm_id"]])
    _autosize(ws1)

    ws2 = wb.create_sheet("Отклонённые")
    _write_header(ws2, ["Артикул", "Название", "Категория", "Причина"])
    for f in failed:
        ws2.append([f["article"], f["name"], f["subject"] or "", f["error"]])
    _autosize(ws2)

    ws3 = wb.create_sheet("Не успели")
    _write_header(ws3, ["Комментарий"])
    if restarts:
        ws3.append([
            f"Бот перезапускался сегодня {len(restarts)} раз(а): {', '.join(restarts)}. "
            "Точный список позиций, не попавших в батч до/после перезапуска, "
            "недоступен — исходный Excel-файл батча не сохраняется на диск "
            "(скачивается прямо в память), сверяйте вручную по последнему "
            "отправленному файлу."
        ])
    else:
        ws3.append(["Перезапусков сегодня не было — все запущенные позиции должны быть в одном из других листов."])
    _autosize(ws3)

    ws4 = wb.create_sheet("Нагрузка на ПК")
    _write_header(ws4, [
        "Категория", "Карточек", "Время сред. (с)", "Время мин (с)", "Время макс (с)",
        "Фото сред.", "RAM своб. сред. (ГБ)", "RAM своб. мин (ГБ)",
        "GPU исп. сред. (МБ)", "GPU util сред. (%)",
    ])
    by_cat: dict[str, list[dict]] = {}
    for c in created:
        by_cat.setdefault(c["subject"] or "(без категории)", []).append(c)
    for cat, rows in sorted(by_cat.items(), key=lambda kv: -len(kv[1])):
        elapsed = [r["elapsed_sec"] for r in rows if r["elapsed_sec"] is not None]
        photos = [r["photos_count"] for r in rows if r["photos_count"] is not None]
        ram = [r["ram_free_gb"] for r in rows if r["ram_free_gb"] is not None]
        gpu_used = [r["gpu_used_mb"] for r in rows if r["gpu_used_mb"] is not None]
        gpu_util = [r["gpu_util_pct"] for r in rows if r["gpu_util_pct"] is not None]
        ws4.append([
            cat, len(rows),
            round(_stats.mean(elapsed), 1) if elapsed else None,
            round(min(elapsed), 1) if elapsed else None,
            round(max(elapsed), 1) if elapsed else None,
            round(_stats.mean(photos), 1) if photos else None,
            round(_stats.mean(ram), 2) if ram else None,
            round(min(ram), 2) if ram else None,
            round(_stats.mean(gpu_used), 0) if gpu_used else None,
            round(_stats.mean(gpu_util), 0) if gpu_util else None,
        ])
    ws4.append([])
    all_ram = [c["ram_free_gb"] for c in created if c.get("ram_free_gb") is not None]
    all_elapsed = [c["elapsed_sec"] for c in created if c.get("elapsed_sec") is not None]
    ws4.append(["ИТОГО", len(created),
                round(_stats.mean(all_elapsed), 1) if all_elapsed else None, "", "", "", "", "", "", ""])
    if all_ram:
        ws4.append([f"Мин. свободная RAM за день: {min(all_ram):.2f} ГБ "
                     f"(см. pc_hardware_hangs — критично ниже ~0.7-1ГБ)"])
    if restarts:
        ws4.append([f"Перезапусков бота сегодня: {len(restarts)} ({', '.join(restarts)})"])
    _autosize(ws4)

    ws5 = wb.create_sheet("Инфографика")
    _write_header(ws5, ["Артикул", "Название", "Категория", "Статус", "Дата"])
    for article, title, category, date in infographic["uploaded_today"]:
        ws5.append([article, title, category, "Залито сегодня (фото + рич)", date])
    for article, title, category, date in infographic["pending"]:
        ws5.append([article, title, category, "Сгенерено, ждёт заливки", date])
    if infographic["deferred"] or infographic["unavailable"]:
        ws5.append([])
        ws5.append(["Требует внимания:"])
        for article, reason in infographic["deferred"]:
            ws5.append([article, "", "", f"Отложено — {reason}", ""])
        for article, reason in infographic["unavailable"]:
            ws5.append([article, "", "", f"Недоступно — {reason}", ""])
    _autosize(ws5)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue(), {
        "created": len(created),
        "failed": len(failed),
        "restarts": len(restarts),
        "infographic_uploaded_today": len(infographic["uploaded_today"]),
        "infographic_pending": len(infographic["pending"]),
    }
