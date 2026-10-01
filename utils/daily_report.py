"""
Вечерний отчёт по /wb_create за день — список точно созданных карточек
+ список того, что упало/зависло и требует ручной проверки.
Запускается планировщиком в main.py в 17:55 по Алматы (см. _scheduler).
"""
import asyncio
import datetime
import glob
import logging
import re

import pytz

log = logging.getLogger("daily_report")

_ERROR_RE = re.compile(
    r"ERROR handlers\.wb: wb_create \[(?P<article>.+?)\] (?P<name>.+?): (?P<reason>.+)"
)


def _today_almaty_str() -> str:
    tz = pytz.timezone("Asia/Almaty")
    return datetime.datetime.now(tz).strftime("%Y-%m-%d")


def _collect_today_errors(data_dir: str = "data") -> list[dict]:
    """Ошибки wb_create за сегодня из живого stderr-лога И архивных
    crash_*_bot_stderr.log — start.ps1 архивирует лог при каждом рестарте
    бота (см. ai_bot_v2_project), поэтому ошибки до последнего рестарта
    живут только в архиве, не в текущем bot_stderr.log."""
    today = _today_almaty_str()
    today_compact = today.replace("-", "")
    paths = [f"{data_dir}/bot_stderr.log"] + sorted(
        glob.glob(f"{data_dir}/crash_{today_compact}*_bot_stderr.log")
    )
    seen: dict[str, dict] = {}
    for path in paths:
        try:
            with open(path, encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if not line.startswith(today):
                        continue
                    m = _ERROR_RE.search(line)
                    if m:
                        article = m.group("article").strip()
                        seen[article] = {
                            "article": article,
                            "name": m.group("name").strip(),
                            "reason": m.group("reason").strip(),
                        }
        except FileNotFoundError:
            continue
    return list(seen.values())


async def build_daily_wb_report(data_dir: str = "data") -> tuple[list[dict], list[dict]]:
    """(confirmed, needs_check). confirmed — из БД (реально создано,
    подтверждено поллингом при создании) + ошибки, которые при повторной
    live-проверке прямо сейчас всё же нашлись на WB (WB иногда индексирует
    дольше обычного — см. wb_create_silent_upload_failures). needs_check —
    то, что так и не нашлось."""
    from utils.billing import get_wb_created_today
    from services.wb_content import _find_card

    confirmed = await get_wb_created_today()
    confirmed_articles = {c["article"] for c in confirmed}

    errors = _collect_today_errors(data_dir)
    needs_check = []
    for e in errors:
        if e["article"] in confirmed_articles:
            continue
        try:
            card = await asyncio.to_thread(_find_card, e["article"])
        except Exception as exc:
            log.warning(f"daily_report: recheck {e['article']} failed: {exc}")
            card = None
        if card:
            confirmed.append({
                "article": e["article"], "nm_id": card.get("nmID"),
                "title": card.get("title"), "subject": None,
                "photos_count": None, "elapsed_sec": None, "created_at": None,
            })
        else:
            needs_check.append(e)
        await asyncio.sleep(0.4)  # WB API rate-limit (429 на быстрых подряд запросах)

    return confirmed, needs_check


def format_daily_report(confirmed: list[dict], needs_check: list[dict]) -> str:
    date_str = _today_almaty_str()
    lines = [f"# WB — итоги дня {date_str} (Алматы)", ""]
    lines.append(f"## Точно созданы ({len(confirmed)})")
    for c in confirmed:
        nm = f" (nmID {c['nm_id']})" if c.get("nm_id") else ""
        lines.append(f"- {c['article']} — {c.get('title') or '?'}{nm}")
    lines.append("")
    lines.append(f"## Требуют проверки/доработки ({len(needs_check)})")
    for e in needs_check:
        lines.append(f"- {e['article']} — {e['name']}: {e['reason']}")
    return "\n".join(lines)
