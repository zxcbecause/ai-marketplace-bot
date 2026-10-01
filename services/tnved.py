"""
База ТН ВЭД кодов по категориям Ozon — таблица tnved_codes в bot.db.
Приоритет источника (см. services/ozon_pipeline.py):
1. Собственное поле "Код ТН ВЭД" на карточке WB-аналога (если продавец
   его заполнил — редко, но точнее всего).
2. Эта база (category_key -> код), обновляется по факту находок.
3. Фиксированный код из CategoryProfile.tnved (страховочный фолбэк).

Сидинг таблицы текущими значениями из ozon_categories.PROFILES происходит
один раз при первом обращении, если строки для профиля ещё нет — так что
существующие профили сразу доступны через get_tnved(), а новые правки
(set_tnved) не требуют трогать код.
"""
import logging
import re

from database.db import db_connect

log = logging.getLogger(__name__)

# Официальные/справочные базы кодов ТН ВЭД ЕАЭС — не обзоры и не форумы,
# у каждой есть выделенная страница на конкретный код с текстовым описанием
# позиции. Тот же набор источников, которым пользуется человек при ручном
# подборе (kodtnved.ru/ifcg.ru/classifikators.ru/tws.by/alta.ru).
_TNVED_CLASSIFIER_DOMAINS = ["kodtnved.ru", "ifcg.ru", "classifikators.ru", "tws.by", "alta.ru"]
# 10 цифр слитно — редко в самом тексте страницы (там код почти всегда
# печатают группами "8518 10 9600"), зато НАДЁЖНО есть в URL-пути этих
# классификаторов (.../tnved/8518109600/, .../code/8518109600/) — проверено
# живьём 10.08.2026, поиск по одному только слитному тексту не находил ничего
# на страницах, где код был буквально в заголовке и адресе.
_TNVED_CODE_RE = re.compile(r"\b\d{10}\b")
_TNVED_GROUPED_RE = re.compile(r"\b(\d{4})[\s-](\d{2})[\s-]?(\d{2})[\s-]?(\d{2})\b")
_TNVED_URL_CODE_RE = re.compile(r"/(\d{10})(?:[/?]|$)")


def _extract_tnved_codes(url: str, title: str, text: str) -> set[str]:
    """Коды, реально встречающиеся у страницы — слитно в тексте, группами
    через пробел/дефис (офиц. запись) в тексте/заголовке, или в самом URL."""
    codes: set[str] = set(_TNVED_CODE_RE.findall(text))
    codes |= {"".join(m) for m in _TNVED_GROUPED_RE.findall(text + " " + title)}
    codes |= set(_TNVED_URL_CODE_RE.findall(url))
    return codes


async def get_tnved(category_key: str) -> str | None:
    """Код ТН ВЭД для профиля категории из базы. None — записи нет вообще
    (вызывающий код сам решает, откатываться ли на CategoryProfile.tnved)."""
    async with db_connect() as db:
        cur = await db.execute(
            "SELECT tnved FROM tnved_codes WHERE category_key = ?", (category_key,)
        )
        row = await cur.fetchone()
        return row[0] if row else None


async def set_tnved(category_key: str, tnved: str, note: str = "") -> None:
    """Записывает/обновляет код для категории — правки по факту находок."""
    async with db_connect() as db:
        await db.execute(
            """INSERT INTO tnved_codes (category_key, tnved, note, updated_at)
               VALUES (?, ?, ?, datetime('now'))
               ON CONFLICT(category_key) DO UPDATE SET
                   tnved = excluded.tnved, note = excluded.note,
                   updated_at = excluded.updated_at""",
            (category_key, tnved, note),
        )
        await db.commit()
    log.info(f"tnved_codes: {category_key!r} -> {tnved!r} ({note})")


async def get_wb_tnved(subject_id: int) -> str | None:
    """Код ТН ВЭД для subjectID WB из своей библиотеки (фолбэк, когда
    официальный справочник content/v2/directory/tnved недоступен или пуст
    для этой категории). None — записи нет."""
    async with db_connect() as db:
        cur = await db.execute(
            "SELECT tnved FROM wb_tnved_codes WHERE subject_id = ?", (subject_id,)
        )
        row = await cur.fetchone()
        return row[0] if row else None


async def set_wb_tnved(subject_id: int, subject_name: str, tnved: str, note: str = "") -> None:
    """Записывает/обновляет код для subjectID WB — накапливается по факту
    каждого успешного резолва через официальный справочник."""
    async with db_connect() as db:
        await db.execute(
            """INSERT INTO wb_tnved_codes (subject_id, subject_name, tnved, note, updated_at)
               VALUES (?, ?, ?, ?, datetime('now'))
               ON CONFLICT(subject_id) DO UPDATE SET
                   subject_name = excluded.subject_name, tnved = excluded.tnved,
                   note = excluded.note, updated_at = excluded.updated_at""",
            (subject_id, subject_name, tnved, note),
        )
        await db.commit()
    log.info(f"wb_tnved_codes: {subject_id} ({subject_name!r}) -> {tnved!r} ({note})")


async def get_wb_tnved_candidates(subject_id: int) -> list[dict]:
    """Список кандидатов [{tnved, description}] для subjectID WB из своей
    библиотеки кандидатов. Пустой список — записей нет. В отличие от
    get_wb_tnved (один код на категорию), кандидаты позволяют пер-товарный
    выбор: внутри одной WB-категории коды часто различаются по материалу/
    техническим признакам (напр. рюкзаки 4202 91/92 по материалу поверхности,
    кроссовки — десятки кодов по материалу верха)."""
    async with db_connect() as db:
        cur = await db.execute(
            "SELECT tnved, description FROM wb_tnved_candidates "
            "WHERE subject_id = ? ORDER BY tnved",
            (subject_id,),
        )
        rows = await cur.fetchall()
        return [{"tnved": r[0], "description": r[1]} for r in rows]


async def set_wb_tnved_candidates(
    subject_id: int, subject_name: str,
    candidates: list[tuple[str, str]], source: str = "",
) -> None:
    """Сохраняет/обновляет кандидатов (код, официальное описание) для
    subjectID WB. Апсерт по (subject_id, tnved) — повторный сид той же
    категории обновляет описания, не плодит дублей."""
    async with db_connect() as db:
        for code, description in candidates:
            await db.execute(
                """INSERT INTO wb_tnved_candidates
                       (subject_id, tnved, description, subject_name, source, updated_at)
                   VALUES (?, ?, ?, ?, ?, datetime('now'))
                   ON CONFLICT(subject_id, tnved) DO UPDATE SET
                       description = excluded.description,
                       subject_name = excluded.subject_name,
                       source = excluded.source,
                       updated_at = excluded.updated_at""",
                (subject_id, code, description, subject_name, source),
            )
        await db.commit()
    log.info(f"wb_tnved_candidates: {subject_id} ({subject_name!r}) — "
             f"{len(candidates)} кандидатов ({source})")


async def search_tnved_candidates(subject_name: str) -> list[dict]:
    """14.08.2026: веб-поиск ВСЕХ заземлённых кандидатов кода для категории —
    в отличие от search_tnved_code (который выбирал одного «победителя» на
    всю категорию), возвращает каждый код, реально найденный на страницах
    классификаторов, вместе с текстом его страницы как официальным
    описанием. Выбор между кандидатами делает вызывающий код ПО КОНКРЕТНОМУ
    ТОВАРУ (его извлечённым характеристикам) — та же классификационная
    задача, что LLM-выбор из кандидатов официального справочника WB в
    wb_create.py, не генеративное угадывание (см. крах 11.07).

    Заземление: код обязан буквально встречаться в тексте/URL найденной
    страницы. Описание кандидата — заголовок+текст страницы, где код
    упоминается; предпочитается выделенная страница кода (код в URL).
    Возвращает [{tnved, description, domains}], пустой список — не нашлось."""
    from services.search.hybrid import product_search

    resp = await product_search.search_text(
        f"{subject_name} ТН ВЭД код", max_results=12, max_chars=3000,
        include_domains=_TNVED_CLASSIFIER_DOMAINS,
    )
    by_code: dict[str, dict] = {}
    for r in resp.results:
        codes = _extract_tnved_codes(r.url, r.title, r.text)
        if not codes:
            continue
        domain = r.url.split("/")[2] if r.url.startswith("http") else r.url
        for code in codes:
            entry = by_code.setdefault(
                code, {"tnved": code, "description": "", "domains": set(),
                       "_score": (False, 0)},
            )
            entry["domains"].add(domain)
            # Лучшее описание: выделенная страница кода (код в URL) важнее
            # страницы-списка, при равенстве — где больше связного текста.
            # Живой пример 14.08: alta.ru отдаёт в выдержке навигационную
            # шапку сайта, tws.by — настоящий текст позиции; порядок выдачи
            # не должен решать, какое описание останется.
            is_dedicated = bool(_TNVED_URL_CODE_RE.search(r.url)) and code in r.url
            desc = f"{r.title}\n{r.text[:800]}".strip()
            meaningful = sum(ch.isalpha() for ch in desc)
            if (is_dedicated, meaningful) > entry["_score"]:
                entry["description"] = desc
                entry["_score"] = (is_dedicated, meaningful)
    out = []
    for entry in by_code.values():
        entry["domains"] = sorted(entry["domains"])
        entry.pop("_score")
        out.append(entry)
    out.sort(key=lambda e: e["tnved"])
    return out


async def search_tnved_code(subject_name: str, llm) -> tuple[str, str] | None:
    """10.08.2026: живой веб-поиск кода ТН ВЭД по официальным классификаторам —
    заземлён на реально найденных страницах (код должен буквально встречаться
    в тексте/URL найденных страниц, не только в ответе модели) и требует
    согласия ≥2 независимых доменов. НЕ применяется и не сохраняется
    автоматически (см. использование в create_one) — только ПРЕДЛАГАЕТСЯ в
    чат как подсказка для ручного подтверждения. Живое тестирование 10.08.2026
    показало, что для широких/многозначных категорий (напр. "Оперативная
    память" — реальный нерешённый вопрос 8542 vs 8471/8473) даже 2 согласных
    домена иногда отвечают не на тот вопрос (общий каталог, а не конкретно
    этот товар), а порог в 3+ домена наоборот отбрасывает почти все
    однозначные случаи (мало результатов на узкий запрос) — надёжного
    автоматического критерия найти не удалось, поэтому окончательное решение
    сознательно оставлено человеку (тот же класс риска, что и слепое
    LLM-угадывание по памяти — см. крах 11.07, из-за которого этот путь
    раньше был закрыт наглухо).
    Возвращает (code, note) или None, если ничего убедительного не нашлось."""
    from services.search.hybrid import product_search

    resp = await product_search.search_text(
        f"{subject_name} ТН ВЭД код", max_results=12, max_chars=3000,
        include_domains=_TNVED_CLASSIFIER_DOMAINS,
    )
    page_codes = {r.url: _extract_tnved_codes(r.url, r.title, r.text) for r in resp.results}
    pages = [r for r in resp.results if page_codes[r.url]]
    if len(pages) < 2:
        # Меньше 2 страниц с хоть каким-то кодом — недостаточно материала
        # для перекрёстной проверки, не пытаемся.
        return None

    all_codes = sorted({c for r in pages for c in page_codes[r.url]})
    context = "\n\n".join(
        f"=== {r.url} ===\n{r.title}\n{r.text[:1200]}" for r in pages[:6]
    )
    resp_llm = await llm.chat(
        "",
        "Ниже — выдержки с сайтов-классификаторов кодов ТН ВЭД ЕАЭС "
        f"по запросу «{subject_name} ТН ВЭД код».\n\n{context}\n\n"
        f"Найденные в тексте 10-значные коды: {', '.join(all_codes)}\n\n"
        f"Какой ОДИН код ТН ВЭД правильно классифицирует товар "
        f"«{subject_name}»? Используй ТОЛЬКО код из списка найденных выше — "
        "не придумывай новый. Если источники противоречат друг другу или "
        "ни один код нельзя выбрать уверенно — ответь ровно 'НЕТ'.\n"
        "Ответь ТОЛЬКО кодом (10 цифр) или 'НЕТ', без пояснений.",
        max_tokens=20, enable_thinking=False,
    )
    text = resp_llm.text.strip()
    m = _TNVED_CODE_RE.search(text)
    if not m:
        return None
    code = m.group()
    # Страховка: код должен буквально встречаться в найденном тексте, а не
    # быть придуман моделью в ответ на "не придумывай" — не полагаемся на
    # одну только инструкцию в промпте.
    if code not in all_codes:
        log.warning(f"search_tnved_code({subject_name!r}): LLM выдала код {code!r}, "
                    f"которого нет среди найденных {all_codes} — отбрасываю")
        return None
    domains = sorted({r.url.split("/")[2] for r in pages if code in page_codes[r.url] and r.url.startswith("http")})
    if len(domains) < 2:
        return None
    return code, f"веб-поиск классификаторов ({', '.join(domains)}) — ТРЕБУЕТ проверки человеком"


async def verify_generic_tnved_fit(
    product_or_subject_name: str, code: str, official_description: str, llm
) -> tuple[bool, str]:
    """11.08.2026: последний резерв, когда ни справочник WB, ни своя
    библиотека, ни живой веб-поиск (search_tnved_code) не дали кода —
    иногда есть широкий "прочие"-код верхнего уровня товарной позиции
    (напр. 8517620009 — "аппаратура для приёма, преобразования и передачи
    или восстановления голоса, изображений или других данных, включая
    аппаратуру коммутации и маршрутизации: прочая", покрывает и роутеры, и
    коммутаторы, и модемы одним пунктом), который заведомо в тему, просто
    не расписан WB на конкретный subjectID.

    Ключевое отличие от закрытого наглухо "слепого угадывания" (см. крах
    11.07, docstring search_tnved_code): здесь LLM НЕ придумывает код — код
    и его официальное описание подаются на вход человеком/кодом заранее
    (из реальной номенклатуры ТН ВЭД), модель только проверяет логическое
    соответствие товара описанию, т.е. классификационная, не генеративная
    задача — тот же класс риска, что verify_prompt в wb_create.py для
    подтверждения subjectID, а не тот, что уронил карточку 11.07.

    Возвращает (fits, reason) — fits=False должно блокировать применение
    кода, а не просто предупреждать."""
    resp = await llm.chat(
        "",
        f"Официальное описание пункта ТН ВЭД ЕАЭС {code}:\n«{official_description}»\n\n"
        f"Товар/категория: «{product_or_subject_name}»\n\n"
        "Подходит ли этот код по смыслу описания для этого товара? Не придумывай "
        "другой код — только оцени соответствие описания указанному товару. "
        "Ответь строго в формате 'ДА: <причина одной фразой>' или "
        "'НЕТ: <причина одной фразой>'.",
        max_tokens=60, enable_thinking=False,
    )
    text = resp.text.strip()
    fits = text.upper().startswith("ДА")
    reason = text.split(":", 1)[1].strip() if ":" in text else text
    return fits, reason


async def seed_from_profiles() -> int:
    """Заполняет базу текущими значениями CategoryProfile.tnved для всех
    профилей, у которых ещё нет строки в базе. Возвращает число добавленных."""
    from services.ozon_categories import PROFILES

    added = 0
    async with db_connect() as db:
        for key, profile in PROFILES.items():
            cur = await db.execute(
                "SELECT 1 FROM tnved_codes WHERE category_key = ?", (key,)
            )
            if await cur.fetchone():
                continue
            await db.execute(
                "INSERT INTO tnved_codes (category_key, tnved, note) VALUES (?, ?, ?)",
                (key, profile.tnved, "сид из CategoryProfile.tnved"),
            )
            added += 1
        await db.commit()
    if added:
        log.info(f"tnved_codes: засеяно {added} категорий из PROFILES")
    return added
