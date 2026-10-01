"""
Сервис ремонта карточек Ozon с ошибками модерации.

Поддерживает:
- attribute_id 4194 — нарушение на лицевом фото (реклама/контакты): убирается,
  новая инфографика встаёт первой.
- attribute_id 4195 — нарушение на доп. фото: убирается последнее доп. фото.
- Прочие ошибки атрибутов: переимпорт с новой инфографикой и rich content
  (часто помогает при зависших задачах и ошибках валидации).

Импортируется из handlers/ozon.py (команда /ozon_fix).
"""
import asyncio
import io
import logging

import requests
from PIL import Image

from services.ozon.client import (
    get_category_attributes,
    get_import_info,
    get_products_attributes,
    get_products_info,
    import_product,
)
from services.ozon.richcontent_json import build_image_rich_content
from services.image import (
    make_infographic,
    make_gaming_infographic,
    make_richcontent,
    pick_gaming_accent,
)
from services.storage import upload_image
from services.wb_content import get_wb_card_data
from handlers.image import (
    _extract_features,
    _extract_rich_slogan,
    _extract_slogan,
    _extract_tips,
    _get_user_style,
)
from utils.billing import save_cost

log = logging.getLogger(__name__)

RICHCONTENT_ATTR_ID = 11254
BRAND_ATTR_ID       = 85
ANNOTATION_ATTR_ID  = 4191

_attr_names_cache: dict[tuple[int, int], dict[int, str]] = {}


async def _attr_names(category_id: int, type_id: int) -> dict[int, str]:
    key = (category_id, type_id)
    if key not in _attr_names_cache:
        meta = await get_category_attributes(category_id, type_id)
        _attr_names_cache[key] = {a["id"]: a["name"] for a in meta}
    return _attr_names_cache[key]


def _attr_value(attrs: dict, attr_id: int) -> str:
    for a in attrs.get("attributes", []):
        if a["id"] == attr_id and a.get("values"):
            return a["values"][0].get("value", "")
    return ""


async def _build_context(attrs: dict) -> str:
    names = await _attr_names(attrs["description_category_id"], attrs["type_id"])
    lines = [attrs["name"]]
    annotation = _attr_value(attrs, ANNOTATION_ATTR_ID)
    if annotation:
        lines.append(annotation)
    for a in attrs.get("attributes", []):
        if a["id"] in (ANNOTATION_ATTR_ID, RICHCONTENT_ATTR_ID) or not a.get("values"):
            continue
        name = names.get(a["id"], str(a["id"]))
        value = a["values"][0].get("value", "")
        if value:
            lines.append(f"{name}: {value}")
    return "\n".join(lines)


def _strip_alpha(data: bytes) -> bytes:
    img = Image.open(io.BytesIO(data)).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95)
    return buf.getvalue()


async def _download_url(url: str) -> bytes | None:
    try:
        r = await asyncio.to_thread(requests.get, url, timeout=20)
        return r.content if r.ok else None
    except Exception as e:
        log.warning(f"Не скачалось фото {url}: {e}")
        return None


async def vision_check_violation(img_bytes: bytes) -> bool:
    """Спрашивает Gemini Vision: есть ли на фото нарушение правил Ozon?
    Проверяет рекламный/промо текст, контакты, сайты, вотермарки брендов.
    Возвращает True = нарушение найдено, False = фото чистое.
    При ошибке возвращает False (не удаляем если не уверены)."""
    from google import genai as gai
    from google.genai import types as gai_types
    from config import settings

    client = gai.Client(api_key=settings.gemini_api_key)
    from services.image.gemini import _detect_mime
    mime = _detect_mime(img_bytes)

    prompt = (
        "Проверь это фото товара на нарушения правил маркетплейса Ozon.\n\n"
        "Ответь VIOLATION если на фото ЕСТЬ ХОТЯ БЫ ОДНО из следующего:\n"
        "1. Цены, скидки и распродажные плашки — «SALE», «СКИДКА 50%», «Лучшая цена», "
        "зачёркнутые цены, проценты скидки. ВАЖНО: инфографика с ХАРАКТЕРИСТИКАМИ товара "
        "(название модели, спецификации, преимущества, слоган) — это НЕ нарушение, "
        "такой текст на фото разрешён.\n"
        "2. Контактные данные — номер телефона, e-mail, адрес, мессенджеры (WhatsApp, "
        "Telegram, ВКонтакте и т.п.)\n"
        "3. Ссылки на сайты, QR-коды для перехода на внешние ресурсы\n"
        "4. Вотермарки с названием магазина/бренда продавца, нанесённые поверх фото "
        "(логотип магазина, надпись с URL-адресом магазина)\n"
        "5. Призывы к действию — «Купить», «Заказать», «Позвоните нам», «Подпишитесь»\n"
        "6. На фото только аксессуары/комплектующие без основного товара: "
        "только кабели, провода, шнуры питания, AUX, USB без самого устройства; "
        "только пульт, крепление, переходник без основного устройства; "
        "только чехол/насадки/подставка без самого гаджета. "
        "Если в кадре нет главного товара, а только его комплектующие — VIOLATION.\n"
        "7. На фото присутствует РОЗНИЧНАЯ УПАКОВКА/КОРОБКА товара — коробка с логотипом "
        "бренда, штрихкодом, окном для обзора или печатными изображениями/характеристиками "
        "на ней. Это VIOLATION в ЛЮБОМ случае: даже если само устройство при этом видно — "
        "через прозрачное окно коробки, напечатано на коробке, или лежит рядом с коробкой. "
        "Нужно фото товара БЕЗ упаковки.\n\n"
        "Ответь OK если фото чистое — виден сам товар, без перечисленного выше.\n\n"
        "Тексты и логотипы, напечатанные непосредственно на корпусе самого товара "
        "(шильдики, гравировка, маркировка на корпусе) — это OK, не нарушение.\n\n"
        "Ответь СТРОГО одним словом: VIOLATION или OK."
    )

    def _sync():
        return client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[
                gai_types.Part.from_bytes(data=img_bytes, mime_type=mime),
                prompt,
            ],
            config=gai_types.GenerateContentConfig(
                thinking_config=gai_types.ThinkingConfig(thinking_budget=0)
            ),
        )

    for attempt in range(2):
        try:
            response = await asyncio.wait_for(asyncio.to_thread(_sync), timeout=30.0)
            text = (response.text or "").strip().upper()
            result = "VIOLATION" in text
            log.info(f"Vision violation check: {text[:30]!r} → {'VIOLATION' if result else 'OK'}")
            return result
        except Exception as e:
            err = str(e)
            if ("429" in err or "503" in err) and attempt == 0:
                await asyncio.sleep(6)
                continue
            log.warning(f"Vision violation check failed: {e} — считаем OK (не удаляем)")
            return False
    return False


async def _find_violating_extras(extra_urls: list[str]) -> list[int]:
    """Скачивает каждое доп. фото и прогоняет через Vision.
    Возвращает индексы нарушающих фото. Если Vision ничего не нашёл —
    возвращает [-1] (последнее), чтобы хоть что-то убрать."""
    if not extra_urls:
        return []

    flagged = []
    for i, url in enumerate(extra_urls):
        data = await _download_url(url)
        if data is None:
            continue
        if await vision_check_violation(data):
            flagged.append(i)

    if not flagged:
        log.info("Vision не нашёл нарушений в доп. фото — удаляем последнее как fallback")
        flagged = [len(extra_urls) - 1]

    return flagged


async def _fetch_base_photo(offer_id: str, fallback_url: str | None) -> bytes | None:
    vendor_code = offer_id.split()[0]
    wb_data = await get_wb_card_data(vendor_code)
    raw = None
    if wb_data and wb_data.get("photos"):
        raw = wb_data["photos"][0]
    elif fallback_url:
        try:
            r = await asyncio.to_thread(requests.get, fallback_url, timeout=30)
            if r.ok:
                raw = r.content
        except Exception as e:
            log.warning(f"{offer_id}: fallback-фото не скачалось: {e}")
    return _strip_alpha(raw) if raw else None


async def scan_issues(offer_ids: list[str]) -> dict[str, dict]:
    """Сканирует все offer_id батчами по 50.
    Возвращает {offer_id: {"errors": [...], "warnings": [...]}}
    только для карточек у которых есть хотя бы одна ошибка или доработка."""
    result: dict[str, dict] = {}
    for i in range(0, len(offer_ids), 50):
        batch = offer_ids[i : i + 50]
        try:
            info_map = await get_products_info(batch)
            for oid, info in info_map.items():
                errs  = info.get("errors")   or []
                warns = info.get("warnings") or []
                if errs or warns:
                    result[oid] = {"errors": errs, "warnings": warns}
        except Exception as e:
            log.warning(f"scan_issues batch {i}-{i+50}: {e}")
    return result


# Обратная совместимость для старых вызовов
async def scan_errors(offer_ids: list[str]) -> dict[str, list[dict]]:
    issues = await scan_issues(offer_ids)
    return {oid: v["errors"] for oid, v in issues.items() if v["errors"]}


async def repair_card(
    offer_id: str,
    llm,
    user_id: int,
    errors: list[dict] | None = None,
    warnings: list[dict] | None = None,
    send_text=None,
) -> bool:
    """Чинит одну карточку (ошибки + доработки). Возвращает True при успехе.
    errors/warnings можно передать снаружи (из scan_issues) — тогда
    повторный запрос /v3/product/info/list не делается."""

    async def _say(msg: str) -> None:
        log.info(msg)
        if send_text:
            await send_text(msg)

    # Полные данные карточки нужны всегда (атрибуты, фото, размеры)
    attrs_map = await get_products_attributes([offer_id])
    attrs = attrs_map.get(offer_id)
    if not attrs:
        await _say(f"⚠️ {offer_id}: карточка не найдена")
        return False

    # info нужен для currency_code, vat, price — и для errors/warnings если не переданы
    info_map = await get_products_info([offer_id])
    info = info_map.get(offer_id) or {}

    if errors is None:
        errors = info.get("errors") or []
    if warnings is None:
        warnings = info.get("warnings") or []

    all_issues = errors + warnings
    violation_ids = {e.get("attribute_id") for e in all_issues}
    error_codes   = {e.get("code", "") for e in errors}
    drop_main  = 4194 in violation_ids
    drop_extra = 4195 in violation_ids

    primary = attrs.get("primary_image") or ""
    extra   = list(attrs.get("images") or [])

    # Доп. фото: Vision определяет какое именно нарушает
    if drop_extra and extra:
        await _say(f"🔍 {offer_id}: проверяю {len(extra)} доп. фото через Vision...")
        bad_indices = await _find_violating_extras(extra)
        bad_set = set(bad_indices)
        kept_extra = [url for i, url in enumerate(extra) if i not in bad_set]
        removed_extra = [extra[i] for i in bad_indices]
        await _say(
            f"Vision: удаляю {len(removed_extra)} из {len(extra)} доп. фото "
            f"(индексы {bad_indices})"
        )
    else:
        kept_extra = extra

    kept_primary = None if drop_main else (primary or None)
    kept = ([kept_primary] if kept_primary else []) + kept_extra

    fallback = kept[0] if kept else (primary or None)
    base_photo = await _fetch_base_photo(offer_id, fallback)
    if not base_photo:
        await _say(f"🔴 {offer_id}: базовое фото не получено — пропускаю")
        return False

    product = attrs.get("name", offer_id)
    brand   = _attr_value(attrs, BRAND_ATTR_ID)
    context = await _build_context(attrs)
    user_style = await _get_user_style(user_id)

    features, feat_resp = await _extract_features(
        context, product, "", llm, gaming=(user_style == "gaming"),
    )
    await save_cost(user_id, "repair_features", response=feat_resp)

    gaming_accent = None
    if user_style == "gaming":
        gaming_accent = await pick_gaming_accent(product, features, llm)
        infographic, bg_warn = await make_gaming_infographic(
            product, features, base_photo, llm=llm, brand=brand, accent=gaming_accent,
        )
    else:
        slogan, slogan_resp = await _extract_slogan(context, product, llm)
        await save_cost(user_id, "repair_slogan", response=slogan_resp)
        infographic, bg_warn = await make_infographic(
            product, features, base_photo, llm=llm, brand=brand, slogan=slogan,
        )

    if not infographic:
        await _say(f"🔴 {offer_id}: инфографика не сгенерировалась ({bg_warn})")
        return False

    infographic_url = await upload_image(infographic, ext="jpg")
    if not infographic_url:
        await _say(f"🔴 {offer_id}: не залилась инфографика на R2")
        return False

    tips, tips_resp = await _extract_tips(context, product, llm)
    await save_cost(user_id, "repair_tips", response=tips_resp)

    rich_slogan, rich_resp = await _extract_rich_slogan(context, product, tips, llm)
    await save_cost(user_id, "repair_rich_slogan", response=rich_resp)
    if rich_slogan:
        tips = tips + [(rich_slogan, "")]

    richcontent = await make_richcontent(
        product, features, tips, base_photo,
        extra_photos=[], llm=llm, gaming_accent=gaming_accent,
    )
    rich_content_json: str | None = None
    if richcontent:
        rich_url = await upload_image(richcontent, ext="jpg")
        if rich_url:
            rich_content_json = build_image_rich_content(rich_url)

    new_images = [infographic_url] + kept
    new_attrs = [
        {"id": a["id"], "complex_id": a.get("complex_id", 0), "values": a["values"]}
        for a in attrs.get("attributes", [])
        if a["id"] != RICHCONTENT_ATTR_ID
    ]
    # Добавляем обязательный атрибут если отсутствует в карточке
    _REQUIRED_DEFAULTS = {23536: "false"}  # 23536 = "Нужен код маркировки" (Boolean)
    existing_ids = {a["id"] for a in new_attrs}
    for attr_id, default_val in _REQUIRED_DEFAULTS.items():
        if attr_id not in existing_ids:
            new_attrs.append({"id": attr_id, "complex_id": 0, "values": [{"value": default_val}]})
    if rich_content_json:
        new_attrs.append({
            "id": RICHCONTENT_ATTR_ID, "complex_id": 0,
            "values": [{"value": rich_content_json}],
        })

    item: dict = {
        "offer_id": offer_id,
        "name": attrs["name"],
        "description_category_id": attrs["description_category_id"],
        "type_id": attrs["type_id"],
        "images": new_images,
        "attributes": new_attrs,
        "currency_code": info.get("currency_code", "KZT"),
        "vat": info.get("vat", "0.16"),
    }
    if attrs.get("barcode"):
        item["barcode"] = attrs["barcode"]
    for dim_key in ("height", "width", "depth"):
        if attrs.get(dim_key):
            item[dim_key] = attrs[dim_key]
    if attrs.get("height") and attrs.get("width") and attrs.get("depth"):
        item["dimension_unit"] = attrs.get("dimension_unit", "mm")
    if attrs.get("weight"):
        item["weight"]      = attrs["weight"]
        item["weight_unit"] = attrs.get("weight_unit", "g")
    # Цена: берём из карточки. Если ошибка цены — ищем в прайсе по бренду+названию.
    price_error_codes = {"price_is_negative", "price_too_low", "price_too_high",
                         "price_is_zero", "wrong_price"}
    has_price_error = bool(error_codes & price_error_codes)
    current_price = info.get("price")
    if has_price_error or not current_price:
        try:
            from services.price_list import find_price_by_model
            import re as _re
            # Модель = название без первого слова-категории (например "Беспроводная колонка JBL Clip 5" → "Clip 5")
            name_for_search = attrs.get("name", "")
            price_from_list = find_price_by_model(brand, name_for_search)
            if price_from_list:
                current_price = str(price_from_list)
                await _say(f"💰 {offer_id}: цена из прайса → {price_from_list} ₸")
            elif has_price_error:
                await _say(f"⚠️ {offer_id}: цена не найдена в прайсе, карточка может не пройти")
        except Exception as e:
            log.warning(f"{offer_id}: ошибка поиска цены в прайсе: {e}")
    if current_price:
        item["price"] = current_price

    task_id = await import_product(item)
    await asyncio.sleep(5)
    import_result = await get_import_info(task_id)
    import_errors = [
        e.get("code", "?")
        for it in import_result.get("items", [])
        for e in it.get("errors", [])
    ]
    if import_errors:
        await _say(f"⚠️ {offer_id}: переимпорт завершён с ошибками: {import_errors}")
        return False

    statuses = ", ".join(it.get("status", "?") for it in import_result.get("items", []))
    await _say(f"✅ {offer_id}: исправлено ({statuses})")
    return True
