"""
Общий пайплайн «товар → карточка Ozon», параметризованный по категории
(см. services/ozon_categories.py): поиск контекста/описания, поиск до 4
фото с заливкой на R2 (Ozon не принимает page-ссылки от Playwright-
источников — поэтому байты перезаливаются на свой бакет), характеристики
по реальным полям/справочникам шаблона категории, упаковка (с ВБ — точнее
оценки LLM — или оценка LLM, если товара нет на ВБ), инфографика+rich
content (с учётом стиля пользователя), заполнение Excel (архив) и живой
вызов Ozon Seller API (/v3/product/import) — товар реально создаётся
в кабинете.

Используется и из tools/ozon_ssd_fill.py (отдельный фоновый скрипт),
и из handlers/ozon.py (команда /ozon в боте) — чтобы не дублировать логику.

send_text(text)/send_photo(bytes, caption) — async-функции доставки,
разные для скрипта (через aiohttp Telegram API) и хендлера (через aiogram).
"""
import asyncio
import logging
from typing import Awaitable, Callable

from services.card import generate_full_card
from services.card.generator import generate_packaging
from services.search import find_product_images
from services.image import make_infographic, make_second_slide, make_gaming_infographic, make_signal_infographic, make_chair_infographic, make_simple_infographic, make_speakers_infographic, make_richcontent, pick_gaming_accent, pick_bg_palette
from services.storage import upload_image
from services.ozon import (
    get_category_attributes, build_attributes_payload, import_product, get_import_info,
    build_image_rich_content,
)
from services.ozon.client import update_product_attributes as _update_attrs
from services.ozon_categories import CategoryProfile
from services.price_list import find_price, find_price_by_model
from services.wb_content import get_wb_card_data
from handlers.image import (
    _extract_features, _extract_slogan, _extract_tips, _extract_rich_slogan,
    _extract_visuals,
    _get_user_style, USE_VISION, SLIDE2_CATEGORIES,
)
from utils.billing import save_cost
from utils.gpu_safety import run_gpu
from utils.excel_filler import _parse_chars, _translate_color
from utils.ozon_filler import OzonExcelFiller
from prompts import OZON_NAME_PROMPT, HASHTAG_PROMPT

log = logging.getLogger(__name__)

# Цену пока не подставляем — оставляем пустой, продавец заполнит сам.
DEFAULT_PRICE = None
N_PHOTOS = 4

# ══════════════════════════════════════════════════════════════════════
# ВРЕМЕННЫЙ РЕЖИМ СКОРОСТИ (09.07.2026) — гонка по объёму карточек Ozon.
# ОТКАТ К ПРЕЖНЕМУ КАЧЕСТВУ: поставить все три флага обратно в True,
# больше НИЧЕГО менять не нужно.
#
# GENERATE_INFOGRAPHIC=False — НЕ генерируем инфографику и rich content
# (DeepSeek на характеристики/слоганы + Gemini на фон/фото-композицию +
# rembg-вырезка ×2) — карточка уходит на Ozon с WB-фото как есть. Было
# самой долгой и дорогой частью на карточку.
#
# FILTER_WB_PHOTOS_VISION=False — WB-фото НЕ проверяются Gemini Vision
# массово (было 7-8 последовательных/параллельных вызовов на карточку).
#
# CHECK_COVER_PHOTO_VISION=False — ПОЛНОСТЬЮ выключена Vision-проверка,
# включая лицевое фото (было: хотя бы обложку проверять). Осознанный риск:
# вотермарки/коробки могут проскочить на любое фото, включая первое —
# приняли ради скорости, смотрим по факту, что реально прилетает на Ozon.
# ══════════════════════════════════════════════════════════════════════
GENERATE_INFOGRAPHIC = False
FILTER_WB_PHOTOS_VISION = False
CHECK_COVER_PHOTO_VISION = False
GENERATE_HASHTAGS = True

SendText = Callable[[str], Awaitable[None]]
SendPhoto = Callable[[bytes, str], Awaitable[None]]


async def load_filler_and_attrs(profile: CategoryProfile) -> tuple[OzonExcelFiller, list[dict]]:
    """Загружает Excel-шаблон категории и список её атрибутов из Ozon API.
    Вызывать один раз на весь батч (не на каждый товар)."""
    filler = OzonExcelFiller(profile.template, profile.category)
    if not filler.load():
        raise RuntimeError(f"Не удалось загрузить Ozon-шаблон: {profile.template}")
    attrs_meta = await get_category_attributes(profile.description_category_id, profile.type_id)
    log.info(f"Атрибутов категории {profile.key!r}: {len(attrs_meta)}")
    return filler, attrs_meta


async def process_ozon_product(
    profile: CategoryProfile,
    raw: str, article: str, filler: OzonExcelFiller, attrs_meta: list[dict],
    llm, user_id: int, *,
    price: int | None = DEFAULT_PRICE,
    photo_source: str = "search",
    wb_article: str | None = None,
    send_text: SendText,
    send_photo: SendPhoto,
) -> None:
    """Полный цикл для одного товара: описание → фото/R2 → характеристики →
    упаковка → инфографика/rich content → строка в Excel → живой Ozon API.
    wb_article — если нумерация WB и Ozon у продавца РАЗНАЯ (свой внутренний
    SKU ≠ артикул на WB, см. батч пылесосов 13.07.2026): article остаётся
    Ozon offer_id, а поиск карточки ВБ идёт по wb_article. По умолчанию
    совпадают (как во всех предыдущих категориях)."""
    # При WB-режиме карточку ВБ тянем ДО генерации: её характеристики/описание
    # идут в контекст LLM вместо Exa-поиска (WB-first, экономия ~$0.02/товар),
    # а бренд нужен для прайса, когда нормализатор не нашёл его в названии.
    wb_data: dict | None = None
    wb_context: str | None = None
    wb_rich = False
    if photo_source == "wb":
        wb_data = await get_wb_card_data(wb_article or article)
        if wb_data and (wb_data.get("characteristics") or wb_data.get("description")):
            chars = wb_data.get("characteristics") or []
            block = [f"=== Наша карточка Wildberries (артикул {article}, проверенные данные) ==="]
            if wb_data.get("title"):
                block.append(f"Название: {wb_data['title']}")
            if wb_data.get("brand"):
                block.append(f"Бренд: {wb_data['brand']}")
            if chars:
                block.append("Характеристики:\n" + "\n".join(f"{n}: {v}" for n, v in chars))
            if wb_data.get("description"):
                block.append(f"Описание:\n{wb_data['description']}")
            wb_context = "\n".join(block) + "\n"
            wb_rich = len(chars) >= 6
            log.info(f"WB-first контекст: {len(chars)} характеристик, "
                     f"описание {len(wb_data.get('description') or '')} симв., rich={wb_rich}")

    result = await generate_full_card(raw, llm, desc_only=True,
                                      wb_context=wb_context, wb_context_rich=wb_rich)
    category = result.category or profile.category

    # Бренд для поиска цены: нормализатор → карточка ВБ → пусто
    effective_brand = result.brand or (wb_data["brand"] if wb_data else "") or ""

    # Цена — из прайс-листа продавца (бренд+модель+объём), если не передана
    # явно вызывающим кодом. Не нашли точного совпадения — оставляем пустой,
    # не угадываем.
    if price is None:
        price = find_price(
            effective_brand, result.memory, result.model,
            category_hint=profile.category.lower().split()[0],
        )
    # Fallback для товаров без объёма (акустика, наушники и т.п.)
    if price is None and not result.memory:
        price = find_price_by_model(
            effective_brand, result.model,
            category_hint=profile.category.lower().split()[0],
        )

    name_hint = f"Товар: {result.product}\nБренд: {result.brand}\n\n{result.context[:800]}"
    name_resp = await llm.chat(name_hint, OZON_NAME_PROMPT)
    ozon_name = name_resp.text.strip().strip('"').strip("'").splitlines()[0][:200]
    # Гарантируем наличие бренда в названии — LLM иногда его пропускает
    if result.brand and result.brand.lower() not in ozon_name.lower():
        parts = ozon_name.split(None, 1)
        if len(parts) == 2:
            ozon_name = f"{parts[0]} {result.brand} {parts[1]}"
        else:
            ozon_name = f"{ozon_name} {result.brand}"
        ozon_name = ozon_name[:200]
        log.info(f"Brand inject: '{result.brand}' добавлен в название → '{ozon_name}'")
    await save_cost(user_id, "batch_wb_name", response=name_resp)

    await send_text(f"{ozon_name}\n\n{result.description}")
    for i, resp in enumerate(result.llm_responses):
        await save_cost(user_id, "batch_card", response=resp,
                        exa_requests=result.exa_requests if i == 0 else 0)

    # ── Фото: до 4 штук, каждое сразу перезаливаем на R2 ────────────────
    wb_dims: dict | None = None
    if photo_source == "wb":
        # wb_data уже получен выше (до поиска цены) — не запрашиваем повторно
        raw_wb_photos = wb_data["photos"] if wb_data else []
        if wb_data and all(wb_data.get(k) is not None for k in ("width_cm", "height_cm", "length_cm", "weight_kg")):
            wb_dims = wb_data

        if not FILTER_WB_PHOTOS_VISION:
            # Fast mode: Vision-проверки выключены целиком (см. баннер выше) —
            # берём WB-фото как есть, без похода к Gemini.
            photos = raw_wb_photos
        elif raw_wb_photos:
            # Фильтруем WB-фото: убираем те, на которых вотермарки / промо-текст.
            # Раньше — последовательно по одному (7-8 фото × ~5-7с Gemini-вызов =
            # 40-70с/товар); без инфографики это стало непропорционально большой
            # долей времени карточки, поэтому проверяем все фото ПАРАЛЛЕЛЬНО
            # (asyncio.gather) — тот же набор проверок, тот же результат, но
            # ограничено временем САМОГО МЕДЛЕННОГО вызова, а не суммой всех.
            from services.ozon.repair import vision_check_violation

            async def _check_violation(ph: bytes) -> bool:
                try:
                    return await vision_check_violation(ph)
                except Exception:
                    return False

            violation_flags = await asyncio.gather(*(_check_violation(ph) for ph in raw_wb_photos))
            clean_wb = [ph for ph, bad in zip(raw_wb_photos, violation_flags) if not bad]
            if len(clean_wb) < len(raw_wb_photos):
                log.info(f"WB фото: {len(raw_wb_photos)} → {len(clean_wb)} после фильтрации вотермарков")

            # Отдельно от вотермарков — проверяем, что на фото сам товар, а не
            # только аксессуар/комплектация (зарядка, кабель, коробка и т.п.).
            # Без инфографики WB-фото уходит на Ozon КАК ЕСТЬ (без композиции
            # поверх карточки) — эта проверка защищает саму карточку на Ozon,
            # не только больше не актуальна для инфографики.
            if clean_wb:
                from services.image.gemini import validate_product_image

                async def _check_accessory(ph: bytes) -> bool | None:
                    try:
                        return await validate_product_image(ph, result.search_name, color=result.color)
                    except Exception:
                        return None

                accessory_flags = await asyncio.gather(*(_check_accessory(ph) for ph in clean_wb))
                verified_wb = [ph for ph, ok in zip(clean_wb, accessory_flags) if ok is not False]
                if len(verified_wb) < len(clean_wb):
                    log.info(f"WB фото: {len(clean_wb)} → {len(verified_wb)} после проверки на аксессуары")
                clean_wb = verified_wb

            photos = clean_wb
        else:
            photos = raw_wb_photos

        if not photos:
            log.warning(f"WB: фото не найдено для артикула {article!r}, ищу в интернете")
            photos = await find_product_images(
                result.search_name, n=N_PHOTOS, brand=effective_brand,
                color=result.color, color_en=result.color_en, category=category,
                llm=llm, vision_validate=USE_VISION,
            )
        elif len(photos) < 2:
            need = 1
            log.info(f"WB: только {len(photos)} фото, добираю {need} доп. ракурс поиском")
            extra = await find_product_images(
                result.search_name, n=need, brand=effective_brand,
                color=result.color, color_en=result.color_en, category=category,
                llm=llm, vision_validate=USE_VISION,
            )
            photos = photos + extra
    else:
        # search_name = product + объём — иначе фото может подобраться от
        # другой ёмкости той же линейки (кейс Patriot Burst Elite 120GB →
        # фото с этикеткой 960GB, Vision не знал, что объём не совпадает).
        photos = await find_product_images(
            result.search_name, n=N_PHOTOS, brand=result.brand,
            color=result.color, color_en=result.color_en, category=category,
            llm=llm, vision_validate=USE_VISION,
        )
    # Сортируем WB-фото по CLIP-скору — комплектация/коробка уйдёт вниз,
    # чистое фото товара окажется первым и пойдёт в инфографику.
    if photo_source == "wb" and len(photos) > 1:
        from services.image.clip_scorer import clip_score
        _scores = await run_gpu(
            lambda: [(clip_score(ph, result.search_name, category=category), ph) for ph in photos],
            timeout=90, default=None, label="clip_score(WB photos)",
        )
        if _scores is not None:
            _scores.sort(key=lambda x: x[0], reverse=True)
            photos = [ph for _, ph in _scores]
            log.info(f"WB фото CLIP-скоры: {[round(s, 3) for s, _ in _scores]}")
        else:
            log.warning("CLIP sort failed/timeout — оставляем исходный порядок фото")

    # Fast mode: остальные ракурсы летят без проверки, но ЛИЦЕВОЕ фото
    # (photos[0] — то, что видно первым в галерее и уйдёт под инфографику,
    # если её включат обратно) проверяем Vision-ом на артефакты (вотермарка/
    # коробка). Проверяем ВСЕХ кандидатов параллельно (не только топ-2) —
    # т.к. вызовы идут через asyncio.gather, время упирается в САМЫЙ
    # МЕДЛЕННЫЙ вызов, а не в их количество, так что урезать список кандидатов
    # почти не экономит время, зато чаще оставляет забракованное фото первым
    # (как случилось с Acer Predator XB273U — все 7 WB-ракурсов оказались
    # с вотермаркой стороннего магазина, топ-2 не хватило). Если ВООБЩЕ ни
    # один WB-кандидат не прошёл — как раньше, добираем один чистый ракурс
    # поиском в интернете, а не молча оставляем брак на обложке.
    if not FILTER_WB_PHOTOS_VISION and CHECK_COVER_PHOTO_VISION and photo_source == "wb" and photos:
        from services.ozon.repair import vision_check_violation
        from services.image.gemini import validate_product_image

        async def _cover_ok(ph: bytes) -> bool:
            try:
                if await vision_check_violation(ph):
                    return False
            except Exception:
                pass
            try:
                ok = await validate_product_image(ph, result.search_name, color=result.color)
                return ok is not False
            except Exception:
                return True

        cover_flags = await asyncio.gather(*(_cover_ok(ph) for ph in photos))
        pass_idx = next((i for i, ok in enumerate(cover_flags) if ok), None)
        if pass_idx is None:
            log.warning("Лицевое фото: ни один WB-ракурс не прошёл Vision — добираю чистое фото поиском")
            fallback = await find_product_images(
                result.search_name, n=1, brand=effective_brand,
                color=result.color, color_en=result.color_en, category=category,
                llm=llm, vision_validate=True,
            )
            if fallback:
                photos = fallback + photos
                log.info("Лицевое фото: найдено в интернете, поставлено первым")
        elif pass_idx:
            ph = photos[pass_idx]
            photos = [ph] + [p for j, p in enumerate(photos) if j != pass_idx]
            log.info(f"Лицевое фото: кандидат #{pass_idx+1} прошёл Vision, переставлен первым")

    r2_urls: list[str] = []
    for ph in photos:
        url = await upload_image(ph, ext="jpg")
        if url:
            r2_urls.append(url)
    log.info(f"  -> фото найдено {len(photos)}, на R2 залито {len(r2_urls)}")

    img_bytes = photos[0] if photos else None
    color_tag = f" ({result.color})" if result.color else ""
    if not img_bytes:
        await send_text(f"⚠️ {result.product}{color_tag}: фото не найдено — инфографики не будет.")
    elif photo_source != "wb" and result.color_en:
        # Поиск находит фото нужного цвета не всегда — при mismatch скоринг
        # (services/search/images.py) всё равно отдаёт лучшее ИЗ НАЙДЕННЫХ
        # (штраф ×0.05, не отказ), чтобы карточка не осталась без фото вовсе.
        # Раз так, явно предупреждаем пользователя — иначе расхождение
        # обнаружится только при ручной проверке готовой карточки.
        from services.image.background import remove_background
        from services.image.color_match import color_matches
        rgba = await run_gpu(remove_background, img_bytes, timeout=110, label="remove_background(color check)")
        if rgba is not None:
            match, detected, _ = color_matches(rgba, result.color_en)
            if not match:
                await send_text(
                    f"⚠️ {result.product}{color_tag}: нужного цвета фото не нашлось, "
                    f"взято похоже на «{detected or '?'}» — проверьте вручную."
                )

    # ── Характеристики для Ozon (реальные поля/значения шаблона) ───────
    ozon_chars_text = ""
    chars_prompt = filler.get_chars_prompt()
    if chars_prompt:
        ozon_resp = await llm.chat(result.context, chars_prompt)
        ozon_chars_text = ozon_resp.text.strip()
        await save_cost(user_id, "batch_card", response=ozon_resp)
    ozon_chars = _parse_chars(ozon_chars_text)
    # Гарантия — всегда фиксированные 12 месяцев (правило магазина, 14.07.2026),
    # что бы ни написала LLM из веб-источников.
    for _k in list(ozon_chars):
        if "гарант" in _k.lower():
            ozon_chars.pop(_k)
    ozon_chars["Гарантия"] = "12 месяцев"
    # Страна-изготовитель: НЕ дефолтим (у Nike — Вьетнам, у ASUS — Тайвань,
    # слепой «Китай» — подмена данных). Промпт требует заполнять базовые поля
    # из знаний о бренде; если LLM всё же промолчала — явно предупреждаем.
    if not ozon_chars.get("Страна-изготовитель"):
        log.warning(f"  -> Страна-изготовитель не определена для {result.product}")
        await send_text(
            f"⚠️ {result.product}: страна-изготовитель не определена — "
            f"проверьте и заполните в карточке вручную."
        )

    # ── Хэштеги Ozon (attr 23171) ────────────────────────────────────────
    hashtags = ""
    if GENERATE_HASHTAGS:
        try:
            hash_prompt = HASHTAG_PROMPT.format(
                product_name=result.product,
                category=profile.category,
                key_specs=ozon_chars_text[:600] if ozon_chars_text else result.description[:300],
            )
            hash_resp = await llm.chat("", hash_prompt)
            import re as _re
            _banned = {"бюджетный","дешёвый","дешевый","недорогой","эконом","дорогой","премиум","топовый","лучш"}
            raw_tags = hash_resp.text.strip()
            tags = _re.findall(r'#\S+', raw_tags)
            clean_tags = []
            for t in tags:
                c = _re.sub(r'[^\w]', '', t[1:]).lower()
                if c and not any(w in c for w in _banned):
                    clean_tags.append(f"#{c[:29]}")
            hashtags = " ".join(clean_tags)[:500]
            await save_cost(user_id, "batch_hashtags", response=hash_resp)
            log.info(f"  -> хэштеги: {hashtags[:80]}")
        except Exception as _he:
            log.warning(f"hashtags generation failed: {_he}")

    # ── Упаковка — реальные габариты с ВБ точнее оценки LLM (сверено на
    #    практике: LLM дал 8×0.8×11.5см при реальных 15×8×21см с ВБ) ──────
    if wb_dims:
        pack = {
            "Ширина упаковки (см)": str(wb_dims["width_cm"]),
            "Высота упаковки (см)": str(wb_dims["height_cm"]),
            "Длина упаковки (см)": str(wb_dims["length_cm"]),
            "Вес с упаковкой (кг)": str(wb_dims["weight_kg"]),
        }
    else:
        pack_text, pack_resp = await generate_packaging(result.context, llm, product=result.product,
                                                        category=result.category)
        await save_cost(user_id, "batch_pack", response=pack_resp)
        pack = _parse_chars(pack_text)

    def _pack_num(key: str) -> float | None:
        raw_val = pack.get(key)
        if not raw_val:
            return None
        try:
            return float(str(raw_val).replace(",", "."))
        except ValueError:
            return None

    weight_g = round(_pack_num("Вес с упаковкой (кг)") * 1000) if _pack_num("Вес с упаковкой (кг)") else None
    width_mm = round(_pack_num("Ширина упаковки (см)") * 10) if _pack_num("Ширина упаковки (см)") else None
    height_mm = round(_pack_num("Высота упаковки (см)") * 10) if _pack_num("Высота упаковки (см)") else None
    depth_mm = round(_pack_num("Длина упаковки (см)") * 10) if _pack_num("Длина упаковки (см)") else None

    # Лимит магазина (14.07.2026): товары тяжелее 26 кг в упаковке не создаём.
    if weight_g and weight_g > 26_000:
        _kg = weight_g / 1000
        await send_text(f"⛔ {result.product}: вес в упаковке {_kg:.1f} кг > 26 кг — карточку не создаём (лимит магазина).")
        raise ValueError(f"вес в упаковке {_kg:.1f} кг превышает лимит 26 кг")

    # ── Инфографика + rich content (с учётом стиля пользователя —
    #    gaming/default) — генерируем ДО Excel/API, чтобы залить готовые
    #    картинки на R2 и приложить к карточке Ozon ──────────────────────
    infographic = None
    slide2 = None
    richcontent = None
    if img_bytes and GENERATE_INFOGRAPHIC:
        user_style = await _get_user_style(user_id)
        features, slogan, tips, rich_slogan, vis_resps = await _extract_visuals(
            result.context, result.product, category, llm,
            gaming=(user_style == "gaming"),
        )
        for _vr in vis_resps:
            await save_cost(user_id, "batch_visuals", response=_vr)
        if rich_slogan:
            tips.append((rich_slogan, ""))

        gaming_accent = None
        bg_palette = None
        if category == "Сетевое оборудование":
            gaming_accent = await pick_gaming_accent(
                result.product, features, llm, color_en=result.color_en, category=category,
            )
            infographic, bg_warning = await make_signal_infographic(
                result.product, features, img_bytes, llm=llm,
                brand=result.brand, category=category, accent=gaming_accent,
                raw_specs=raw,
            )
        elif category == "Игровые кресла":
            gaming_accent = await pick_gaming_accent(
                result.product, features, llm, color_en=result.color_en, category=category,
            )
            infographic, bg_warning = await make_chair_infographic(
                result.product, features, img_bytes, llm=llm,
                brand=result.brand, category=category, accent=gaming_accent,
                raw_specs=raw,
            )
        elif category == "Акустика":
            gaming_accent = await pick_gaming_accent(
                result.product, features, llm, color_en=result.color_en, category=category,
            )
            infographic, bg_warning = await make_speakers_infographic(
                result.product, features, img_bytes, llm=llm,
                brand=result.brand, category=category, accent=gaming_accent,
                raw_specs=raw,
            )
        elif user_style == "gaming":
            gaming_accent = await pick_gaming_accent(
                result.product, features, llm, color_en=result.color_en, category=category,
            )
            infographic, bg_warning = await make_gaming_infographic(
                result.product, features, img_bytes, llm=llm,
                brand=result.brand, category=category, accent=gaming_accent,
            )
        elif user_style == "simple":
            gaming_accent = await pick_gaming_accent(
                result.product, features, llm, color_en=result.color_en, category=category,
            )
            infographic, bg_warning = await make_simple_infographic(
                result.product, features, tips, img_bytes, llm=llm,
                brand=result.brand, category=category, accent=gaming_accent,
            )
        else:
            # Палитра выбирается заранее — второй слайд (если будет) получит
            # фон в той же гамме, что и первый.
            bg_palette = await pick_bg_palette(result.product, features, llm)
            infographic, bg_warning = await make_infographic(
                result.product, features, img_bytes, llm=llm,
                brand=result.brand, slogan=slogan, palette=bg_palette,
                category=category,
            )
        if infographic:
            caption = f"Инфографика: {result.product}{color_tag}"
            if bg_warning:
                caption += f"\n⚠️ {bg_warning}. Белый фон."
            await send_photo(infographic, caption)

        # ── Второй слайд (пока только Мониторы): фото второго ракурса +
        #    одна главная характеристика + слоган из рич-контента.
        #    photos[1] уже скачан (N_PHOTOS=4) — доп. расход только rembg ──
        if (category in SLIDE2_CATEGORIES and len(photos) > 1 and features
                and gaming_accent is None):
            slide2 = await make_second_slide(
                result.product, features[0], rich_slogan or slogan, photos[1],
                palette=bg_palette,
            )
            if slide2:
                await send_photo(slide2, f"Слайд 2: {result.product}{color_tag}")

        # extra_photos=[] — миниатюры доп. ракурсов в rich content отключены
        # (см. handlers/image.py): compose_left_zone часто давал кривой
        # результат (повтор ракурса/неудачный кроп).
        richcontent = await make_richcontent(
            result.product, features, tips, img_bytes,
            extra_photos=[], llm=llm, gaming_accent=gaming_accent,
            category=category,
        )
        if richcontent:
            await send_photo(richcontent, f"Rich content: {result.product}{color_tag}")

    # ── Заливаем инфографику на R2 и кладём ПЕРВОЙ (главное/лицевое фото
    #    карточки), исходные фото товара — следом. Rich content в галерею
    #    фото больше НЕ идёт — он грузится отдельно в настоящий блок Ozon
    #    через атрибут "Rich-контент JSON" (см. ниже) ─────────────────────
    if infographic:
        url = await upload_image(infographic, ext="jpg")
        if url:
            r2_urls.insert(0, url)
    if slide2:
        s2_url = await upload_image(slide2, ext="jpg")
        if s2_url:
            # Сразу после инфографики (или первым, если её не было)
            r2_urls.insert(1 if infographic else 0, s2_url)
    rich_content_json: str | None = None
    if richcontent:
        rich_url = await upload_image(richcontent, ext="jpg")
        if rich_url:
            rich_content_json = build_image_rich_content(rich_url)
    log.info(f"  -> R2 итого (инфографика+фото): {len(r2_urls)}, rich content json: {bool(rich_content_json)}")

    # ТН ВЭД — приоритет: 1) WB-карточка-аналог, если продавец его там
    # указал (заполняется редко, но точнее всего); 2) база tnved_codes
    # (правки по факту находок); 3) фиксированный код CategoryProfile —
    # страховочный фолбэк, если ни того ни другого нет.
    from services.tnved import get_tnved
    db_tnved = await get_tnved(profile.key)
    effective_tnved = (wb_data.get("tnved") if wb_data else "") or db_tnved or profile.tnved

    # ── Excel (архив/ручная проверка) ───────────────────────────────────
    filler.add_row(
        article=article, name=ozon_name, brand=result.brand, model=result.model,
        fixed_type=profile.fixed_type, color=result.color, tnved=effective_tnved,
        description=result.description, photo_url=(r2_urls[0] if r2_urls else ""),
        chars_text=ozon_chars_text, pack=pack, price=price,
    )

    # ── Живой Ozon API: атрибуты + создание товара ──────────────────────
    api_values = {
        "Бренд": result.brand,
        "Тип": profile.fixed_type,
        "Название модели (для объединения в одну карточку)": result.model,
        "Аннотация": result.description,
        "ТН ВЭД коды ЕАЭС": effective_tnved,
        "Нужен код маркировки": "false",
        **ozon_chars,
    }
    # hashtags идут отдельно через update_product_attributes после импорта (reimport игнорирует этот атрибут)
    if result.color:
        # "Цвет товара" исключён из LLM-промпта характеристик (заполняется
        # отдельно, см. _SKIP_FOR_LLM в utils/ozon_filler.py) и до сих пор
        # уходил только в Excel-архив (filler.add_row) — в живой вызов API
        # не попадал вовсе.
        api_values["Цвет товара"] = _translate_color(result.color)
    if rich_content_json:
        api_values["Rich-контент JSON"] = rich_content_json
    attributes = await build_attributes_payload(
        attrs_meta, api_values, profile.description_category_id, profile.type_id,
    )

    item = {
        "description_category_id": profile.description_category_id,
        "type_id": profile.type_id,
        "offer_id": article,
        "name": ozon_name,
        "currency_code": "KZT",
        "vat": "0.16",
        "attributes": attributes,
        "images": r2_urls,
    }
    if price:
        item["price"] = str(price)
    if weight_g:
        item["weight"] = weight_g
        item["weight_unit"] = "g"
    if width_mm and height_mm and depth_mm:
        item["dimension_unit"] = "mm"
        item["width"] = width_mm
        item["height"] = height_mm
        item["depth"] = depth_mm

    # Статусы, которые считаем УСПЕХОМ. "skipped" — не провал: так Ozon
    # отвечает на повторную отправку, если предыдущая ещё обрабатывается
    # (дедуп на их стороне). Проверено вживую на реальных offer_id (10.07) —
    # карточки с итоговым skipped есть в кабинете без ошибок, с ценой.
    _OK_STATUSES = {"imported", "skipped"}

    try:
        task_id = await import_product(item)
        await asyncio.sleep(5)
        info = await get_import_info(task_id)
        items_status = info.get("items", [])
        errors = [e for it in items_status for e in it.get("errors", [])]

        # Ozon-импорт асинхронный — за 5с не всегда успевает обработаться
        # (особенно под нагрузкой при быстром темпе батча). Раньше что бы ни
        # пришло на первой проверке — считали готово и ехали дальше, из-за
        # чего часть карточек зависала в лимбо (помогало только вручную
        # открыть карточку и нажать "отправить" ещё раз). Теперь: не
        # "imported"/есть ошибки -> ждём ещё, и если не помогло -
        # переотправляем (тот же item, тот же offer_id — Ozon делает upsert,
        # это ровно то же самое действие, что ручной повторный сабмит).
        # На УЖЕ успешных карточках (подавляющее большинство) это не
        # добавляет ни секунды — доп. проверки идут только при проблеме.
        # Пауза перед решением о ретрае увеличена с 10с до 25с (10.07) —
        # короткого окна часто не хватало, и ретрай улетал в уже обрабатывающийся
        # первый импорт, откуда прилетал skipped, который раньше считался
        # ошибкой — ретрай был не нужен в большинстве таких случаев.
        retried = False
        if not items_status or errors or any(it.get("status") not in _OK_STATUSES for it in items_status):
            await asyncio.sleep(25)
            info = await get_import_info(task_id)
            items_status = info.get("items", [])
            errors = [e for it in items_status for e in it.get("errors", [])]

            if not items_status or errors or any(it.get("status") not in _OK_STATUSES for it in items_status):
                log.warning(f"  -> карточка {article!r} не завершилась за 30с — переотправляю")
                retried = True
                task_id = await import_product(item)
                await asyncio.sleep(10)
                info = await get_import_info(task_id)
                items_status = info.get("items", [])
                errors = [e for it in items_status for e in it.get("errors", [])]

        status_line = ", ".join(f"{it.get('offer_id')}: {it.get('status')}" for it in items_status)
        msg = f"Ozon API: task_id={task_id}\n{status_line}"
        if retried:
            msg += "\n♻️ понадобилась переотправка"
        if errors:
            msg += "\n⚠️ Ошибки: " + "; ".join(str(e) for e in errors)
        if not items_status or errors or any(it.get("status") not in _OK_STATUSES for it in items_status):
            msg += "\n🔴 Всё ещё не завершилось — проверить вручную или через /ozon_fix"
            log.warning(f"  -> {article!r} осталась незавершённой после переотправки")
        await send_text(msg)
        log.info(f"  -> {msg}")

        # Хэштеги через отдельный endpoint (reimport их игнорирует)
        if hashtags:
            try:
                await _update_attrs(article, [{"id": 23171, "complex_id": 0, "values": [{"value": hashtags}]}])
                log.info(f"  -> хэштеги обновлены: {hashtags[:60]}")
            except Exception as _he:
                log.warning(f"  -> хэштеги не обновились: {_he}")
    except Exception as e:
        log.error(f"  -> Ozon API ОШИБКА: {e}", exc_info=True)
        await send_text(f"🔴 Ozon API ошибка для {result.product}: {e}")

    log.info(f"  -> готово: {result.product}{color_tag}")
