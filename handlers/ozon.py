"""
Команда /ozon — батч создания карточек Ozon для категории (см.
services/ozon_categories.PROFILES): поиск, фото+R2, характеристики,
упаковка, инфографика/rich content, заполнение Excel и живой вызов
Ozon Seller API (товар реально создаётся в кабинете).

Формат: /ozon <категория>, затем список — одна позиция на строку,
артикул и название через таб или пробел: "АРТИКУЛ  Название со спеками".
"""
import asyncio
import logging

from aiogram import Router
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.filters import Command
from aiogram.types import Message, BufferedInputFile

from services.llm import get_llm
from services.ozon_categories import PROFILES, get_profile, detect_speaker_profile, SPEAKER_PROFILES
from services.ozon_pipeline import load_filler_and_attrs, process_ozon_product
from services.ozon.client import get_all_products_list
from services.ozon.repair import scan_issues, repair_card
from handlers.tasks import run_task
from utils.helpers import send_photo
from utils.billing import save_cost

log = logging.getLogger(__name__)
router = Router()


def _split_ozon_line(raw: str) -> tuple[str, str]:
    """Разделяет строку на артикул и название.
    Приоритет: таб → 2+ пробела → первое слово без кириллицы → фолбэк."""
    import re as _re
    if "\t" in raw:
        article, _, name = raw.partition("\t")
        return article.strip(), name.strip()
    parts = _re.split(r" {2,}", raw, maxsplit=1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    # Фолбэк: первый «токен» без кириллицы — артикул (цифры, латиница, дефис)
    m = _re.match(r'^([A-Za-z0-9_/\-*\.]+)\s+(.+)$', raw.strip())
    if m:
        return m.group(1), m.group(2)
    return "", raw.strip()


async def _run_ozon_batch(message: Message, profile_key: str, lines: list[str]):
    import re as _re
    user_id = message.from_user.id
    auto_speakers = (profile_key == "speakers")
    llm = await get_llm(user_id)
    total = len(lines)

    # Для авто-режима грузим все под-профили акустики заранее
    if auto_speakers:
        progress = await message.answer(f"Ozon-батч «Акустика (авто)»: {total} товаров. Загружаю шаблоны...")
        sub_cache: dict[str, tuple] = {}
        for key in SPEAKER_PROFILES:
            try:
                p = get_profile(key)
                sub_cache[key] = (p, *await load_filler_and_attrs(p))
            except Exception as e:
                log.warning(f"speakers: не удалось загрузить профиль {key!r}: {e}")
        if not sub_cache:
            await progress.edit_text("🔴 Ни один профиль акустики не загрузился.")
            return
        await progress.edit_text(f"Ozon-батч «Акустика (авто)»: {total} товаров. Начинаю...")
        profile = filler = attrs_meta = None  # используются из sub_cache
    else:
        profile = get_profile(profile_key)
        progress = await message.answer(f"Ozon-батч «{profile.category}»: {total} товаров (живая загрузка). Начинаю...")
        try:
            filler, attrs_meta = await load_filler_and_attrs(profile)
        except Exception as e:
            await progress.edit_text(f"🔴 Не удалось загрузить шаблон/атрибуты Ozon: {e}")
            return

    async def send_text(text: str):
        for attempt in range(3):
            try:
                await message.answer(text)
                return
            except TelegramBadRequest:
                # Описание товара может содержать «<1 мс», «<0.5%» и т.п. —
                # дефолтный parse_mode=HTML ломается на «<цифра» как на теге.
                # Повторяем без разметки, чтобы не ронять весь товар из-за чата.
                await message.answer(text, parse_mode=None)
                return
            except TelegramNetworkError:
                # Кратковременный обрыв сети (16:08 07.07 убил целый батч):
                # ждём и повторяем, а не роняем товар.
                if attempt == 2:
                    raise
                await asyncio.sleep(20 * (attempt + 1))

    async def send_photo_(data: bytes, caption: str):
        await send_photo(message, data, caption)

    failed = 0
    brackets_cache: tuple | None = None   # лениво: профиль brackets для авто-маршрута
    brackets_tried = False
    for i, raw_line in enumerate(lines, 1):
        article, name = _split_ozon_line(raw_line)
        if not name:
            continue
        if not article:
            await message.answer(f"⚠️ [{i}/{total}] нет артикула в строке «{raw_line}» — пропуск.")
            failed += 1
            continue

        if auto_speakers:
            detected_key = detect_speaker_profile(name)
            if detected_key not in sub_cache:
                detected_key = next(iter(sub_cache))  # фолбэк на первый доступный
            cur_profile, cur_filler, cur_attrs = sub_cache[detected_key]
            log.info(f"speakers авто: «{name}» → {detected_key!r} ({cur_profile.fixed_type})")
        else:
            cur_profile, cur_filler, cur_attrs = profile, filler, attrs_meta

        # В списках мониторов регулярно попадаются кронштейны/крепления
        # (NB F5 07.07, NB A5C 08.07) — карточка уезжала в категорию «Монитор»
        # (сменить категорию потом нельзя — только удалять). Автомаршрут
        # на профиль brackets, по образцу авто-акустики.
        if (profile_key == "monitors"
                and _re.search(r"кроншт|креплен", name.lower())):
            if brackets_cache is None and not brackets_tried:
                brackets_tried = True
                try:
                    bp = get_profile("brackets")
                    brackets_cache = (bp, *await load_filler_and_attrs(bp))
                except Exception as e:
                    log.warning(f"brackets авто: профиль не загрузился: {e}")
            if brackets_cache:
                cur_profile, cur_filler, cur_attrs = brackets_cache
                await send_text(f"↪️ [{i}/{total}] «{name[:60]}» похоже на кронштейн — "
                                f"создаю в категории «{cur_profile.fixed_type}»")
                log.info(f"brackets авто: «{name}» → профиль brackets")

        try:
            await send_text(f"[{i}/{total}] {name}")
            await process_ozon_product(
                cur_profile, name, article, cur_filler, cur_attrs, llm, user_id,
                photo_source="wb",
                send_text=send_text, send_photo=send_photo_,
            )
        except Exception as e:
            log.error(f"[{i}/{total}] ОШИБКА на товаре '{name}': {e}", exc_info=True)
            failed += 1
            try:
                await message.answer(f"🔴 [{i}/{total}] Ошибка: {name}\n{e}")
            except Exception as notify_err:
                # Сообщение об ошибке не должно ронять весь батч: при обрыве
                # сети (16:08 07.07 — TelegramNetworkError поверх ошибки товара)
                # погиб целый прогон. Лог есть — едем дальше.
                log.warning(f"[{i}/{total}] не удалось отправить сообщение об ошибке: {notify_err}")

    # Сохраняем Excel — для авто-режима отдаём все под-профили у которых есть строки
    if auto_speakers:
        sent_any = False
        for key, (p, f, _) in sub_cache.items():
            if f.save(p.out):
                doc = BufferedInputFile(p.out.read_bytes(), filename=p.out.name)
                await message.answer_document(doc, caption=f"{p.fixed_type}: файл")
                sent_any = True
        await message.answer(f"Ozon-батч «Акустика (авто)» готов: {total - failed}/{total}.")
    else:
        if filler.save(profile.out):
            doc = BufferedInputFile(profile.out.read_bytes(), filename=profile.out.name)
            await message.answer_document(doc, caption=f"Ozon-батч готов: {total - failed}/{total}.")
        else:
            await message.answer(f"Ozon-батч готов: {total - failed}/{total}.\n⚠️ Excel не сохранён.")


@router.message(Command("ozon"))
async def cmd_ozon(message: Message):
    text = message.text or ""
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    first_line_parts = lines[0].split(None, 1) if lines else []
    profile_key = first_line_parts[1].strip() if len(first_line_parts) > 1 else ""
    raw_lines = lines[1:]

    if not profile_key or (profile_key not in PROFILES and profile_key != "speakers") or not raw_lines:
        keys = ", ".join(list(PROFILES) + ["speakers"])
        await message.answer(
            f"Формат: /ozon категория, затем список товаров — каждый с "
            f"новой строки, артикул и название через таб или пробел.\n"
            f"Доступные категории: {keys}\n\n"
            "/ozon ssd\n"
            "GP-GSTFS31480GNTD\tSSD GIGABYTE GP-GSTFS31480GNTD, 480GB, SATA 6Gb/s, 2.5\"\n"
            "NT01N930E-256G-E4X\tSSD Netac N930E Pro, 256GB, M.2 NVMe PCIe 3.0\n\n"
            "Карточки создаются напрямую в твоём кабинете Ozon (живой API).\n"
            "/cancel — остановить"
        )
        return

    run_task(message.from_user.id, _run_ozon_batch(message, profile_key, raw_lines))


_OZON_FIX_BATCH = 30  # карточек за один запуск /ozon_fix

# description_category_id=17028908 — вся группа «Акустика и колонки» на Ozon
_ACOUSTIC_CAT_ID = 17028908
_SPEAKER_TYPE_IDS = {
    95305: "soundbar",
    95315: "hifi",
    95318: "pc_speakers",
    95320: "acoustic",
}


async def _run_ozon_recat_speakers(message: Message):
    from services.ozon.client import get_products_attributes, import_product, get_import_info

    progress = await message.answer("Recat speakers: получаю все товары Ozon...")
    try:
        all_ids = await get_all_products_list()
    except Exception as e:
        await progress.edit_text(f"🔴 Не удалось получить список товаров: {e}")
        return

    # Получаем атрибуты чанками по 100
    acoustic_items: dict[str, dict] = {}
    chunk = 100
    for i in range(0, len(all_ids), chunk):
        batch_ids = all_ids[i:i + chunk]
        try:
            batch = await get_products_attributes(batch_ids)
            for oid, item in batch.items():
                if item.get("description_category_id") == _ACOUSTIC_CAT_ID:
                    acoustic_items[oid] = item
        except Exception as e:
            log.warning(f"recat: ошибка получения атрибутов чанка {i}: {e}")

    await progress.edit_text(
        f"Recat speakers: найдено {len(acoustic_items)} акустических товаров из {len(all_ids)}. Проверяю типы..."
    )

    to_fix: list[tuple[str, dict, str]] = []  # (offer_id, attrs, new_profile_key)
    for oid, attrs in acoustic_items.items():
        current_type_id = attrs.get("type_id")
        name = attrs.get("name", oid)
        correct_key = detect_speaker_profile(name)
        correct_profile = get_profile(correct_key)
        if current_type_id != correct_profile.type_id:
            to_fix.append((oid, attrs, correct_key))

    if not to_fix:
        await progress.edit_text(
            f"✅ Всё верно — у всех {len(acoustic_items)} акустических товаров правильный тип."
        )
        return

    lines = [f"• {oid}: {_SPEAKER_TYPE_IDS.get(attrs.get('type_id'), '?')} → {get_profile(key).fixed_type}"
             for oid, attrs, key in to_fix[:20]]
    preview = "\n".join(lines) + (f"\n...и ещё {len(to_fix)-20}" if len(to_fix) > 20 else "")
    await message.answer(f"Нужно исправить {len(to_fix)} карточек:\n{preview}\n\nИсправляю...")

    fixed = 0
    errors = 0
    for oid, attrs, new_key in to_fix:
        new_profile = get_profile(new_key)
        item = {
            "offer_id": oid,
            "name": attrs["name"],
            "description_category_id": _ACOUSTIC_CAT_ID,
            "type_id": new_profile.type_id,
            "images": attrs.get("images", []),
            "attributes": [
                {"id": a["id"], "complex_id": a.get("complex_id", 0), "values": a["values"]}
                for a in attrs.get("attributes", [])
            ],
            "currency_code": attrs.get("currency_code", "KZT"),
            "vat": attrs.get("vat", "0.16"),
        }
        for dim in ("height", "width", "depth", "weight"):
            if attrs.get(dim):
                item[dim] = attrs[dim]
        if attrs.get("height") and attrs.get("width") and attrs.get("depth"):
            item["dimension_unit"] = attrs.get("dimension_unit", "mm")
        if attrs.get("weight"):
            item["weight_unit"] = attrs.get("weight_unit", "g")
        if attrs.get("barcode"):
            item["barcode"] = attrs["barcode"]
        try:
            task_id = await import_product(item)
            import asyncio as _aio
            await _aio.sleep(3)
            info = await get_import_info(task_id)
            statuses = [it.get("status") for it in info.get("items", [])]
            if any(s == "imported" for s in statuses):
                fixed += 1
                log.info(f"recat: {oid} → {new_profile.fixed_type} ✅")
            else:
                errors += 1
                log.warning(f"recat: {oid} статус {statuses}")
        except Exception as e:
            errors += 1
            log.error(f"recat: {oid} ошибка: {e}")

    await message.answer(
        f"✅ Recat speakers завершён: исправлено {fixed}, ошибок {errors} из {len(to_fix)}."
    )


async def _run_ozon_fix(message: Message):
    user_id = message.from_user.id
    llm = await get_llm(user_id)

    progress = await message.answer("Ozon Fix: получаю список всех товаров...")
    try:
        all_ids = await get_all_products_list()
    except Exception as e:
        await progress.edit_text(f"🔴 Не удалось получить список товаров Ozon: {e}")
        return

    if not all_ids:
        await progress.edit_text("Ozon Fix: в кабинете нет товаров.")
        return

    await progress.edit_text(f"Ozon Fix: {len(all_ids)} товаров, проверяю ошибки и доработки...")
    try:
        issues_map = await scan_issues(all_ids)
    except Exception as e:
        await progress.edit_text(f"🔴 Ошибка при сканировании: {e}")
        return

    if not issues_map:
        await progress.edit_text(
            f"✅ Ozon Fix: замечаний нет ({len(all_ids)} карточек проверено)."
        )
        return

    total_issues = len(issues_map)
    batch = dict(list(issues_map.items())[:_OZON_FIX_BATCH])
    skipped = total_issues - len(batch)

    def _issue_label(v: dict) -> str:
        parts = []
        if v["errors"]:
            codes = ", ".join(str(e.get("code", "?")) for e in v["errors"])
            parts.append(f"❌ {codes}")
        if v["warnings"]:
            codes = ", ".join(str(w.get("code", "?")) for w in v["warnings"])
            parts.append(f"⚠️ {codes}")
        return " | ".join(parts)

    issue_list = "\n".join(
        f"• {oid}: {_issue_label(v)}" for oid, v in batch.items()
    )
    skip_note = f"\n\n⏭ Ещё {skipped} — следующий /ozon_fix." if skipped else ""
    await progress.edit_text(
        f"Ozon Fix: всего замечаний {total_issues} (❌ошибки + ⚠️доработки), "
        f"исправляю первые {len(batch)}:\n{issue_list}{skip_note}"
    )

    fixed = 0
    failed = 0
    total = len(batch)
    for i, (offer_id, v) in enumerate(batch.items(), 1):
        label = _issue_label(v)
        await message.answer(f"[{i}/{total}] {offer_id} | {label}")
        try:
            ok = await repair_card(
                offer_id, llm, user_id,
                errors=v["errors"], warnings=v["warnings"],
                send_text=message.answer,
            )
            if ok:
                fixed += 1
            else:
                failed += 1
        except Exception as e:
            log.error(f"repair_card {offer_id}: {e}", exc_info=True)
            await message.answer(f"🔴 [{i}/{total}] {offer_id}: {e}")
            failed += 1

    finish = f"Ozon Fix завершён: ✅ {fixed}, 🔴 {failed} из {total}."
    if skipped:
        finish += f"\nОсталось ещё {skipped} — /ozon_fix для следующей партии."
    await message.answer(finish)


@router.message(Command("ozon_fix"))
async def cmd_ozon_fix(message: Message):
    """Сканирует все товары в кабинете Ozon, находит карточки с ошибками
    (модерация, валидация атрибутов) и автоматически их исправляет."""
    run_task(message.from_user.id, _run_ozon_fix(message))


@router.message(Command("ozon_recat"))
async def cmd_ozon_recat(message: Message):
    """Исправляет категорию (тип) акустических карточек в Ozon:
    определяет правильный тип по названию и перезаливает карточки с неверным типом."""
    run_task(message.from_user.id, _run_ozon_recat_speakers(message))


_BANNED_HASHTAG_WORDS = {"бюджетный", "дешёвый", "дешевый", "недорогой", "эконом", "дорогой", "премиум", "топовый", "лучш"}

def _normalize_hashtags(raw: str) -> str:
    """Приводит хэштеги к формату Ozon: строчные, #слово_слово, через пробел."""
    import re as _re
    tags = _re.findall(r'#\S+', raw)
    result = []
    for tag in tags:
        clean = _re.sub(r'[^\w]', '', tag[1:]).lower()
        if not clean:
            continue
        if any(w in clean for w in _BANNED_HASHTAG_WORDS):
            continue
        result.append(f"#{clean[:29]}")
    return " ".join(result[:30])


_HASHTAG_ATTR_ID = 23171
_RICHCONTENT_ATTR_ID = 11254
_CATEGORY_NAMES = {
    17028908: "Акустика и колонки",
    17028626: "SSD-накопители",
    17028646: "Клавиатуры и мыши",
    17028929: "Наушники",
    17028914: "Камеры видеонаблюдения",
    17027994: "Блоки питания",
    17028660: "Мониторы",
    17028648: "Мыши",
    17028619: "Компьютеры",
}


async def _run_ozon_hashtags(message: Message):
    from services.ozon.client import get_products_attributes, update_product_attributes
    from prompts import HASHTAG_PROMPT

    user_id = message.from_user.id
    llm = await get_llm(user_id)

    progress = await message.answer("Хэштеги Ozon: получаю товары «в продаже»...")
    all_ids = await get_all_products_list(visibility="VISIBLE", with_stock=True)
    await progress.edit_text(f"Хэштеги Ozon: {len(all_ids)} товаров в продаже. Ищу без хэштегов...")

    # Получаем атрибуты батчами и фильтруем без хэштега
    no_hashtag: list[str] = []
    attrs_map = {}
    for i in range(0, len(all_ids), 100):
        batch = all_ids[i:i + 100]
        try:
            attrs = await get_products_attributes(batch)
            attrs_map.update(attrs)
            for oid, item in attrs.items():
                has_tag = any(
                    a["id"] == _HASHTAG_ATTR_ID and a.get("values") and a["values"][0].get("value", "").strip()
                    for a in item.get("attributes", [])
                )
                if not has_tag:
                    no_hashtag.append(oid)
        except Exception as e:
            log.warning(f"hashtags: ошибка атрибутов чанка {i}: {e}")

    if not no_hashtag:
        await progress.edit_text("✅ У всех товаров «в продаже» уже есть хэштеги.")
        return

    await progress.edit_text(
        f"Хэштеги Ozon: без хэштегов {len(no_hashtag)} из {len(all_ids)}. Генерирую..."
    )

    total = len(no_hashtag)
    ok = fail = 0
    for i, oid in enumerate(no_hashtag, 1):
        raw = attrs_map.get(oid)
        if not raw:
            fail += 1
            continue

        name = raw.get("name", oid)
        cat_id = raw.get("description_category_id")
        category = _CATEGORY_NAMES.get(cat_id, "Электроника")

        existing_attrs = raw.get("attributes", [])
        specs_vals = [
            (a["values"][0].get("value") or "")
            for a in existing_attrs
            if a.get("values") and a["id"] not in (_RICHCONTENT_ATTR_ID, _HASHTAG_ATTR_ID, 23536)
        ]
        key_specs = ", ".join(v for v in specs_vals[:8] if v)[:400] or name

        try:
            prompt = HASHTAG_PROMPT.format(
                product_name=name, category=category, key_specs=key_specs,
            )
            resp = await llm.chat("", prompt)
            await save_cost(user_id, "hashtags_bulk", response=resp)
            hashtags = _normalize_hashtags(resp.text.strip())
        except Exception as e:
            log.warning(f"hashtags LLM {oid}: {e}")
            fail += 1
            continue

        if not hashtags:
            fail += 1
            continue

        try:
            # Ozon attr 23171: одно string-значение, все теги через пробел
            await update_product_attributes(oid, [
                {"id": _HASHTAG_ATTR_ID, "complex_id": 0, "values": [{"value": hashtags}]}
            ])
            ok += 1
            log.info(f"hashtags: {oid} ✅ {hashtags[:60]}")
        except Exception as e:
            fail += 1
            log.error(f"hashtags {oid}: {e}")

        if i % 20 == 0:
            await message.answer(f"Хэштеги: {i}/{total} ({ok} ок, {fail} ошибок)...")

    await message.answer(f"✅ Хэштеги Ozon завершено: {ok} добавлено, {fail} ошибок из {total}.")


@router.message(Command("ozon_hashtags"))
async def cmd_ozon_hashtags(message: Message):
    """Добавляет хэштеги (10 шт.) всем товарам «в продаже» у кого их нет."""
    run_task(message.from_user.id, _run_ozon_hashtags(message))
