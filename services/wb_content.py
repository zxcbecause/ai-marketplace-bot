"""
Wildberries Content API — забираем фото и габариты/вес упаковки уже
существующих карточек продавца по артикулу (vendorCode), вместо поиска по
интернету / оценки LLM. Работает только для товаров, которые уже заведены
в этом кабинете ВБ. Габариты ВБ заметно точнее LLM-оценки (сверено на
реальных SSD: LLM дал 8×0.8×11.5см при реальных 15×8×21см с ВБ).
"""
import asyncio
import logging
import time

import aiohttp
import requests

from config import settings

log = logging.getLogger(__name__)

BASE = "https://content-api.wildberries.ru"
# 20.07.2026: keep-alive соединение вместо TLS-рукопожатия на каждый запрос
# (см. services/wb_create.py — тот же приём, отдельная сессия на модуль).
_wb_session = requests.Session()


def _headers() -> dict:
    return {"Authorization": settings.wb_api_key, "Content-Type": "application/json"}


def _readonly_headers() -> dict:
    return {"Authorization": settings.wb_api_key_readonly, "Content-Type": "application/json"}


def _find_card_by_article_readonly(vendor_code: str) -> int | None:
    """Как _find_card, но через WB_API_KEY_READONLY — для фоновых сверок
    (вечерний отчёт /wb_create), которые не должны делить рейт-лимит с
    боевым ключом создания карточек (23.07.2026: 3 подряд ложных «карточка
    не появилась» из-за конкуренции запросов с этой же машины). Возвращает
    только nmID (без веса всей карточки — тут больше не нужно)."""
    body = {"settings": {"cursor": {"limit": 5}, "filter": {"withPhoto": -1, "textSearch": vendor_code}}}
    target = vendor_code.strip()
    for attempt in range(4):
        try:
            r = _wb_session.post(f"{BASE}/content/v2/get/cards/list", headers=_readonly_headers(),
                                  json=body, timeout=20)
        except requests.RequestException:
            time.sleep(3 * (attempt + 1))
            continue
        if r.status_code == 429:
            time.sleep(3 * (attempt + 1))
            continue
        try:
            r.raise_for_status()
        except Exception:
            return None
        for c in r.json().get("cards", []):
            if str(c.get("vendorCode", "")).strip() == target or str(c.get("nmID", "")).strip() == target:
                return c.get("nmID")
        return None
    return None


async def verify_articles_live(articles: list[str]) -> dict[str, int | None]:
    """Проверяет пачку артикулов на живом WB (readonly-ключ, семафор 3 —
    щадящий темп, это фоновая сверка, не часть создания карточки). Возвращает
    {article: nmID или None}."""
    sem = asyncio.Semaphore(3)
    results: dict[str, int | None] = {}

    async def _one(article: str):
        async with sem:
            results[article] = await asyncio.to_thread(_find_card_by_article_readonly, article)

    await asyncio.gather(*(_one(a) for a in articles))
    return results


def _find_card(vendor_code: str) -> dict | None:
    """Строгий матч по vendorCode ИЛИ nmID — то, что пользователи называют
    «артикулом», на практике почти всегда nmID (число из URL
    wildberries.ru/catalog/<nmID>/detail.aspx), не внутренний vendorCode
    продавца (найдено 13.07.2026 на одном из артикулов). textSearch у ВБ
    нечёткий (может вернуть похожий, но другой товар по той же модели) —
    раньше при отсутствии точного совпадения брался первый результат
    (cards[0]), что давало случайные фото не того артикула. Теперь нет
    точного совпадения — считаем, что карточки на ВБ нет.

    23.07.2026: раньше 429/обрыв соединения тут же вылетали наружу
    исключением — в create_one() внешний поллинг это переживает (просто
    считает попытку неудачной), но одноразовые скрипты (wb_fix_zero_photos
    и т.п.), вызывающие _find_card() напрямую без своего ретрая, падали на
    первом же 429 (случилось сегодня на 3 артикулах). Добавлен тот же
    бэкофф, что уже был в _find_card_by_article_readonly."""
    body = {"settings": {"cursor": {"limit": 5}, "filter": {"withPhoto": -1, "textSearch": vendor_code}}}
    target = vendor_code.strip()
    for attempt in range(4):
        try:
            r = _wb_session.post(f"{BASE}/content/v2/get/cards/list", headers=_headers(),
                                  json=body, timeout=30)
        except requests.RequestException:
            if attempt == 3:
                raise
            time.sleep(3 * (attempt + 1))
            continue
        if r.status_code == 429:
            if attempt == 3:
                r.raise_for_status()
            time.sleep(3 * (attempt + 1))
            continue
        r.raise_for_status()
        cards = r.json().get("cards", [])
        for c in cards:
            if str(c.get("vendorCode", "")).strip() == target or str(c.get("nmID", "")).strip() == target:
                return c
        return None
    return None


async def get_wb_card_data(vendor_code: str, size: str = "big") -> dict | None:
    """Возвращает {"photos": [bytes,...], "width_cm", "height_cm", "length_cm",
    "weight_kg"} для карточки ВБ с этим артикулом. None — карточки нет на ВБ.
    Размеры/вес — None там, где ВБ-карточка их не указала или isValid=False."""
    card = await asyncio.to_thread(_find_card, vendor_code)
    if not card:
        log.info(f"WB: карточка с артикулом {vendor_code!r} не найдена")
        return None

    photos_meta = card.get("photos") or []
    urls = [p.get(size) or p.get("big") for p in photos_meta if p.get(size) or p.get("big")]
    log.info(f"WB: артикул {vendor_code!r} -> {card.get('title')!r}, {len(urls)} фото")

    photos: list[bytes] = []
    async with aiohttp.ClientSession() as session:
        for url in urls:
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                    if resp.status == 200:
                        photos.append(await resp.read())
                    else:
                        log.warning(f"WB photo download {url} -> HTTP {resp.status}")
            except Exception as e:
                log.warning(f"WB photo download failed {url}: {e}")

    dims = card.get("dimensions") or {}
    width_cm = height_cm = length_cm = weight_kg = None
    if dims.get("isValid", True):
        width_cm = dims.get("width")
        height_cm = dims.get("height")
        length_cm = dims.get("length")
        weight_kg = dims.get("weightBrutto")
        if any(v is not None for v in (width_cm, height_cm, length_cm, weight_kg)):
            log.info(
                f"WB: габариты артикула {vendor_code!r}: "
                f"{width_cm}×{height_cm}×{length_cm} см, {weight_kg} кг"
            )

    # Характеристики карточки: [{"name": "Диагональ", "value": ["27"]}, ...]
    # value бывает списком или скаляром — нормализуем в строку.
    characteristics: list[tuple[str, str]] = []
    tnved = ""
    for ch in card.get("characteristics") or []:
        name = str(ch.get("name") or "").strip()
        val = ch.get("value")
        if isinstance(val, list):
            val = "; ".join(str(v) for v in val if v is not None)
        val = str(val or "").strip()
        if name and val:
            characteristics.append((name, val))
            # "Код ТН ВЭД" — необязательная характеристика WB, заполняется
            # редко (проверено вживую 10.07: у большинства карточек пусто),
            # но если продавец её всё же указал — используем как более точное
            # значение вместо фиксированного per-категории кода.
            if name == "Код ТН ВЭД":
                tnved = val

    return {
        "nmID": card.get("nmID"),
        "vendor_code": card.get("vendorCode") or vendor_code,
        "photos": photos,
        "width_cm": width_cm,
        "height_cm": height_cm,
        "length_cm": length_cm,
        "weight_kg": weight_kg,
        "brand": card.get("brand") or "",
        "title": card.get("title") or "",
        "description": str(card.get("description") or "").strip(),
        "characteristics": characteristics,
        "tnved": tnved,
    }


def _upload_video_sync(nm_id: int, video_bytes: bytes) -> dict:
    import requests as _requests
    headers = _headers()
    headers.pop("Content-Type", None)  # requests сам проставит multipart boundary
    headers["X-Nm-Id"] = str(nm_id)
    headers["X-Photo-Number"] = "1"
    files = {"uploadfile": ("video.mp4", video_bytes, "video/mp4")}
    r = _requests.post(f"{BASE}/content/v3/media/file", headers=headers, files=files, timeout=60)
    try:
        body = r.json()
    except Exception:
        body = {"raw": r.text[:500]}
    return {"status": r.status_code, "body": body}


async def upload_video(nm_id: int, video_bytes: bytes) -> dict:
    """Загружает видео на карточку WB (POST /content/v3/media/file,
    multipart, поле uploadfile) — макс. 1 видео на карточку, ≤50Мб,
    MP4/MOV, X-Photo-Number всегда '1' для видео (см. офиц. OpenAPI-спеку,
    сверено 13.07.2026 — раньше ошибочно считали, что видео нельзя
    загрузить через API вообще, это было не так)."""
    return await asyncio.to_thread(_upload_video_sync, nm_id, video_bytes)


async def get_wb_photos(vendor_code: str, size: str = "big") -> list[bytes]:
    """Совместимость: только фото (см. get_wb_card_data для габаритов)."""
    data = await get_wb_card_data(vendor_code, size=size)
    return data["photos"] if data else []


def _get_subject_charcs_by_id(subject_id: int) -> list[dict]:
    """Характеристики предмета по subjectID (актуальный ID-based метод —
    старый name-based _get_subject_characteristics на практике отдаёт 404,
    см. находку сессии 55/10.07.2026). Возвращает список dict: charcID,
    name, required, unitName, charcType, maxCount, existNamedField."""
    r = _wb_session.get(
        f"{BASE}/content/v2/object/charcs/{subject_id}",
        headers=_headers(), params={"locale": "ru"}, timeout=20,
    )
    r.raise_for_status()
    return r.json().get("data", [])


async def get_subject_charcs_by_id(subject_id: int) -> list[dict]:
    return await asyncio.to_thread(_get_subject_charcs_by_id, subject_id)


def _cards_update_sync(cards: list[dict]) -> dict:
    r = _wb_session.post(f"{BASE}/content/v2/cards/update", headers=_headers(), json=cards, timeout=30)
    try:
        body = r.json()
    except Exception:
        body = {"raw": r.text[:500]}
    return {"status": r.status_code, "body": body}


async def wb_cards_update(cards: list[dict]) -> dict:
    """POST /content/v2/cards/update — ПОЛНАЯ перезапись перечисленных
    полей карточки (нужно передавать даже то, что не меняешь — берётся из
    get/cards/list). photos/video/tags/цену через этот метод изменить
    НЕЛЬЗЯ (WB сам их игнорирует) — безопасно вызывать, даже если случайно
    не передать photos, они не пострадают. cards — массив объектов вида
    {nmID, vendorCode, brand, title, description, dimensions,
    characteristics, sizes} (см. офиц. OpenAPI-спеку, сверено 13.07.2026).

    ОБЯЗАТЕЛЬНО передавать `sizes` (эхом из get/cards/list, как и dimensions/
    characteristics) — БЕЗ него запрос отвечает чистым 200 OK, error:false,
    но МОЛЧА НИЧЕГО НЕ ПРИМЕНЯЕТ, ни к одному полю (не только к sizes),
    включая характеристики вроде ТН ВЭД. Живой репро 01.09.2026: 5 попыток
    проставить ТН ВЭД без sizes дали чистый 200 и нулевой эффект (проверено
    даже на нейтральном поле "Гарантийный срок" — тоже не применилось);
    добавление sizes в тот же payload — и то же изменение сразу
    подтвердилось (свежий updatedAt, значение реально на карточке).
    В доке WB это нигде не объяснено. Не полагайся на код статуса ответа —
    после вызова перечитай карточку и сверь конкретное изменённое поле."""
    return await asyncio.to_thread(_cards_update_sync, cards)


def _get_subject_characteristics(name: str) -> list[dict]:
    """Характеристики субъекта WB через Content API v2.
    Возвращает список dict: {charcID, name, required, unitName, charcType, maxCount}"""
    import urllib.parse
    params = urllib.parse.urlencode({"name": name, "locale": "ru"})
    r = _wb_session.get(
        f"{BASE}/content/v2/object/characteristics/list/filter?{params}",
        headers=_headers(), timeout=15,
    )
    r.raise_for_status()
    return r.json().get("data", [])


async def get_wb_subject_characteristics(name: str) -> list[dict]:
    """Асинхронная обёртка — характеристики субъекта WB по русскому названию."""
    return await asyncio.to_thread(_get_subject_characteristics, name)


def _search_subjects(query: str) -> list[dict]:
    """Поиск субъекта WB по части названия (подсказка при ошибке)."""
    r = _wb_session.get(
        f"{BASE}/content/v2/object/all?name={query}&locale=ru",
        headers=_headers(), timeout=15,
    )
    r.raise_for_status()
    return r.json().get("data", [])


async def search_wb_subjects(query: str) -> list[dict]:
    return await asyncio.to_thread(_search_subjects, query)
