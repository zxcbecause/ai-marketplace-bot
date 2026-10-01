import logging
import re
from dataclasses import dataclass, field

from services.llm.base import LLMProvider, LLMResponse
from services.search import product_search, search_web_context
from .category import detect_category, build_chars_prompt, clean_product_name
from .normalize import parse_product_info, ProductInfo
from prompts import (
    DESCRIPTION_PROMPT,
    QUERY_GEN_PROMPT,
    PACKAGING_PROMPT,
)

log = logging.getLogger(__name__)


@dataclass
class CardResult:
    product: str
    category: str
    context: str
    description: str
    brand: str = ""
    model: str = ""
    color: str = ""
    color_en: str = ""
    memory: str = ""  # объём/память ("256GB", "8/256GB") — для поиска фото
                       # и Vision-проверки нужной ёмкости/SKU, отдельно от product
    characteristics: str = ""
    packaging: str = ""
    llm_responses: list[LLMResponse] = field(default_factory=list)
    exa_requests: int = 0
    article: str = ""
    wb_name: str = ""

    @property
    def search_name(self) -> str:
        """product + memory — для поиска фото/Vision: модель без объёма
        может совпасть с фото другой ёмкости той же линейки (кейс Patriot
        Burst Elite 120GB → подобралось фото 960GB)."""
        return f"{self.product} {self.memory}".strip() if self.memory else self.product

    @property
    def total_usd(self) -> float:
        return sum(r.usd for r in self.llm_responses)

    @property
    def total_tokens(self) -> int:
        return sum(r.input_tokens + r.output_tokens for r in self.llm_responses)

    def cost_summary(self) -> str:
        if not self.llm_responses:
            return ""
        providers = {}
        for r in self.llm_responses:
            if r.provider not in providers:
                providers[r.provider] = {"usd": 0.0, "tok": 0}
            providers[r.provider]["usd"] += r.usd
            providers[r.provider]["tok"] += r.input_tokens + r.output_tokens

        parts = []
        for name, data in providers.items():
            parts.append(f"{name}: ${data['usd']:.4f} ({data['tok']:,} тк)")
        if self.exa_requests:
            parts.append(f"Exa: {self.exa_requests} запросов")
        parts.append(f"Итого: ${self.total_usd:.4f}")
        return "\n".join(parts)


def _build_context_header(product: str, category: str,
                          search_product: str = "",
                          raw_specs: str = "") -> str:
    """raw_specs — исходная строка пользователя со всеми характеристиками.
    Если передана — добавляется как АВТОРИТЕТНЫЙ источник истины: DeepSeek
    обязан использовать именно эти значения (RAM, память, экран и т.п.),
    а не брать их из веб-контекста где могут быть другие варианты модели."""
    cat_line = f"КАТЕГОРИЯ ТОВАРА: {category}\n" if category else ""
    target = search_product if search_product and search_product != product else product
    specs_block = ""
    if raw_specs and raw_specs.strip():
        specs_block = (
            f"ДАННЫЕ ТОВАРА ОТ ПРОДАВЦА — АБСОЛЮТНЫЙ ПРИОРИТЕТ:\n"
            f"{raw_specs.strip()}\n"
            f"КРИТИЧНО: любое числовое значение из строки выше (мА·ч, МП, ГГц, ГБ, дюйм, Гц и т.д.) "
            f"ЗАПРЕЩЕНО заменять на данные из веб-контекста ниже. "
            f"Веб-контекст использовать ТОЛЬКО для характеристик которых НЕТ в строке выше.\n\n"
        )
    return (
        f"ЦЕЛЕВАЯ МОДЕЛЬ: {target}\n"
        f"{cat_line}"
        f"{specs_block}"
        f"ВАЖНО: использовать ТОЛЬКО данные для модели «{target}». "
        f"Если в тексте упоминаются другие объёмы памяти или другие модели — игнорировать.\n\n"
    )


async def _generate_search_queries(product: str, llm: LLMProvider) -> tuple[str, str]:
    """LLM генерирует EN и OTHER запросы для поиска."""
    resp = await llm.chat(f"Товар: {product}", QUERY_GEN_PROMPT)
    query_en, query_other = "", ""
    for line in resp.text.strip().splitlines():
        if line.upper().startswith("QUERY_EN:"):
            query_en = line.split(":", 1)[1].strip()
        elif line.upper().startswith("QUERY_OTHER:"):
            val = line.split(":", 1)[1].strip()
            if val.lower() != "none":
                query_other = val
    if not query_en:
        query_en = f"{product} specifications"
    return query_en, query_other, resp


async def build_context_from_search(
    product: str,
    category: str,
    llm: LLMProvider,
    raw_specs: str = "",
) -> tuple[str, int, list[LLMResponse]]:
    """
    Ищет информацию о товаре через Exa.
    raw_specs — исходная строка пользователя, вставляется в заголовок как авторитет.
    Возвращает (context, exa_requests, llm_responses).
    """
    query_en, query_other, query_resp = await _generate_search_queries(product, llm)
    log.info(f"Queries: EN='{query_en}' OTHER='{query_other}'")

    # Для фильтра контекста используем чистое имя (без спеков и запятых),
    # иначе токены вида "dk," или "230w," никогда не совпадают с текстом страниц.
    search_id = clean_product_name(product)
    context, exa_count = await product_search.search_product_context(
        search_id, query_en, query_other
    )

    if not context:
        # Exa ничего не нашла (товар слишком новый/нишевый) — пробуем
        # резервный поиск напрямую через Google/Yandex (Playwright).
        log.warning(f"Empty Exa context for: {product} — пробуем web-fallback (Google/Yandex)")
        web_resp = await search_web_context(f"{search_id} характеристики specifications")
        web_context = web_resp.as_context(model_id=search_id, min_score=0.4)
        if web_context:
            log.info(f"Web-fallback context найден для: {product}")
            full_context = _build_context_header(product, category, product, raw_specs=raw_specs) + web_context
            return full_context, exa_count, [query_resp]

        if raw_specs and raw_specs.strip():
            # Ни Exa, ни web-fallback ничего не дали, но продавец сам
            # дал характеристики в исходной строке — работаем с ними как с
            # единственным источником, веб-контекст не обязателен.
            log.warning(
                f"Empty context for: {product} — используем только данные "
                f"продавца (raw_specs), Exa-запросов: {exa_count}"
            )
            full_context = _build_context_header(product, category, product, raw_specs=raw_specs)
            return full_context, exa_count, [query_resp]
        log.warning(f"Empty context for: {product} — пропускаем генерацию")
        raise RuntimeError(f"Exa не нашёл данных для «{search_id}» — товар слишком новый или нишевый")

    full_context = _build_context_header(product, category, product, raw_specs=raw_specs) + context
    return full_context, exa_count, [query_resp]


async def build_context_from_url(
    product: str,
    category: str,
    url: str,
    raw_specs: str = "",
) -> tuple[str, int]:
    """Загружает контекст с конкретного URL."""
    import aiohttp
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0",
        "Accept-Encoding": "gzip, deflate",
    }
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, ssl=False, timeout=aiohttp.ClientTimeout(total=15),
                             headers=headers) as resp:
                if resp.status == 200:
                    text = await resp.text(errors="ignore")
                    # Убираем HTML теги
                    text = re.sub(r"<[^>]+>", " ", text)
                    text = re.sub(r"\s{3,}", "\n", text)
                    text = text[:10000]
                    context = _build_context_header(product, category, raw_specs=raw_specs) + f"Данные со страницы {url}:\n{text}"
                    return context, 0
    except Exception as e:
        log.warning(f"URL fetch failed {url}: {e}")

    return "", 0


def _normalize_spacing(text: str) -> str:
    """Только форматирование — без изменения слов/регистра/содержания:
    схлопывает повторные пробелы, убирает пробел перед запятой,
    гарантирует ровно один пробел после запятой."""
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s*,\s*", ", ", text)
    return text.strip(", ")


def truncate_title(text: str, limit: int = 60) -> str:
    """Обрезает wb_title до limit символов ПО ГРАНИЦЕ СЛОВА — жёсткий
    text[:60] резал прямо посреди слова (живой случай 25.08.2026, nmID
    1417170599: «...13 дюймов игров» вместо «игровой»). Если пробела в
    пределах limit нет — режет как раньше (нет более безопасного варианта
    для одного длинного слова/модели без пробелов)."""
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    last_space = cut.rfind(" ")
    return cut[:last_space] if last_space > 0 else cut


# Конец предложения: .!?… + пробел/конец строки. Пробел в проверке обязателен —
# иначе точка десятичной дроби («вес 5.2 кг», «2.5 ГГц») считалась бы концом
# предложения и резала прямо по числу.
_SENTENCE_END_RE = re.compile(r"[.!?…](?=\s|$)")


def truncate_description(text: str, limit: int = 2000) -> str:
    """Обрезает описание до limit символов ПО ГРАНИЦЕ ПРЕДЛОЖЕНИЯ.

    01.09.2026: жёсткий срез `wb_description[:2000]` обрывал текст на
    полуслове. Замер по 262 карточкам из логов: 177 упирались в потолок,
    171 (65%!) висели на живом WB с обрывом вида «…компактные размеры:
    10 см в ширину, высоту и глубину, при вес» (артикул 68128) или
    «…диммирования от 1% до 1» (84698). Причина не только в срезе: промпт
    просит 1500-1900 символов, но 80% ответов LLM длиннее 1900 — значит
    на потолок натыкается большинство карточек, и полагаться на
    дисциплину модели нельзя, обрезка обязана быть безопасной сама по себе.

    Если границы предложения в пределах limit нет вообще (текст без точек) —
    откатываемся на обрезку по границе слова: лучше потерять хвост фразы,
    чем разорвать слово.
    """
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    ends = list(_SENTENCE_END_RE.finditer(cut))
    if ends:
        return cut[:ends[-1].end()].rstrip()
    last_space = cut.rfind(" ")
    return (cut[:last_space] if last_space > 0 else cut).rstrip()


# 13.08.2026: живой случай (артикул 21QG003UFW, "Intel® Core™ Ultra 5...") —
# официальные спецификации Lenovo/Intel содержат ®/™/© прямо в тексте.
# _format_supplier_line копирует raw-строку продавца 1:1 в первую строку
# описания WB-карточки — WB асинхронно (уже ПОСЛЕ 200 OK на cards/upload)
# отклоняет карточку с "Поле Описание не должно содержать запрещенные
# символы: ® ™", и это раньше даже не долетало до пользователя (см. фикс
# _wb_error_for рядом). Вырезаем эти 3 символа — они не несут смысловой
# нагрузки и не меняют слова/модель, только визуальную пунктуацию.
_WB_FORBIDDEN_DESC_SYMBOLS_RE = re.compile(r"[®™©]")

# 17.08.2026: живой случай (артикул Z10, «Zalman Z10 ... TG (side]») — «TG»
# у продавцов корпусов ПК означает Tempered Glass (закалённое стекло,
# стандартная спека боковой панели), но модерация WB ловит «TG»/«ТГ» как
# упоминание мессенджера Telegram и отклоняет карточку ("Запрещено
# указывать мессенджеры в поле Наименование; ...в поле Описание") — та же
# асинхронная ошибка ПОСЛЕ 200 OK, что и с ®™© выше. Токен целиком, по
# границе слова (а не подстрокой где угодно) — не задевает буквы "tg"
# внутри других слов, но режет и модели вида "TG-500", раз WB режет их тоже.
_WB_FORBIDDEN_MESSENGER_RE = re.compile(r"\bTG\b", re.IGNORECASE)

# 26.08.2026: живой случай (артикул LS25HG400EIXCI, «...300кд/м2 1000:1
# 1xHDMI...») — WB отклонил карточку с "Запрещено указывать телефонные
# номера в поле Наименование" из-за контрастности "1000:1": модерация видит
# цифры через двоеточие как телефонный номер, хотя это техническая
# характеристика (контрастность, соотношение сторон и т.п.). Меняем
# двоеточие МЕЖДУ цифрами на "/" — значение остаётся читаемым (1000/1
# вместо 1000:1), но перестаёт совпадать с шаблоном телефона. Не трогает
# обычные двоеточия (после слов, в описании характеристик и т.п.).
#
# 27.08.2026: тот же фильтр WB ловит и "/" между цифрами, не только ":" —
# живой случай (артикул 27MS500, сырая строка продавца «...27" IPS/1920 x
# 1080/ 100Hz /5ms/ 200 кд/м?/  HDMI»), "поле Наименование" — карточка
# копирует сырую строку продавца 1:1 в первую строку описания
# (_format_supplier_line), а там спеки через "/" вплотную к цифрам
# ("1080/ 100Hz"). Раньше regex ловил только ":", "/" пропускал — здесь
# триггер именно на "/". Заменять на ещё один "/" уже нельзя (это и есть
# триггер) — заменяем на " · " (не похоже на разделитель телефона/дроби,
# читаемость сохраняется). Допускаем необязательные пробелы вокруг
# разделителя (в реальных сырых строках между "/" и следующей цифрой часто
# есть пробел, как в примере выше) — иначе паттерн не поймает.
_WB_PHONE_LIKE_RE = re.compile(r"(?<=\d)\s*[:/]\s*(?=\d)")

# 27.08.2026, третий заход на тот же фильтр (артикул 27MS500) — оказалось,
# что WB ловит "телефонный номер" даже без ":"/"/"" вовсе: чистый заголовок
# без единого спецсимвола "Монитор IPS 1920x1080 100 Гц" ТОЖЕ отклонялся.
# Изолировано прямыми тестовыми cards/upload (без полного пайплайна) —
# триггер: подряд идущие цифры (через пробел/x/× без буквы между ними)
# длиной ~10+ цифр подряд похожи на телефон ("1920x1080 100" → "19201080100"
# без разделителей = 11 цифр). Разрешение (WxH), сразу за которым идёт ещё
# одно число только через пробел — самый частый живой случай (мониторы/ТВ:
# разрешение + герцовка/яркость/время отклика рядом). Запятая ЭКСПЕРИМЕНТАЛЬНО
# подтверждена как безопасный разделитель (WB её не считает частью
# телефонного шаблона, в отличие от пробела/x/:/ /-) — вставляем её сразу
# после WxH, если следом (через пробел) идёт ещё цифра.
_WB_RESOLUTION_ADJACENT_DIGIT_RE = re.compile(r"(\d{3,4}\s*[xX×]\s*\d{3,4})(\s+)(?=\d)")


def _wb_sanitize_text(text: str) -> str:
    """Общий набор санитайзеров модерации WB (символы ®™©, "TG" как
    мессенджер, цифры-как-телефон) — один вызов вместо ручного повторения
    цепочки .sub() в каждом месте (title/description/значения
    характеристик), см. док-комментарии у каждого regex выше."""
    text = _WB_FORBIDDEN_DESC_SYMBOLS_RE.sub("", text)
    text = _WB_FORBIDDEN_MESSENGER_RE.sub("", text)
    text = _WB_RESOLUTION_ADJACENT_DIGIT_RE.sub(r"\1,\2", text)
    text = _WB_PHONE_LIKE_RE.sub(" · ", text)
    return text


def _format_supplier_line(raw: str) -> str:
    """Первая строка описания — характеристики из строки, которую ввёл
    продавец (название+артикул, затем спеки через первую запятую — в
    квадратных скобках), с лёгкой нормализацией пробелов/запятых.
    Без LLM и без изменения слов — гарантирует, что содержание совпадает
    1:1 с тем, что было в команде, без искажений при перефразировании моделью."""
    raw = _wb_sanitize_text(raw)
    raw = _normalize_spacing(raw)
    if "," not in raw:
        return raw
    head, _, tail = raw.partition(",")
    return f"{_normalize_spacing(head)} [{_normalize_spacing(tail)}]"


# 12.08.2026: живой случай (карточка 86124) — DeepSeek дописал в конце
# описания видимую самопроверку по символам ("Посчитал символы: примерно
# 1600. Проверю точнее. Текст: от ... Давай посчитаю по частям...") прямо в
# content вместо того, чтобы держать её в reasoning_content — ушло на живую
# карточку WB как есть. Промпт (DESCRIPTION_PROMPT) переформулирован, чтобы
# не провоцировать это ("посчитай перед отправкой" звучало как указание
# показать работу), но страховка на случай повтора не помешает — резать
# последний абзац, если он похож на такую самопроверку, а не на текст
# описания.
_LEAK_MARKERS_RE = re.compile(
    r"посчита[лю]|символов\s*(?:включая|примерно|:)|провер[юь]\s+точн|"
    r"давай посчита|итого символ",
    re.IGNORECASE,
)


def _strip_leaked_reasoning(text: str) -> str:
    paragraphs = text.split("\n\n")
    while paragraphs and _LEAK_MARKERS_RE.search(paragraphs[-1]):
        paragraphs.pop()
    return "\n\n".join(paragraphs).strip()


async def generate_description(
    context: str,
    llm: LLMProvider,
) -> tuple[str, LLMResponse]:
    resp = await llm.chat(context, DESCRIPTION_PROMPT)
    return _strip_leaked_reasoning(resp.text), resp


async def generate_characteristics(
    context: str,
    category: str,
    llm: LLMProvider,
) -> tuple[str, LLMResponse, bool]:
    """Возвращает (text, response, category_found)."""
    chars_prompt, cat_found = build_chars_prompt(category)
    # Потолок размышления — та же причина, что и у WB-характеристик
    # (services/wb_create.py, замер 01.09.2026): заполнение полей справочника
    # разгоняет «мысли» в разы сильнее самого ответа, а биллятся они как output.
    resp = await llm.chat(context, chars_prompt, thinking_budget=512)
    return resp.text.strip(), resp, cat_found


async def generate_packaging(
    context: str,
    llm: LLMProvider,
    product: str = "",
    category: str = "",
) -> tuple[str, LLMResponse]:
    pack_context = context
    if product:
        from services.search import product_search
        pack_extra, _ = await product_search.search_packaging_context(product, category=category)
        if pack_extra:
            # Данные маркетплейсов идут первыми — приоритет над общим контекстом
            pack_context = (
                "ДАННЫЕ МАРКЕТПЛЕЙСОВ (габариты и вес В УПАКОВКЕ):\n"
                + pack_extra
                + "\n\nОБЩИЙ КОНТЕКСТ:\n"
                + context
            )
    resp = await llm.chat(pack_context, PACKAGING_PROMPT)
    return resp.text.strip(), resp


async def generate_full_card(
    product: str,
    llm: LLMProvider,
    url: str | None = None,
    desc_only: bool = False,
    info: ProductInfo | None = None,
    wb_context: str | None = None,
    wb_context_rich: bool = False,
    need_characteristics: bool = True,
) -> CardResult:
    """
    Полный цикл генерации карточки.
    desc_only=True — только описание (для /batch).
    info — заранее разобранный ProductInfo (если уже вызывали parse_product_info
    для этой строки) — избегаем повторного DeepSeek-запроса.
    wb_context — готовый блок данных с нашей WB-карточки (WB-first режим,
    экономия Exa): полный Exa-поиск не выполняется. При wb_context_rich=False
    (характеристик на WB мало) — добор максимум 2 запросами (lite).
    need_characteristics=False (17.07) — пропускает внутренний
    generate_characteristics() (один полный DeepSeek round-trip, 15-40+с)
    при desc_only=False. Для /wb_create и /wb_batch результат этого вызова
    (result.characteristics) нигде не используется — они строят СВОЙ
    промпт характеристик под конкретные поля WB-категории отдельным
    вызовом. Экономит время и деньги без изменения поведения там, где
    result.characteristics реально нужен (пока таких вызывающих нет).
    """
    raw_input = product  # сохраняем исходную строку пользователя со всеми спеками
    product = clean_product_name(product)
    if info is None:
        info = await parse_product_info(product, llm)
    # full_name — для отображения ("Infinix HOT 12")
    # search_name — для поиска ("Infinix HOT 12 4/128GB") — точнее различает варианты
    product = info.full_name
    search_product = info.search_name
    category = detect_category(product) or detect_category(raw_input)
    llm_responses: list[LLMResponse] = []
    exa_requests = 0

    # Контекст
    if url:
        context, exa_requests = await build_context_from_url(
            product, category, url, raw_specs=raw_input
        )
        if not context:
            log.warning(f"URL fetch failed, fallback to search: {url}")
            context, exa_requests, search_responses = await build_context_from_search(
                search_product, category, llm, raw_specs=raw_input
            )
            llm_responses.extend(search_responses)
    elif wb_context is not None:
        # WB-first: данные собственной WB-карточки вместо Exa-поиска
        context = _build_context_header(product, category, product, raw_specs=raw_input) + wb_context
        if not wb_context_rich:
            extra, extra_reqs = await product_search.search_product_context_lite(search_product)
            exa_requests += extra_reqs
            if extra:
                context += "\n\n" + extra
            log.info(f"WB-first: характеристик мало, добор lite ({extra_reqs} Exa) для {product}")
        else:
            log.info(f"WB-first: контекст целиком с WB-карточки (0 Exa) для {product}")
    else:
        context, exa_requests, search_responses = await build_context_from_search(
            search_product, category, llm, raw_specs=raw_input
        )
        llm_responses.extend(search_responses)

    # Генерация текстов
    description, desc_resp = await generate_description(context, llm)
    llm_responses.append(desc_resp)
    # LLM-абзац тоже может утащить ®/™/© из веб-контекста (спецификации
    # источника) — та же зачистка, что и для echo-строки продавца ниже.
    description = _WB_FORBIDDEN_DESC_SYMBOLS_RE.sub("", description)
    # Первая строка — точная копия ввода продавца (см. _format_supplier_line)
    description = f"{_format_supplier_line(raw_input)}\n\n{description}"

    characteristics = ""
    packaging = ""

    if not desc_only:
        if need_characteristics:
            chars_text, chars_resp, _ = await generate_characteristics(context, category, llm)
            llm_responses.append(chars_resp)
            characteristics = chars_text
        pack_text, pack_resp = await generate_packaging(context, llm)
        llm_responses.append(pack_resp)
        packaging = pack_text

    return CardResult(
        product=product,
        category=category,
        context=context,
        description=description,
        brand=info.brand,
        model=info.model,
        color=info.color,
        color_en=info.color_en,
        memory=info.memory,
        characteristics=characteristics,
        packaging=packaging,
        llm_responses=llm_responses,
        exa_requests=exa_requests,
    )
