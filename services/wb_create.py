"""
Создание карточек WB с нуля через Content API (POST /content/v2/cards/upload) —
сервис для команды /wb_create (14.07.2026).

Ядро перенесено из tools/wb_live_create.py (разовый скрипт 11-12.07.2026, там же
задокументированы все грабли живого API). Сюда НЕ переносим его побочные
эффекты (включение Exa на процесс, logging.basicConfig).

Правила живого API (проверено на 178 товарах, сессия 55):
— dimensions.weightBrutto обязателен, иначе 400 на весь батч;
— бренд/цвет только из справочников WB (точное имя), иначе падает вся карточка;
— existNamedField-характеристики не слать в characteristics (конфликт);
— текстовые значения резать до ~95 символов;
— по ОДНОЙ карточке за запрос: cards/upload — всё-или-ничего на весь батч;
— ТН ВЭД (14.07.2026) берём из официального справочника WB
  (content/v2/directory/tnved?subjectID=...), не угадываем LLM — раньше
  пропускали поле вообще, так как LLM-код ронял карточку;
— гарантийный срок (id 9623) форсим «12 месяцев» (правило магазина).
"""
import asyncio
import difflib
import html
import json
import logging
import re
import statistics
import time
from pathlib import Path

import requests

from config import settings
from database.db import db_connect
from services.llm import LLMProvider
from services.card.generator import (
    generate_full_card, _wb_sanitize_text, _normalize_spacing, truncate_title,
    truncate_description,
)
from services.search.images import find_product_images
from services.storage import upload_image
from services.wb_content import _find_card
from prompts import WB_NAME_PROMPT
from utils.billing import save_cost, save_wb_tnved_miss
from services.tnved import (
    get_wb_tnved, set_wb_tnved,
    get_wb_tnved_candidates, set_wb_tnved_candidates, search_tnved_candidates,
)

log = logging.getLogger(__name__)

BASE = "https://content-api.wildberries.ru"

# 20.07.2026: одно keep-alive соединение на процесс вместо нового
# TLS-handshake на каждый запрос — сегодняшние ConnectTimeout'ы
# content-api.wildberries.ru били именно в фазу установки соединения.
_wb_session = requests.Session()

# Лимит магазина (14.07.2026, ужесточён 23.07.2026 с 26 до 24.5 кг):
# тяжелее в упаковке не создаём.
MAX_WEIGHT_KG = 24.5

# Лимит магазина (23.07.2026, по факту карнизов Яндекс 2.4-4.5м — созданы,
# потом вручную удалены): длиннее 120 см САМОГО ТОВАРА (не упаковки — карниз
# телескопический и пакуется компактно, но реальная длина в разложенном виде
# намного больше) тоже не создаём.
MAX_PRODUCT_LENGTH_CM = 120.0
_LENGTH_KEY_RE = re.compile(r"длина", re.IGNORECASE)

# 28.07.2026: ранний грубый фильтр по весу/габаритам (см. create_one) —
# один дешёвый LLM-вызов ДО resolve_subject/generate_full_card.
_QUICK_DIMS_PROMPT_TMPL = (
    "Товар: {name}\n\n"
    "Дай ГРУБУЮ оценку по названию: примерный вес в упаковке (кг) и "
    "наибольший габарит УПАКОВКИ, как товар реально поставляется (см). "
    "ВАЖНО: гибкие/сворачиваемые товары (кабели, шнуры, провода, "
    "светодиодные ленты, шланги, удлинители) ВСЕГДА едут смотанными в "
    "компактной коробке — их номинальная длина в названии (например «3M», "
    "«5M») НЕ является габаритом упаковки, для них давай оценку по размеру "
    "бухты/катушки (обычно 15-30 см), а не по развёрнутой длине. Задача — "
    "отсечь ЯВНО крупногабаритные/тяжёлые товары (мебель, крупная бытовая "
    "техника вроде холодильников/стиральных машин, стройматериалы) до "
    "подробного анализа, а не точно посчитать цифры — если сомневаешься, "
    "давай оценку С ЗАПАСОМ В МЕНЬШУЮ сторону (не завышай).\n"
    "Если по названию вообще невозможно предположить (мало данных) — "
    "ответь ровно 'НЕИЗВЕСТНО'.\n"
    "Иначе ответь СТРОГО двумя строками, без пояснений:\n"
    "ВЕС_КГ: <число>\n"
    "ДЛИНА_СМ: <число>"
)

# У сложной электроники (особенно нишевые BTO/EMEA-конфигурации ноутбуков) в
# сети часто гуляет один и тот же официальный фотосет — дедуп по силуэту (IoU)
# и Vision-фильтр схлопывают дефолтный пул кандидатов (n=3, max_candidates=10)
# до 1 уникального фото. Для этих категорий просим заметно больше кандидатов и
# больше финальных фото — чтобы на руках реально было несколько разных
# ракурсов, а не одно случайно выжившее.
_MANY_PHOTOS_CATEGORIES = {"Ноутбуки", "Мониторы", "Моноблоки", "Смартфоны", "Планшеты"}
_MANY_PHOTOS_N = 6
_MANY_PHOTOS_MAX_CANDIDATES = 20


def _headers() -> dict:
    return {"Authorization": settings.wb_api_key, "Content-Type": "application/json"}


# ── Определение категории (subjectID) ──────────────────────────────────

def _search_subjects(query: str, limit: int = 25) -> list[dict]:
    """GET /content/v2/object/all — поиск предметов WB по подстроке имени."""
    r = _wb_session.get(
        f"{BASE}/content/v2/object/all",
        headers=_headers(),
        params={"name": query, "limit": limit, "locale": "ru"},
        timeout=20,
    )
    r.raise_for_status()
    return r.json().get("data", [])


# ── Локальный полный справочник предметов + нечёткий поиск (19.07.2026) ───
# object/all по параметру name матчит почти ДОСЛОВНОЕ имя предмета — любое
# расхождение в окончании/числе/порядке слов даёт 0 результатов (примеры за
# 14-19.07: «Сканеры штрих-кодов» вместо реального «Сканеры штрих-кода»,
# «Игровые кресла» вместо «Кресла игровые», «Материнская плата» вместо
# «Материнские платы»...). Раньше каждый такой случай чинился вручную через
# _WB_SUBJECT_HINTS ПОСЛЕ того как пользователь ловил ошибку на живом батче —
# не масштабируется. Вместо этого держим у себя ПОЛНЫЙ справочник (7199
# предметов, постранично через offset — на один запрос отдаёт максимум 1000)
# и матчим нечётко (token-sort ratio на нормализованных именах) — ловит
# ошибки склонения/числа/порядка слов без ручного вмешательства. Хинты в
# _WB_SUBJECT_HINTS остаются нужны только для НАСТОЯЩИХ смысловых расхождений
# (одна наша категория = несколько разных предметов WB, например «Акустика»).
_WB_SUBJECTS_CACHE_FILE = Path(__file__).resolve().parent.parent / "data" / "wb_subjects_cache.json"
_all_subjects_cache: list[dict] | None = None


def _fetch_all_subjects_live() -> list[dict]:
    all_data: list[dict] = []
    offset = 0
    while True:
        r = _wb_session.get(
            f"{BASE}/content/v2/object/all",
            headers=_headers(),
            params={"limit": 1000, "offset": offset, "locale": "ru"},
            timeout=20,
        )
        r.raise_for_status()
        page = r.json().get("data", [])
        all_data.extend(page)
        if len(page) < 1000:
            break
        offset += 1000
    return all_data


def _load_all_subjects() -> list[dict]:
    """Полный справочник предметов WB, кэш на диске (обновляется вручную —
    см. tools/wb_refresh_subjects_cache.py — таксономия WB меняется редко)."""
    global _all_subjects_cache
    if _all_subjects_cache is not None:
        return _all_subjects_cache
    if _WB_SUBJECTS_CACHE_FILE.exists():
        try:
            _all_subjects_cache = json.loads(_WB_SUBJECTS_CACHE_FILE.read_text(encoding="utf-8"))
            return _all_subjects_cache
        except Exception as e:
            log.warning(f"wb_subjects_cache.json повреждён, перезагружаю: {e}")
    data = _fetch_all_subjects_live()
    _WB_SUBJECTS_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _WB_SUBJECTS_CACHE_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    _all_subjects_cache = data
    return data


def _tokenize_for_match(s: str) -> list[str]:
    return re.findall(r"[а-яa-z0-9]+", s.lower().replace("ё", "е"))


def _token_set_ratio(words_a: list[str], words_b: list[str]) -> float:
    """Аналог fuzzywuzzy.token_set_ratio на stdlib difflib — сравнивает по
    общим словам, лишние уточняющие слова с одной стороны («... и блоки
    питания») не портят оценку, в отличие от обычного SequenceMatcher."""
    set_a, set_b = set(words_a), set(words_b)
    common = " ".join(sorted(set_a & set_b))
    only_a = (common + " " + " ".join(sorted(set_a - set_b))).strip()
    only_b = (common + " " + " ".join(sorted(set_b - set_a))).strip()
    return max(
        difflib.SequenceMatcher(None, common, only_a).ratio(),
        difflib.SequenceMatcher(None, common, only_b).ratio(),
        difflib.SequenceMatcher(None, only_a, only_b).ratio(),
    )


def _prefix_coverage(a: str, b: str) -> float:
    """Доля общего префикса от длины короткого слова. Нужна, чтобы отличить
    словоформы одного слова («мышь»/«мыши», общий префикс покрывает 75%+
    короткого слова) от разных слов со случайно общим приставочным куском
    («электробритва»/«электрогитары» — общий кусок только «электро», 54%)."""
    i = 0
    while i < len(a) and i < len(b) and a[i] == b[i]:
        i += 1
    shorter = min(len(a), len(b))
    return i / shorter if shorter else 0.0


def _fuzzy_subjects(query: str, top_k: int = 6, min_ratio: float = 0.72) -> list[dict]:
    """Нечёткий локальный поиск по полному справочнику — словопорядок,
    небольшие отличия в окончании/числе и лишние уточняющие слова не мешают
    (в отличие от буквального поиска object/all)."""
    q_words = _tokenize_for_match(query)
    if not q_words:
        return []
    q_sorted = " ".join(sorted(q_words))
    scored = []
    for s in _load_all_subjects():
        s_words = _tokenize_for_match(s["subjectName"])
        ratio = max(
            difflib.SequenceMatcher(None, q_sorted, " ".join(sorted(s_words))).ratio(),
            _token_set_ratio(q_words, s_words),
        )
        if ratio < min_ratio:
            continue
        # 30.07.2026: «электробритва» → «Электрогитары» прошло по ratio=0.769
        # (порог 0.72) — оба слова начинаются с «электро-», SequenceMatcher
        # засчитал этот общий кусок как сходство, хотя предметы никак не
        # связаны. Если у запроса и кандидата нет ни одного полностью общего
        # слова, требуем, чтобы общий префикс покрывал большую часть короткого
        # слова — иначе это две разные основы, а не словоформа.
        if not (set(q_words) & set(s_words)):
            if not any(_prefix_coverage(qw, sw) >= 0.7 for qw in q_words for sw in s_words):
                continue
        scored.append((ratio, s))
    scored.sort(key=lambda x: -x[0])
    return [s for _, s in scored[:top_k]]


async def _gather_subject_candidates(
    q: str, candidates: dict[int, dict], exclude: set[int] | None,
    exact_ids: set[int] | None = None,
) -> None:
    """exact_ids — subjectID, найденные ТОЧНЫМ совпадением через object/all
    (не нечётким поиском) — см. использование в resolve_subject: единственный
    кандидат, найденный ТОЛЬКО нечётким поиском, не считается надёжным сам
    по себе (живой случай: "Филамент" ложно совпал по буквам с "Фламенко" —
    категория обуви, попавшая в карточку без единой смысловой проверки,
    потому что кандидат был ровно один)."""
    try:
        for s in await asyncio.to_thread(_search_subjects, q):
            if not exclude or s["subjectID"] not in exclude:
                candidates[s["subjectID"]] = s
                if exact_ids is not None:
                    exact_ids.add(s["subjectID"])
    except Exception as e:
        log.warning(f"object/all «{q}»: {e}")
    for s in await asyncio.to_thread(_fuzzy_subjects, q):
        if not exclude or s["subjectID"] not in exclude:
            candidates.setdefault(s["subjectID"], s)


# 17.07.2026: наша внутренняя категоризация (category.py CATEGORY_MAP) —
# это ~35 своих, часто более широких или иначе названных группировок, а не
# точное отражение официальной таксономии WB (object/all матчит почти
# ДОСЛОВНОЕ совпадение имени). Раньше расхождение чинилось по одному
# случаю за раз, когда пользователь натыкался на ошибку (так нашли и
# починили "Сетевое оборудование" → "Роутеры"). Вместо этого проверили
# ЖИВЬЁМ через object/all все ~35 категорий разом — нашлось ещё 6 таких же
# расхождений. Причины расходятся по случаям:
#   - другое слово/множественное число: "Сетевое оборудование" → "Роутеры"
#     (WB вообще не знает такого обобщающего названия);
#   - другой порядок слов: "Игровые кресла" → "Кресла игровые";
#   - лишнее уточнение в нашем названии: "Зарядные устройства и блоки
#     питания" → "Зарядные устройства", "Кабели и аксессуары" → "Кабели";
#   - другая буква ё/е: "Внешние жёсткие диски" → "Внешние жесткие диски";
#   - множественное число у ОБОИХ слов сразу, не только у первого:
#     "Комплект клавиатура и мышь" → "Комплекты клавиатур и мышей" (не
#     "Комплекты клавиатура и мышь" — первая попытка была неверной, WB
#     склоняет во множественное оба существительных).
_WB_SUBJECT_HINTS: dict[str, list[str]] = {
    # Несколько реальных WB-подкатегорий стоят за одной нашей — оба варианта
    # идут кандидатами, LLM выбирает по факту (802.11/PCIe/USB → адаптер,
    # а не роутер) вместо слепого хардкода первого варианта (17.07, батч
    # сетевых карт Asus/TP-Link, см. category.py "сетевая карта").
    "Сетевое оборудование": ["Роутеры", "Wi-Fi-адаптер"],
    "SSD накопители": ["SSD-накопители"],  # неоднозначно (внешние/внутренние) — решает LLM-выбор ниже
    "Внешние жёсткие диски": ["Внешние жесткие диски"],
    "Зарядные устройства и блоки питания": ["Зарядные устройства"],
    "Игровые кресла": ["Кресла игровые"],
    "Кабели и аксессуары": ["Кабели"],
    "Комплект клавиатура и мышь": ["Комплекты клавиатур и мышей"],
    # 18.07: наша "Акустика" (category.py) не матчится в object/all вовсе —
    # WB находит только нерелевантные "Акустика для микроэлектроники"/
    # "Мотоакустика", LLM выбирала одну из них, а в её справочнике брендов
    # закономерно нет Edifier/JBL/Sony и т.п. (карточка уходила без бренда).
    # Реальные WB-категории для колонок/акустических систем — три штуки,
    # LLM выбирает по факту (портативная/Bluetooth vs проводная компьютерная).
    "Акустика": ["Колонки портативные", "Колонки компьютерные", "Колонки"],
    # 20.07: "Блоки питания" (общая, 1896 брендов, для мелкой электроники/
    # адаптеров) нечёткий поиск ранжирует ВЫШЕ узкой "Блоки питания для
    # компьютеров" (443 бренда, но именно она — правильный раздел для ATX-БП)
    # — тот же класс риска, что уже стоил 8 карточек Edifier в корзине WB
    # (subjectID нельзя сменить после создания, только пересоздание+корзина).
    # Форсим специализированный вариант первым кандидатом.
    "Блоки питания": ["Блоки питания для компьютеров", "Блоки питания"],
    # 30.07.2026: наша "Бритвы электрические" (category.py) — а не одно слово
    # "электробритва", как обычно пишут в названии товара; форсим точное
    # WB-имя первым кандидатом.
    "Бритвы электрические": ["Бритвы электрические"],
    # 04.08.2026: WB не знает слова "филамент" вообще (object/all и нечёткий
    # локальный поиск оба дают 0 совпадений) — реальная категория для
    # пластика/нити для 3D-печати называется "Комплектующие для 3D"
    # (subjectID 1174, раздел "Оргтехника", там же 3D-принтеры/3D-ручки).
    # Проверено live через content-api object/all 04.08.2026.
    "Филамент для 3D-печати": ["Комплектующие для 3D"],
    # 10.08.2026: WB пишет эту категорию латиницей ("Web-камеры", subjectID
    # 1378) — кириллический запрос "Веб-камеры" не даёт ни точного, ни
    # нечёткого совпадения вообще (единственное, что находится по буквам —
    # не связанные по смыслу "Шторки для веб-камер").
    "Веб-камеры": ["Web-камеры"],
    # 17.08.2026: живой батч — 5 из 6 "Контроллер доступа Hikvision DS-K2xxx"
    # упали вообще без карточки (категория не нашлась), шестой (DS-K2814)
    # создался, но со смыслово неверной категорией "Контроллеры для
    # микроэлектроники" (раздел "Электрика"). У WB нет отдельной категории
    # для СКУД/контроллеров доступа — резервуар "контроллер"-кандидатов из
    # нечёткого поиска (микроэлектроника/ПК/заряд/насосы/ИБП и т.п.) весь
    # смыслово мимо. Ближайшая реальная категория — "Блоки управления
    # замком" (subjectID 5797, раздел "Умный дом и безопасность") — там же
    # у WB "Кнопки выхода", "Умные дверные замки", домофоны и т.п., это и
    # есть фактический раздел для СКУД-оборудования на маркетплейсе.
    # Проверено live через wb_subjects_cache.json 17.08.2026.
    "СКУД": ["Блоки управления замком"],
    # 28.08.2026: точной WB-категории под форм-фактор нет (проверено нечётким
    # поиском по всему справочнику 7199 предметов) — без этой записи LLM-
    # верификация справедливо бракует "Средства для очистки воды" для
    # физического устройства-фильтра (правильно отличает средство/жидкость
    # от прибора), карточка падала в "категория WB не найдена" даже после
    # прямого попадания в кандидаты через CATEGORY_MAP. Решение принято
    # пользователем вручную (чат, 28.08) — как и "Акустика"/"СКУД" выше,
    # доверяем выбору без повторной проверки.
    "Средства для очистки воды": ["Средства для очистки воды"],
    # 28.08.2026: аналогично — «Кронштейн Ubiquiti UB-AM» (универсальный
    # крепёж для точки доступа/свитча), готовой WB-категории нет (прецедент —
    # "ubiquiti quick-mount" в category.py, тот же вывод раньше). LLM-
    # верификация бракует "Кронштейны настенные" по буквальному смыслу
    # (не крепление для мебели/полки), хотя это ближайший реальный вариант.
    # Решение пользователя (чат, 28.08).
    "Кронштейны настенные": ["Кронштейны настенные"],
    # 17.08.2026: см. category.py — "подставка"/"стойка" для АС/колонок не
    # матчится со словом WB-категории ("кронштейн"). Форсим точное имя,
    # чтобы обойти нечёткий поиск, который иначе тонет в полусотне
    # неродственных "Подставки для X" (зонтов, ножей, казана и т.п.).
    "Кронштейны для колонок": ["Кронштейны для колонок"],
}


async def _subject_synonym_terms(
    name: str, llm, user_id: int, context_block: str,
) -> list[str]:
    """14.08.2026: последний резерв resolve_subject — синонимные формулировки
    категории. Живой кейс: «Игровая приставка SONY PlayStation5» — поиск по
    словам названия давал «Игровые палатки»/«Рули игровые» (совпадение по
    букве, смысловая проверка их верно рубила), а настоящая категория WB
    называется другим словом — «Игровые консоли». Фаззи-матч по каталогу
    ловит склонения/порядок слов, но НЕ синонимы. Просим LLM назвать тип
    товара разными словами; кандидаты по этим термам дальше проходят ту же
    смысловую проверку, что и обычные — это не слепое доверие LLM."""
    syn_resp = await llm.chat(
        "",
        "Назови 3-5 РАЗНЫХ формулировок категории Wildberries для этого "
        "товара — обязательно синонимами, разными словами (примеры: "
        "«игровая приставка» → Игровые консоли; «батарейка» → Элементы "
        "питания; «трипод» → Штативы).\n"
        f"Товар: {name}\n{context_block}\n"
        "Множественное число, как называются категории WB. По одной "
        "формулировке на строку, без пояснений и нумерации.",
        max_tokens=80, enable_thinking=False,
    )
    await save_cost(user_id, "wb_create_subject_syn", response=syn_resp)
    terms, seen = [], set()
    for ln in syn_resp.text.strip().splitlines():
        t = ln.strip().strip('"').strip("'").lstrip("-•0123456789. ")[:40]
        if t and t.lower() not in seen:
            seen.add(t.lower())
            terms.append(t)
    return terms[:5]


async def resolve_subject(
    name: str, llm: LLMProvider, user_id: int, exclude: set[int] | None = None,
    context: str = "", raw_name: str = "", _syn_queries: list[str] | None = None,
) -> tuple[int, str]:
    """Определяет subjectID/subjectName для произвольного названия товара:
    кандидаты из object/all по первым словам названия, выбор — LLM.
    Бросает ValueError, если кандидатов нет.

    exclude — subjectID, которые уже пробовали и забраковали (см.
    create_one: ретрай, когда бренд не нашёлся в справочнике выбранной
    категории — сигнал, что категория выбрана неверно, см. случай Edifier/
    "Акустика" 18.07).

    context — кусок исследованного контента товара (result.context из
    generate_full_card, 10.08.2026: resolve_subject теперь вызывается ПОСЛЕ
    generate_full_card, не параллельно — категория определяется уже понимая,
    что это за товар, не только по сырому названию продавца). Идёт в промпты
    смысловой проверки как дополнительное обоснование, необязателен — вызовы
    без него (напр. из старых мест/тестов) работают как раньше.

    raw_name — исходное название продавца ДО нормализации (10.08.2026,
    второй фикс за день: result.product/name часто теряют само слово-
    категорию — normalize.py/parse_product_info оставляют только бренд+
    модель, напр. «Активный сабвуфер Dali SUB K-14 F» → «Dali SUB K-14 F»,
    «Батарея для UPS ... WBR GP1272 F2» → «WBR GP1272 F2». detect_category(name)
    тогда бьёт мимо CATEGORY_MAP не потому что категории там нет, а потому
    что её слово физически не попало в укороченную строку — и товар уходит
    в LLM-угадывание вместо надёжного прямого совпадения. Пробуем raw_name
    как запасной источник ДО угадывания."""
    context_block = f"\nНайденная информация о товаре:\n{context[:600]}\n" if context else ""
    # 27.08.2026: тот же класс бага, что и с detect_category(raw_name) выше
    # (10.08.2026) — result.product/name часто теряют слово-категорию
    # («Концентратор USB TP-Link UE330C» → «TP-Link UE330C»), но раньше
    # только ПОИСК кандидатов получал raw_name как запасной источник, а
    # промпты смысловой проверки/выбора («Товар: {name}») всё ещё показывали
    # LLM урезанное имя без типа товара. Живой случай: кандидаты «Разветвители
    # USB»/«Адаптеры» (в целом правильные) отбраковывались LLM, потому что
    # из «TP-Link UE330C» вообще не видно, что это USB-хаб. display_name —
    # то, что реально показываем LLM в прояснении смысла; поиск кандидатов
    # (words/queries) по-прежнему работает от name, это не трогаем.
    display_name = raw_name or name
    words = [w for w in re.split(r"[\s,]+", name) if w]
    queries = []

    # 14.08.2026: синонимный ретрай (см. _resolve_subject_synonym_retry ниже).
    # Когда сюда пришли с готовым списком синонимных формулировок — ищем
    # ТОЛЬКО по ним, без повторного detect/первых слов (они уже провалились).
    if _syn_queries:
        queries = list(_syn_queries)

    # detect_category уже переводит сырое слово категории в каноническое
    # WB-название (обычно множественное число — «материнская плата» →
    # «Материнские платы»). object/all матчит почти ТОЧНОЕ имя категории:
    # «Материнская плата» (как обычно пишут в названии товара) даёт 0
    # результатов, «Материнские платы» — 1. Без этого шага единственное
    # число из сырого названия почти всегда мимо (проверено 14.07.2026).
    from services.card.category import detect_category
    detected = "" if _syn_queries else (detect_category(name) or (detect_category(raw_name) if raw_name else ""))
    via_hints = False
    if detected:
        hints = _WB_SUBJECT_HINTS.get(detected)
        if hints:
            via_hints = True
            # Наша категория заведомо не матчится в WB (см. _WB_SUBJECT_HINTS) —
            # сразу пробуем известно рабочее имя вместо гарантированного промаха.
            queries.extend(hints)
        else:
            queries.append(detected)

    if words and not _syn_queries:
        queries.append(words[0])
    if len(words) >= 2 and not _syn_queries:
        queries.insert(1 if detected else 0, " ".join(words[:2]))

    candidates: dict[int, dict] = {}
    exact_ids: set[int] = set()
    rejected_ids: set[int] = set()
    for q in queries:
        await _gather_subject_candidates(q, candidates, exclude, exact_ids)
        if len(candidates) >= 8:
            break

    guessed_types: list[str] = []
    via_guess = False
    if not candidates:
        # Название — «голый» артикул без слова-категории (модель+код, напр.
        # «NBLN V15 G5 IRL I3 8G 512G NOS» — по I3/8G/512G видно ноутбук,
        # но буквальный первый токен «NBLN» ничего не найдёт в object/all).
        # Просим LLM угадать РУССКИЙ тип товара по контексту и ищем по нему.
        # 10.08.2026: раньше промпт не передавал context_block (найденную
        # веб-исследованием информацию о товаре) вообще, хотя он уже был
        # посчитан выше — угадывание шло вслепую по одному голому названию.
        # Живой баг: «Аккумулятор для ИБП WBR GP1272 F2» (после нормализации
        # осталось просто «WBR GP1272 F2», без слова-категории) LLM дважды
        # угадала как «Видеокарты» — ровно из примеров в промпте ниже — хотя
        # context уже содержал «свинцово-кислотный аккумулятор... для ИБП».
        guess_resp = await llm.chat(
            "",
            "Определи ТИП товара (категорию) Wildberries по его артикульному названию.\n"
            f"Название: {name}\n{context_block}\n"
            "Обрати внимание на технические коды внутри названия (объём "
            "памяти, ГГц, разрешение, диагональ, тип процессора и т.п.) — "
            "они выдают тип устройства, даже если явного слова-категории нет. "
            "Если выше есть найденная информация о товаре — она надёжнее "
            "голого названия, используй её.\n"
            "Ответь МНОЖЕСТВЕННЫМ числом, как называются категории на "
            "Wildberries (например: Ноутбуки, Мыши, Материнские платы). "
            "Дай 3-5 вариантов РАЗНЫМИ словами-синонимами (например: "
            "приставка → Игровые консоли; батарейка → Элементы питания), "
            "по одному на строку, без пояснений.",
            max_tokens=60, enable_thinking=False,
        )
        await save_cost(user_id, "wb_create_subject_guess", response=guess_resp)
        # 23.07.2026: промпт явно просит "одна-две строки" (запасной вариант
        # на случай, если первая догадка не найдётся в справочнике), но раньше
        # бралась только первая строка — вторая догадка молча выбрасывалась.
        # Из-за этого сегодня «Считыватель магнитных карт Posiflex RA-101»
        # потребовал 4 ручных перезапуска с переформулировкой названия,
        # хотя нужная формулировка вполне могла быть у LLM во второй строке.
        # Теперь пробуем все предложенные варианты по очереди.
        raw_lines = [
            ln.strip().strip('"').strip("'")[:40]
            for ln in guess_resp.text.strip().splitlines()
        ]
        seen = set()
        for guessed_type in raw_lines:
            if not guessed_type or guessed_type.lower() in seen:
                continue
            seen.add(guessed_type.lower())
            guessed_types.append(guessed_type)
            queries.append(guessed_type)
            await _gather_subject_candidates(guessed_type, candidates, exclude, exact_ids)
            if candidates:
                via_guess = True
                break

    if not candidates:
        if _syn_queries is None:
            syn_terms = await _subject_synonym_terms(name, llm, user_id, context_block)
            if syn_terms:
                log.warning(f"resolve_subject: кандидатов нет — пробую синонимные формулировки: {syn_terms}")
                return await resolve_subject(
                    name, llm, user_id, exclude=exclude,
                    context=context, raw_name=raw_name, _syn_queries=syn_terms,
                )
        tried = " / ".join(queries)
        raise ValueError(
            f"категория WB не найдена по «{tried}»"
            + (f" (LLM предположила «{', '.join(guessed_types)}», тоже не нашлось)" if guessed_types else "")
            + " — переформулируй начало названия (первым словом — тип товара)"
        )

    if len(candidates) == 1:
        s = next(iter(candidates.values()))
        if s["subjectID"] in exact_ids and not via_guess:
            # Точное совпадение object/all — доверяем без LLM, как раньше.
            # NB: если кандидат пришёл из LLM-угадывания типа товара
            # (via_guess) — «точное совпадение» ничего не гарантирует, ведь
            # запрос сам был угадан той же LLM: она может дословно назвать
            # реальную категорию WB, попавшую в неё случайно (см. 10.08.2026
            # выше, «Видеокарты»). В этом случае экзамен на смысл обязателен.
            return s["subjectID"], s["subjectName"]
        # Единственный кандидат найден ТОЛЬКО нечётким поиском — не
        # принимаем слепо без смысловой проверки (см. _gather_subject_candidates
        # докстринг про "Филамент"/"Фламенко"; _prefix_coverage в
        # _fuzzy_subjects уже фильтрует общий класс "общая приставка ≠
        # смысловое сходство", но это доп. страховка для случаев, которые
        # тот эвристический порог не ловит). Просим LLM явно подтвердить
        # смысловое соответствие, без права "проглотить" неверный вариант
        # по умолчанию.
        verify_prompt = (
            "Категория Wildberries подходит этому товару по смыслу?\n"
            f"Товар: {display_name}\n"
            f"Категория-кандидат: {s['subjectName']} (раздел: {s.get('parentName', '?')})\n"
            f"{context_block}\n"
            "Кандидат найден НЕЧЁТКИМ текстовым сравнением (похож по буквам, "
            "не по смыслу) — например «Филамент» может ложно совпасть с "
            "«Фламенко». Если раздел (parentName) не связан по смыслу с "
            "товаром — это НЕТ, даже при буквенном сходстве названий.\n"
            "Ответь ТОЛЬКО одним словом: ДА или НЕТ."
        )
        verify_resp = await llm.chat("", verify_prompt, max_tokens=10, enable_thinking=False)
        await save_cost(user_id, "wb_create_subject_verify", response=verify_resp)
        if verify_resp.text.strip().lower().startswith("да"):
            return s["subjectID"], s["subjectName"]

        # Единственный отбракованный кандидат не должен сразу ронять всю
        # попытку — часто значит, что первичный поиск (по 1-2 первым словам
        # названия) просто не докопался до правильной категории, а не что
        # категории нет вовсе. Перед тем как сдаться — один более широкий
        # проход: по ВСЕМУ названию (не только первым словам) и с ослабленным
        # порогом нечёткости, исключая уже отбракованный вариант. Если
        # находится что-то ещё — отдаём на многовариантный выбор ниже (там
        # уже есть проверка на смысловое соответствие раздела, не бинарное
        # да/нет).
        broader = await asyncio.to_thread(_fuzzy_subjects, name, 10, 0.55)
        for cand in broader:
            if cand["subjectID"] != s["subjectID"] and (not exclude or cand["subjectID"] not in exclude):
                candidates[cand["subjectID"]] = cand
        candidates.pop(s["subjectID"], None)
        rejected_ids.add(s["subjectID"])
        if not candidates:
            if _syn_queries is None:
                syn_terms = await _subject_synonym_terms(name, llm, user_id, context_block)
                if syn_terms:
                    log.warning(f"resolve_subject: единственный кандидат забракован — пробую синонимы: {syn_terms}")
                    return await resolve_subject(
                        name, llm, user_id, exclude=(exclude or set()) | rejected_ids,
                        context=context, raw_name=raw_name, _syn_queries=syn_terms,
                    )
            raise ValueError(
                f"категория WB не найдена по «{' / '.join(queries)}» — единственный "
                f"нечёткий кандидат «{s['subjectName']}» не прошёл проверку LLM на "
                "смысловое соответствие, расширенный поиск тоже ничего не дал — "
                "переформулируй начало названия (первым словом — тип товара)"
            )

    # 10.08.2026: раньше выбор LLM из списка кандидатов принимался без единой
    # проверки (в отличие от одиночного нечёткого кандидата, который уже
    # проверялся выше) — один LLM-вызов с max_tokens=20 без рассуждения может
    # промахнуться так же, как промахивался одиночный нечёткий кандидат,
    # просто выбирая ИЗ списка, а не находя его заново. Добавлена та же
    # смысловая проверка ПОСЛЕ выбора: если раздел (parentName) не подходит —
    # не молча доверяем, а один раз пробуем выбрать заново из оставшихся
    # кандидатов.
    for attempt in range(2):
        # Точное (object/all) и нечёткое совпадение раньше выглядели в списке
        # одинаково — LLM не видела разницы в надёжности источника. Помечаем
        # явно.
        listing = "\n".join(
            f"{s['subjectID']} — {s['subjectName']} (раздел: {s.get('parentName', '?')})"
            + (" [точное совпадение по названию]" if s["subjectID"] in exact_ids else "")
            for s in candidates.values()
        )
        prompt = (
            "Выбери ОДНУ категорию (предмет) Wildberries для товара.\n"
            f"Товар: {display_name}{context_block}\n\nКандидаты:\n{listing}\n\n"
            "У каждого кандидата указан РАЗДЕЛ (parentName) — он должен "
            "смысловому соответствовать типу товара, а не только совпадать по "
            "словам с названием предмета (пример реальной ошибки: товар — "
            "Bluetooth-колонка, кандидат «Акустика для микроэлектроники» "
            "совпал по слову «акустика», но его раздел «Электрика» никак не "
            "связан с аудиотехникой — такого кандидата брать нельзя, даже если "
            "буквального совпадения по названию больше нет). Кандидат с пометкой "
            "«точное совпадение по названию» надёжнее нечёткого при прочих "
            "равных, но раздел всё равно должен подходить по смыслу.\n"
            "Ответь ТОЛЬКО числом subjectID из списка, без пояснений."
        )
        resp = await llm.chat("", prompt, max_tokens=20, enable_thinking=False)
        await save_cost(user_id, "wb_create_subject", response=resp)
        m = re.search(r"\d+", resp.text)
        sid = int(m.group()) if m else 0
        if sid not in candidates:
            # Раньше здесь молча брался "первый кандидат" из словаря — порядок
            # вставки зависит от того, какой из нескольких запросов (detected/
            # два слова/первое слово) отработал первым, и при сбое/пустом
            # ответе LLM (таймаут, rate-limit под нагрузкой батча) в карточку
            # могла уйти категория, вообще не связанная по смыслу с товаром —
            # так минимум 18 смартфонов Nova/Mate живьём ушли на WB под
            # категорией "Оперативная память" (батч 06.08.2026). Лучше
            # прервать создание карточки с понятной ошибкой, чем угадывать.
            #
            # 26.08.2026: живой случай (AirTag FineWoven Key Ring) — кандидаты
            # пришли из нечёткого поиска по английскому названию продавца и
            # оказались случайным буквенным мусором (клей/шторы/куклы/лодки/
            # пруд), LLM корректно отказалась выбирать ("Предоставленные
            # категории не подходят") — ответ без числа, ветка падала сюда
            # СРАЗУ, минуя синонимный ретрай ниже (который есть у соседнего
            # пути — единственный отбракованный кандидат, строки выше). Тот же
            # третий цветовой вариант этого товара находил категорию нормально
            # через синонимы — значит и здесь есть смысл попробовать, прежде
            # чем сдаваться.
            if _syn_queries is None:
                rejected_ids |= set(candidates)
                syn_terms = await _subject_synonym_terms(name, llm, user_id, context_block)
                if syn_terms:
                    log.warning(
                        f"resolve_subject: LLM отказалась выбрать кандидата "
                        f"(ответ: «{resp.text.strip()[:60]}») — пробую синонимы: {syn_terms}"
                    )
                    return await resolve_subject(
                        name, llm, user_id, exclude=(exclude or set()) | rejected_ids,
                        context=context, raw_name=raw_name, _syn_queries=syn_terms,
                    )
            tried = ", ".join(f"{s['subjectID']} — {s['subjectName']}" for s in candidates.values())
            raise ValueError(
                f"не удалось определить категорию WB для «{display_name}» — LLM не "
                f"выбрала ни одного кандидата из списка (ответ: «{resp.text.strip()[:60]}»). "
                f"Кандидаты были: {tried}. Переформулируй название или выбери категорию вручную."
            )

        picked = candidates[sid]
        if via_hints:
            # via_hints: кандидаты пришли из _WB_SUBJECT_HINTS — человек уже
            # заранее решил, что это приемлемые варианты для категорий без
            # точного соответствия у WB (напр. "Акустика" → три неидеальных
            # варианта колонок, WB не имеет отдельной "Сабвуферы" для дома).
            # Доп. смысловая проверка тут только вредит (живая регрессия:
            # "Активный сабвуфер для домашнего кинотеатра" отбраковывался бы,
            # хотя раньше осознанно уходил в "Колонки портативные" как лучший
            # из доступных вариантов).
            return sid, picked["subjectName"]

        verify_resp = await llm.chat(
            "",
            "Категория Wildberries подходит этому товару по смыслу?\n"
            f"Товар: {display_name}\n"
            f"Выбранная категория: {picked['subjectName']} (раздел: {picked.get('parentName', '?')})\n"
            f"{context_block}\n"
            "Если раздел (parentName) не связан по смыслу с товаром — это НЕТ, "
            "даже при буквенном сходстве названий.\n"
            "Ответь ТОЛЬКО одним словом: ДА или НЕТ.",
            max_tokens=10, enable_thinking=False,
        )
        await save_cost(user_id, "wb_create_subject_verify", response=verify_resp)
        if verify_resp.text.strip().lower().startswith("да"):
            return sid, picked["subjectName"]

        log.warning(
            f"resolve_subject: выбор «{picked['subjectName']}» для «{display_name}» "
            f"не прошёл повторную смысловую проверку (попытка {attempt + 1}) — "
            + ("пробую ещё раз из оставшихся кандидатов" if attempt == 0 and len(candidates) > 1
               else "кандидатов больше нет")
        )
        candidates.pop(sid, None)
        rejected_ids.add(sid)
        if not candidates:
            break

    # 14.08.2026: все кандидаты забракованы смысловой проверкой (живой кейс
    # PS5: «Игровые палатки»/«Рули игровые» верно отклонены, а «Игровые
    # консоли» по слову «приставка» не находились) — прежде чем сдаться,
    # один заход с синонимными формулировками типа товара.
    if _syn_queries is None:
        rejected_ids |= set(candidates)
        syn_terms = await _subject_synonym_terms(name, llm, user_id, context_block)
        if syn_terms:
            log.warning(f"resolve_subject: все кандидаты забракованы — пробую синонимы: {syn_terms}")
            return await resolve_subject(
                name, llm, user_id, exclude=(exclude or set()) | rejected_ids,
                context=context, raw_name=raw_name, _syn_queries=syn_terms,
            )

    raise ValueError(
        f"категория WB не найдена по «{' / '.join(queries)}» — ни один из "
        "предложенных кандидатов не прошёл смысловую проверку дважды — "
        "переформулируй начало названия (первым словом — тип товара)"
    )


# ── Справочники WB (кэши на процесс) ───────────────────────────────────

_brands_cache: dict[int, dict[str, str]] = {}


def _fetch_subject_brands(subject_id: int) -> dict[str, str]:
    """{lower(name): точное_имя_из_справочника_WB} — WB принимает бренд только
    в точном написании из своего справочника (Dell/ASUS падали, DELL/Asus — ок)."""
    if subject_id in _brands_cache:
        return _brands_cache[subject_id]
    r = _wb_session.get(
        f"{BASE}/api/content/v1/brands",
        headers=_headers(), params={"subjectId": subject_id}, timeout=20,
    )
    r.raise_for_status()
    brands = r.json().get("brands", [])
    mapping = {b["name"].strip().lower(): b["name"] for b in brands if b.get("name")}
    _brands_cache[subject_id] = mapping
    return mapping


def _resolve_brand(raw_brand: str, brand_map: dict[str, str]) -> str:
    if not raw_brand:
        return ""
    key = raw_brand.strip().lower()
    exact = brand_map.get(key, "")
    if exact:
        return exact
    # 19.07: тот же класс проблемы, что и с категориями — WB иногда пишет
    # бренд немного иначе (пробел/точка/дефис в названии, напр. "G.Skill" vs
    # "G Skill"), а не только регистром (регистр уже нормализован выше).
    # Нечёткий поиск только как подстраховка на форматирование — порог
    # высокий (0.9), т.к. цена ложного срабатывания (неверный бренд на
    # карточке) выше, чем у категории, где ошибку легко заметить и поправить.
    best_ratio, best_name = 0.0, ""
    for norm_name, real_name in brand_map.items():
        ratio = difflib.SequenceMatcher(None, key, norm_name).ratio()
        if ratio > best_ratio:
            best_ratio, best_name = ratio, real_name
    return best_name if best_ratio >= 0.9 else ""


_CHAR_ID_COLOR = 14177449
_valid_colors_cache: dict[str, str] | None = None
_CHAR_ID_SEASON = 18769
_valid_seasons_cache: set[str] | None = None
_valid_kinds_cache: dict[str, str] | None = None
# 24.08.2026: живой случай MB-MD128SA/EU — LLM написала «Страна
# производства: Корея», в справочнике WB официально «Республика Корея»
# (id 14177451, глобальный — рядом с _CHAR_ID_COLOR/_CHAR_ID_BARCODE в той же
# нумерации). Точное совпадение не находилось, WB отклонял всю карточку.
_CHAR_ID_COUNTRY = 14177451
_valid_countries_cache: dict[str, str] | None = None

_CHAR_ID_WARRANTY = 9623
_WARRANTY_VALUE = "12 месяцев"

# «Ставка НДС» (id 15001405) — как и гарантия, форсим фиксированное значение
# (правило магазина, 14.07.2026): без этого LLM отвечает на характеристику
# свободным текстом и может выдать любую ставку (напр. стандартные 20%
# вместо нужных продавцу 16%), см. карточку C7GF5ET#BJA.
_CHAR_ID_VAT = 15001405
_VAT_VALUE = "16"

# «Баркод» (id 14177453) — необязательное поле, но LLM его иногда всё равно
# заполняет правдоподобным на вид EAN, подцепленным из веб-контекста при
# поиске (описание конкурента/похожего товара). Штрихкод — глобально
# уникальный идентификатор на ВБ; если он совпадает с уже существующим
# товаром (не обязательно нашим), ВБ молча отклоняет всю карточку —
# cards/upload отвечает 200, карточка так и не появляется, error/list пуст
# (см. память wb_create_silent_upload_failures_2026-07-22, 23.07.2026:
# найдено на K1REV.B, значение 8809213766787). У нас нет надёжного
# источника реального штрихкода конкретно ЭТОГО SKU — не рискуем, не шлём.
_CHAR_ID_BARCODE = 14177453
# 12.08.2026: тот же класс поля, что «Баркод», просто под другим charcID —
# живой случай «PoE адаптер Ubiquiti U-PoE» (83924, 11.08.2026): LLM
# подтянула реальный UPC устройства из веб-контекста в «Код упаковки» и
# «NTIN» (эти два поля фильтр на _CHAR_ID_BARCODE не ловил), карточка ушла
# на молчаливый отказ ВБ ровно как раньше с K1REV.B. id проверены живьём по
# 5 разным категориям (Адаптеры/Роботы-пылесосы/Коммутаторы/Роутеры/
# Web-камеры) — одинаковые везде, в отличие от TN VED (см. TNVED fix
# 11.08.2026), поэтому хардкод без справочника на subject безопасен.
_CHAR_ID_PACKAGE_CODE = 15001706
_CHAR_ID_NTIN = 15003988
_GLOBAL_ID_CHAR_IDS = {_CHAR_ID_BARCODE, _CHAR_ID_PACKAGE_CODE, _CHAR_ID_NTIN}


def _fold_yo(s: str) -> str:
    return s.replace("ё", "е").replace("Ё", "Е")


def _fetch_valid_colors() -> dict[str, str]:
    """folded-lower имя → каноническое имя из справочника WB.
    13.08.2026: справочник почти всегда пишет «е» вместо «ё» (939 записей,
    ё встречается только в 6) — «тёмно-серый» от LLM (орфографически верно)
    не совпадал с «темно-серый» в справочнике, характеристика тихо терялась
    на любом тёмном/жёлтом/зелёном/чёрном оттенке. Отсюда фолдинг ё→е."""
    global _valid_colors_cache
    if _valid_colors_cache is not None:
        return _valid_colors_cache
    r = _wb_session.get(
        f"{BASE}/content/v2/directory/colors",
        headers=_headers(), params={"locale": "ru"}, timeout=20,
    )
    r.raise_for_status()
    mapping = {}
    for c in r.json().get("data", []):
        name = c.get("name")
        if name:
            mapping[_fold_yo(name.strip().lower())] = name
    _valid_colors_cache = mapping
    return mapping


# 14.08.2026 (живой кейс S7970 «золотой»): обиходное прилагательное и
# каноническое имя справочника WB нередко расходятся сильнее фаззи-порога
# 0.9 («золотой» vs «золотистый» — ratio 0.82, характеристика тихо
# терялась). Прямые алиасы для известных пар — только однозначные случаи.
_COLOR_RU_ALIASES = {
    "золотой": "золотистый",
    "золото": "золотистый",
    "серебряный": "серебристый",
    "серебро": "серебристый",
}


def _resolve_color(raw: str, color_map: dict[str, str]) -> str:
    """Как _resolve_brand: точное совпадение (после фолдинга ё→е), иначе
    нечёткий поиск с высоким порогом (0.9) — цена ложного срабатывания
    (неверный цвет на карточке) выше цены явного пропуска характеристики."""
    key = _fold_yo(raw.strip().lower())
    key = _COLOR_RU_ALIASES.get(key, key)
    exact = color_map.get(key, "")
    if exact:
        return exact
    best_ratio, best_name = 0.0, ""
    for norm_name, real_name in color_map.items():
        ratio = difflib.SequenceMatcher(None, key, norm_name).ratio()
        if ratio > best_ratio:
            best_ratio, best_name = ratio, real_name
    return best_name if best_ratio >= 0.9 else ""


def _fetch_valid_countries() -> dict[str, str]:
    """folded-lower имя → каноническое имя из справочника WB
    (/directory/countries). См. коммент у _CHAR_ID_COUNTRY."""
    global _valid_countries_cache
    if _valid_countries_cache is not None:
        return _valid_countries_cache
    r = _wb_session.get(
        f"{BASE}/content/v2/directory/countries",
        headers=_headers(), params={"locale": "ru"}, timeout=20,
    )
    r.raise_for_status()
    mapping = {}
    for c in r.json().get("data", []):
        name = c.get("name")
        if name:
            mapping[name.strip().lower()] = name
    _valid_countries_cache = mapping
    return mapping


def _resolve_country(raw: str, country_map: dict[str, str]) -> str:
    """Точное совпадение, иначе — раскрытие обиходного названия («Корея»,
    «Германия») до официальной формы справочника («Республика Корея» и
    т.п.) по вхождению всех слов запроса в слова названия. Обычный
    difflib-фаззи (как у цвета) тут бесполезен: «корея» и «республика
    корея» дают низкий ratio просто из-за разницы длины строк, а не
    похожести. При нескольких совпадениях (в т.ч. КНДР — «Корейская...») —
    берём короче название, но КНДР не совпадёт вовсе: у неё нет отдельного
    слова «корея» среди слов названия."""
    key = raw.strip().lower()
    exact = country_map.get(key, "")
    if exact:
        return exact
    key_words = set(key.split())
    candidates = [
        (norm_name, real_name) for norm_name, real_name in country_map.items()
        if key_words <= set(norm_name.split())
    ]
    if not candidates:
        return ""
    candidates.sort(key=lambda kv: len(kv[0]))
    return candidates[0][1]


def _fetch_valid_seasons() -> set[str]:
    """21.07.2026: живой случай на кроссовках — характеристика "Сезон"
    выдавала произвольный текст от LLM вместо одного из 4 реальных
    значений справочника WB. В отличие от /directory/colors, тут data —
    плоский список строк, а не список объектов с полем name."""
    global _valid_seasons_cache
    if _valid_seasons_cache is not None:
        return _valid_seasons_cache
    r = _wb_session.get(
        f"{BASE}/content/v2/directory/seasons",
        headers=_headers(), params={"locale": "ru"}, timeout=20,
    )
    r.raise_for_status()
    names = {s.strip().lower() for s in r.json().get("data", []) if s}
    _valid_seasons_cache = names
    return names


def _fetch_valid_kinds() -> dict[str, str]:
    """folded-lower имя → каноническое значение справочника пола WB
    (/directory/kinds). 14.08.2026: живой факт — справочник содержит ТОЛЬКО
    ['Мужской', 'Женский', 'Детский', 'Девочки', 'Мальчики'], значения
    «Унисекс» НЕ СУЩЕСТВУЕТ: карточка 82002 (рюкзак) отклонялась WB и с
    'Унисекс', и с 'унисекс' («Некорректное значение в характеристике Пол»).
    Старый зашитый _VALID_GENDERS = {мужской, женский, унисекс} был неверен —
    пропускал несуществующее значение и не знал про детские варианты."""
    global _valid_kinds_cache
    if _valid_kinds_cache is not None:
        return _valid_kinds_cache
    r = _wb_session.get(
        f"{BASE}/content/v2/directory/kinds",
        headers=_headers(), params={"locale": "ru"}, timeout=20,
    )
    r.raise_for_status()
    mapping = {}
    for name in r.json().get("data", []):
        if name:
            mapping[name.strip().lower()] = name.strip()
    _valid_kinds_cache = mapping
    return mapping


_charcs_cache: dict[int, list[dict]] = {}


_CHAR_ID_TNVED = 15004139  # ID поля "Код ТН ВЭД" — работает для большинства категорий
_tnved_cache: dict[int, list[dict]] = {}


def _find_tnved_char_id(chars_meta: list[dict]) -> int | None:
    """11.08.2026: у WB нет единого ID для поля ТН ВЭД на все категории —
    у большинства это charcID 15004139 ("Код ТН ВЭД"), но у некоторых
    (найдено на "Роботы-пылесосы") это отдельный charcID 15000001 с именем
    "ТНВЭД" (без пробела) — тот самый ID, который 04.08.2026 приняли за
    опечатку и глобально заменили на 15004139 (см.
    ai_bot_v2_filament_tnved_fixes_2026-08-04.md), хотя он валиден, просто
    для другой категории. Ищем по названию поля вместо захардкоженного ID,
    чтобы не пропускать категории с нестандартным именем поля."""
    for c in chars_meta:
        name = c["name"].strip().lower()
        if "тн вэд" in name or "тнвэд" in name.replace(" ", ""):
            return c["charcID"]
    return None


def _fetch_tnved_candidates(subject_id: int) -> list[dict]:
    """GET /content/v2/directory/tnved — официальный справочник WB, коды
    привязаны к subjectID и гарантированно проходят валидацию (в отличие от
    LLM-догадки, которая роняла всю карточку неверным кодом, см. крах 11.07).

    Раньше без ретраев — 429 вылетал необработанным исключением наружу и
    ронял ВСЮ карточку, хотя остальные этапы (фото, характеристики) уже были
    готовы. Тот же бэкофф, что в _find_card — 429/обрыв соединения
    переживаются, а не только успех/явная ошибка."""
    if subject_id in _tnved_cache:
        return _tnved_cache[subject_id]
    for attempt in range(4):
        try:
            r = _wb_session.get(
                f"{BASE}/content/v2/directory/tnved",
                headers=_headers(), params={"subjectID": subject_id, "locale": "ru"}, timeout=20,
            )
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
        data = r.json().get("data", [])
        _tnved_cache[subject_id] = data
        return data
    return []


def _fetch_subject_charcs(subject_id: int) -> list[dict]:
    if subject_id in _charcs_cache:
        return _charcs_cache[subject_id]
    r = _wb_session.get(
        f"{BASE}/content/v2/object/charcs/{subject_id}",
        headers=_headers(), params={"locale": "ru"}, timeout=20,
    )
    r.raise_for_status()
    data = r.json().get("data", [])
    _charcs_cache[subject_id] = data
    return data


# ── Промпт и преобразование характеристик ──────────────────────────────

def _build_chars_prompt(subject_name: str, chars: list[dict]) -> str:
    lines = [
        "Ты заполняешь характеристики карточки товара для Wildberries.",
        f"Категория: {subject_name}",
        "",
        "Заполни следующие характеристики на основе описания товара.",
        "Формат ответа — построчно: Название характеристики: значение",
        "Если характеристика неизвестна или неприменима — пропусти её (не пиши),",
        "КРОМЕ помеченных звёздочкой (*) — они обязательны, для них дай лучшую",
        "разумную оценку по контексту, а не пропускай.",
        "Числа без единиц измерения (только цифры).",
    ]
    # 12.08.2026: живой случай — батч из 9 карточек "Процессоры", 8 ушли
    # БЕЗ базовой/турбо частоты вовсе. У WB оба поля не обязательные
    # (required=False, без звёздочки) — общая инструкция выше ("пропусти,
    # если неизвестна") дала LLM повод пропустить их, хотя частота почти
    # всегда есть в самом названии товара продавца, просто в сжатой записи
    # ("3,7ГГц (4,7ГГц Turbo)", "1.5/2.0GHz (4.2/5.6GHz)" — для гибридных
    # Intel второе число обычно у производительных ядер). Раз это настолько
    # предсказуемо теряется — форсим для категории явным указанием, а не
    # надеемся на общее правило.
    if subject_name == "Процессоры":
        lines.append(
            "ОСОБО: тактовая частота (база/турбо) почти всегда есть в "
            "названии товара, даже в сжатой записи вида «3,7ГГц (4,7ГГц "
            "Turbo)» или «1.5/2.0GHz (4.2/5.6GHz)». Извлеки оба значения и "
            "заполни «Базовая частота процессора» и «Максимальная частота "
            "процессора в разгоне» — не пропускай их как неизвестные, если "
            "в названии есть хотя бы одно число с ГГц/GHz."
        )
    lines += ["", "Характеристики:"]
    for c in chars:
        unit = f" ({c['unitName']})" if c.get("unitName") else ""
        req = " *" if c.get("required") else ""
        lines.append(f"- {c['name']}{unit}{req}")
    lines += ["", "* — обязательные поля.", "", "Описание товара:"]
    return "\n".join(lines)


def _parse_chars_response(text: str) -> dict[str, str]:
    result = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        k, v = k.strip().lstrip("-• "), v.strip()
        if k and v:
            result[k] = v
    return result


def _num(v: str) -> float | None:
    digits = "".join(ch for ch in v.replace(",", ".") if ch.isdigit() or ch == ".")
    if not digits:
        return None
    try:
        return float(digits)
    except ValueError:
        return None


# Дефолт габаритов по классу предмета — единый 10×10×10/1кг WB отклонял
# как нереалистичный (см. крах 11.07 на телефонах/часах).
_FLEXIBLE_LENGTH_RE = re.compile(
    r"кабел|шнур|провод|трос|шланг|лент[аы]|верёвк|канат|шнурок|леск", re.IGNORECASE
)


_MAX_COMPAT_RE = re.compile(r"максимальн", re.IGNORECASE)


def _max_length_cm(values: dict[str, str]) -> float | None:
    """Наибольшая длина товара (см) среди характеристик с «длина» в имени,
    ИСКЛЮЧАЯ: упаковку (своё имя — «Длина упаковки (см)», проверяется отдельно
    через MAX_WEIGHT_KG/_build_dimensions); гибкие/сворачиваемые товары
    (кабели, шнуры, шланги — их «длина» не делает посылку крупной, в отличие
    от жёсткого карниза/удочки/лестницы); характеристики совместимости типа
    «Максимальная длина видеокарты»/«Максимальная длина БП» у корпусов ПК —
    это не размер самого товара, а слот под комплектующие (баг 23.07: корпус
    ATX «Максимальная длина видеокарты» 425 ММ был принят за 425 СМ длины
    самого корпуса — юнит не проверялся вообще, миллиметры считались как см).

    Единицы — метры, если в имени характеристики или в самом значении есть
    «м» не как часть «см»/«мм»; миллиметры — если явно «мм»/«mm» в имени;
    иначе считаем сантиметрами. Диапазоны («2,4-4,5 м») — берём все числа,
    возвращаем максимум."""
    best = None
    for key, val in values.items():
        key_lower = key.lower()
        if "упаков" in key_lower or not _LENGTH_KEY_RE.search(key):
            continue
        if _FLEXIBLE_LENGTH_RE.search(key) or _MAX_COMPAT_RE.search(key):
            continue
        is_mm = bool(re.search(r"\bмм\b|\bmm\b", key, re.IGNORECASE))
        is_meters = not is_mm and (
            bool(re.search(r"(?<![a-zа-я])м(?![a-zа-я])", key, re.IGNORECASE)) or
            bool(re.search(r"\d\s*м(?!м)(?![a-zа-я])", val, re.IGNORECASE))
        )
        for num_str in re.findall(r"\d+[.,]?\d*", val):
            num = float(num_str.replace(",", "."))
            cm = num * 100 if is_meters else (num / 10 if is_mm else num)
            if best is None or cm > best:
                best = cm
    return best


_SMALL_SUBJECTS = {787, 1514, 516, 2795}
_LARGE_SUBJECTS = {7696, 1267, 4066, 2922, 2823, 4496}
# 01.09.2026: «Мониторы» (2892) не попадали ни в small, ни в large — падали
# в общий medium-дефолт 25×18×10см/1кг (размер зарядки), хотя реальная
# коробка 24-27" монитора плоская и куда крупнее. Живой репро: в одном
# батче 4 из 5 карточек, упавших на дефолт, оказались именно мониторами
# (MSI MAG 274F получил 25×18×10/1кг). Не переиспользуем кубический
# large-дефолт (55×45×65/12кг — форма кресла/корпуса ПК, не подходит
# плоской коробке) — отдельный класс с формой, близкой к реально
# распознанным габаритам похожих мониторов (напр. Qmax 27" — 69×48×18/7.2кг).
_MONITOR_SUBJECTS = {2892}
_SIZE_DEFAULTS = {
    "small": (16, 8, 3, 0.3),
    "large": (55, 45, 65, 12.0),
    "monitor": (65, 42, 15, 5.5),
    "medium": (25, 18, 10, 1.0),
}


async def _record_dims_history(subject_id: int, length: float, width: float,
                                height: float, weight: float, source: str = "") -> None:
    """Копит РЕАЛЬНО найденные веб-поиском габариты по категории — база для
    самообучающейся медианы в _build_dimensions (01.09.2026, см. живой баг
    с мониторами: 25×18×10/1кг статичного дефолта на 27" монитор). Пишем
    только полный набор (все 4 значения сразу из одного источника) —
    иначе медиана могла бы смешать длину одного товара с высотой другого."""
    try:
        async with db_connect() as db:
            await db.execute(
                """INSERT INTO wb_dims_history (subject_id, length, width, height, weight, source)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (subject_id, length, width, height, weight, source),
            )
            await db.commit()
    except Exception as e:
        log.warning(f"wb_dims_history: не записалось (subject {subject_id}): {e}")


async def _get_dims_median(subject_id: int) -> tuple[float, float, float, float] | None:
    """Медиана накопленных реальных габаритов для категории. None — истории
    ещё нет (холодный старт, вызывающий код откатывается на статичный
    small/medium/large/monitor дефолт)."""
    async with db_connect() as db:
        cur = await db.execute(
            "SELECT length, width, height, weight FROM wb_dims_history WHERE subject_id = ?",
            (subject_id,),
        )
        rows = await cur.fetchall()
    if not rows:
        return None
    lens, wids, heis, wts = zip(*rows)
    return (statistics.median(lens), statistics.median(wids),
            statistics.median(heis), statistics.median(wts))


async def _build_dimensions(pack_values: dict[str, str], subject_id: int = 0) -> dict:
    length = _num(pack_values.get("Длина упаковки (см)", ""))
    width = _num(pack_values.get("Ширина упаковки (см)", ""))
    height = _num(pack_values.get("Высота упаковки (см)", ""))
    weight = _num(pack_values.get("Вес с упаковкой (кг)", ""))

    # Высота/толщина упаковки < 2 см физически нереальна для любого товара —
    # даже тонкий телефон/планшет в коробке требует места под зарядку, кабель,
    # документы. LLM иногда путает толщину самого товара с высотой коробки
    # (+20% к толщине телефона 0.8 см даёт ~1 см — обнаружено 15.07 на HONOR
    # X7e, WB сам пометил такие карточки isValid=False). В этом случае
    # откатываемся на дефолт вместо нереального значения.
    MIN_HEIGHT_CM = 2.0
    if height is not None and height < MIN_HEIGHT_CM:
        height = None

    if length and width and height and weight:
        # Полный реальный набор из веб-поиска — запоминаем как образец
        # категории на будущее, не дожидаясь отдельного фонового job'а.
        await _record_dims_history(subject_id, length, width, height, weight, source="packaging_search")
    else:
        # 01.09.2026: самообучающаяся медиана вместо мёртвого статичного
        # дефолта — сначала пробуем реальные образцы ЭТОЙ ЖЕ категории,
        # накопленные с прошлых карточек (свои же успешные находки), и
        # только если истории ещё нет вообще — откатываемся на грубую
        # классификацию small/large/monitor/medium.
        hist = await _get_dims_median(subject_id)
        if hist:
            d_len, d_wid, d_hei, d_wt = hist
        else:
            size_class = ("small" if subject_id in _SMALL_SUBJECTS
                          else "large" if subject_id in _LARGE_SUBJECTS
                          else "monitor" if subject_id in _MONITOR_SUBJECTS else "medium")
            d_len, d_wid, d_hei, d_wt = _SIZE_DEFAULTS[size_class]
        length = length or d_len
        width = width or d_wid
        height = height or d_hei
        weight = weight or d_wt

    return {
        "length": int(round(length)),
        "width": int(round(width)),
        "height": int(round(height)),
        "weightBrutto": round(weight, 3),
    }


# en→ru для цветов из parse_product_info: цвет в названии товара нередко
# написан по-английски ("Dreame Steam Straight White"), а справочник WB
# русский — без перевода _resolve_color гарантированно промахивается
# (живой кейс AA01A 14.08.2026).
_COLOR_EN_RU = {
    "white": "белый", "black": "черный", "red": "красный", "blue": "синий",
    "green": "зеленый", "grey": "серый", "gray": "серый", "silver": "серебристый",
    "gold": "золотистый", "pink": "розовый", "purple": "фиолетовый",
    "yellow": "желтый", "orange": "оранжевый", "brown": "коричневый",
    "beige": "бежевый", "turquoise": "бирюзовый", "violet": "фиолетовый",
}


def _to_wb_characteristics(values: dict[str, str], chars: list[dict],
                           product_color: str = "",
                           product_color_en: str = "") -> list[dict]:
    by_name = {c["name"]: c for c in chars}
    out = []
    for name, val in values.items():
        c = by_name.get(name)
        if c is None:
            # 12.08.2026: живой случай — _build_chars_prompt показывает поля
            # как "Название (ед.изм.)", и LLM иногда копирует единицу прямо
            # в свой ключ ответа ("Базовая частота процессора (ГГц): 3.7"
            # вместо "Базовая частота процессора: 3.7"). Точное совпадение
            # по имени тогда не находится, и значение ТИХО терялось — без
            # предупреждения в логах, баг годами мог резать любое числовое
            # поле с единицей измерения в любой категории, не только частоту
            # процессоров (обнаружено на 8/9 карточек "Процессоры" в одном
            # батче — сам факт совпадения по 8 из 9 показывает, что это не
            # редкая случайность, а системная привычка модели).
            stripped = re.sub(r"\s*\([^)]*\)\s*$", "", name).strip()
            if stripped != name:
                c = by_name.get(stripped)
        if not c or c.get("charcType") == 0:
            continue
        if c.get("existNamedField"):
            continue
        if c["charcID"] in _GLOBAL_ID_CHAR_IDS:
            continue
        if "тн вэд" in name.strip().lower() or "тнвэд" in name.strip().lower().replace(" ", ""):
            continue
        if name.strip().lower() == "пол":
            # 14.08.2026: значение сверяем с живым справочником /directory/kinds
            # и шлём его каноническую форму. LLM-ное «Унисекс» (частый и
            # логичный для аксессуаров ответ) в справочнике WB не существует —
            # характеристику честно пропускаем, а не шлём на гарантированный
            # reject всей карточки.
            canon_kind = _fetch_valid_kinds().get(val.strip().lower())
            if not canon_kind:
                log.warning(f"Характеристика «Пол»: значение {val!r} не найдено в справочнике WB — характеристика пропущена")
                continue
            val = canon_kind
        if c["charcID"] == _CHAR_ID_SEASON:
            if val.strip().lower() not in _fetch_valid_seasons():
                continue
        if c["charcID"] == _CHAR_ID_COLOR:
            resolved_color = _resolve_color(val, _fetch_valid_colors())
            if not resolved_color:
                log.warning(f"Характеристика «Цвет»: значение {val!r} не найдено в справочнике WB — характеристика пропущена")
                continue
            val = resolved_color
        if c["charcID"] == _CHAR_ID_COUNTRY:
            resolved_country = _resolve_country(val, _fetch_valid_countries())
            if not resolved_country:
                log.warning(f"Характеристика «Страна производства»: значение {val!r} не найдено в справочнике WB — характеристика пропущена")
                continue
            val = resolved_country
        # 21.07.2026: живой случай на кроссовках — контекст подтягивал текст
        # с AliExpress (китайский маркетплейс), LLM не перевела и не
        # отфильтровала "超轻, 缓震, 透气" в характеристику "Особенности обуви".
        # Иероглифы никогда не должны попасть на русскую карточку — отсекаем
        # независимо от того, откуда взялась причина.
        if any("一" <= ch <= "鿿" for ch in val):
            log.warning(f"Характеристика «{name}»: значение с иероглифами отброшено: {val!r}")
            continue
        if c["charcType"] == 4:
            num = "".join(ch for ch in val.replace(",", ".") if ch.isdigit() or ch == ".")
            if not num:
                continue
            try:
                num_val = float(num) if "." in num else int(num)
            except ValueError:
                continue
            out.append({"id": c["charcID"], "value": num_val})
        else:
            # 27.08.2026: живой случай (монитор LG 27MS500) — характеристика
            # «Соотношение сторон» = «16:9» (обычная строковая
            # характеристика, не title/description) уходила на WB
            # неочищенной, WB отклонял всю карточку как "Запрещено
            # указывать телефонные номера в поле Наименование" (то же
            # правило модерации, что и для 1000:1/title, но раньше
            # санитайзер применялся ТОЛЬКО к title/description, не к
            # значениям характеристик — де-факто любое поле с ":"/"/" между
            # цифрами могло сломать создание карточки). Тот же набор
            # санитайзеров, что и для title/description.
            val = _wb_sanitize_text(val)
            # 24.08.2026: живые случаи — CENTURY II 1050W/1200W/APX 650W
            # ("Область применения БП", maxCount=1, значение вида "Для игровых
            # ПК; Для офисных ПК; Для домашних ПК") и MB-MD128SA/EU
            # ("Назначение товара", maxCount=3, 6 значений через "; "). Разбор
            # живых cards/upload payload показал: собственный разделитель
            # многозначных ответов LLM для этих полей — "; " (точка с
            # запятой), НЕ запятая (запятая обычна ВНУТРИ одного значения,
            # напр. в описаниях). Раньше весь список всегда шёл ОДНОЙ строкой
            # (`[val_trunc]`) — WB сама считала "; "-элементы и резала
            # карточку: "имеет слишком много значений. Разрешено не более N".
            # Обрезаем список до лимита WB (maxCount из живого object/charcs),
            # а не гадаем какие из значений LLM правильные.
            max_count = c.get("maxCount") or 0
            if ";" in val:
                parts = [p.strip() for p in val.split(";") if p.strip()]
                if max_count:
                    parts = parts[:max_count]
                parts = [p if len(p) <= 95 else p[:92].rsplit(" ", 1)[0] + "…" for p in parts]
                if parts:
                    out.append({"id": c["charcID"], "value": parts})
                continue
            val_trunc = val if len(val) <= 95 else val[:92].rsplit(" ", 1)[0] + "…"
            out.append({"id": c["charcID"], "value": [val_trunc]})

    present_ids = {c["id"] for c in out}
    # Значения уже заполненных ЧИСЛОВЫХ полей по имени — доноры для
    # обязательных числовых двойников (см. кейс AA01A ниже).
    numeric_by_name: dict[str, float | int] = {}
    meta_by_id = {c["charcID"]: c for c in chars}
    for oc in out:
        m = meta_by_id.get(oc["id"])
        if m and m.get("charcType") == 4 and not isinstance(oc["value"], list):
            key = re.sub(r"\s*\([^)]*\)", "", m["name"]).strip().lower()
            numeric_by_name[key] = oc["value"]
    for c in chars:
        if c.get("required") and not c.get("existNamedField") and c["charcID"] not in present_ids:
            # Цвет — закрытый справочник WB; "нет данных" не пройдёт как
            # валидное значение (тот же класс бага, что нашли на "Пол"
            # 13.08.2026). 14.08.2026 (живой кейс AA01A "Dreame Steam
            # Straight White"): если Цвет обязателен, а LLM его не заполнила,
            # цвет обычно уже известен из САМОГО НАЗВАНИЯ товара
            # (parse_product_info) — берём его через тот же справочник
            # /directory/colors. Это не угадывание: цвет продавец написал сам.
            if c["charcID"] == _CHAR_ID_COLOR:
                color_map = _fetch_valid_colors()
                resolved = _resolve_color(product_color, color_map) if product_color else ""
                if not resolved and product_color_en:
                    ru = _COLOR_EN_RU.get(product_color_en.strip().lower(), "")
                    resolved = _resolve_color(ru, color_map) if ru else ""
                if resolved:
                    out.append({"id": c["charcID"], "value": [resolved]})
                else:
                    # лучше явная ошибка WB "поле не заполнено", чем
                    # гарантированный silent reject по невалидному значению
                    log.warning("Обязательный «Цвет» не заполнен: LLM не дала значение, из названия цвет не распознан")
                continue
            if c.get("name", "").strip().lower() == "пол":
                # 14.08.2026: «Пол» — закрытый справочник WB БЕЗ нейтрального
                # значения (/directory/kinds: только Мужской/Женский/Детский/
                # Девочки/Мальчики, «унисекс» не существует — проверено живьём
                # на карточке 82002). Универсального безопасного дефолта нет,
                # угадывать пол за товар нельзя — та же логика, что для Цвета
                # выше: лучше явная ошибка WB «поле не заполнено», чем
                # гарантированный reject по невалидному значению.
                continue
            if c["charcType"] == 4:
                # 14.08.2026 (кейс AA01A): WB считает 0 в обязательном
                # числовом поле НЕзаполненным ("missing required
                # characteristics") — нулевой фолбэк был бесполезен. Зато у
                # категорий часто есть пары-дубли ("Мощность" и "Мощность
                # устройства (Вт)") — LLM заполняет короткое имя, а
                # обязательным оказывается длинное. Ищем донора среди уже
                # заполненных числовых полей по вложенности имён (без
                # скобок-единиц); не нашёлся — поле честно не шлём, WB
                # ответит явным "заполни X", а не тихим мусором.
                name_l = re.sub(r"\s*\([^)]*\)", "", c["name"]).strip().lower()
                donor = next(
                    (v for k, v in numeric_by_name.items()
                     if k != name_l and (k in name_l or name_l in k)),
                    None,
                )
                if donor is not None:
                    log.info(f"Обязательное «{c['name']}»: взято из одноимённого заполненного поля → {donor}")
                    out.append({"id": c["charcID"], "value": donor})
                continue
            out.append({"id": c["charcID"], "value": ["нет данных"]})

    if any(c["charcID"] == _CHAR_ID_WARRANTY for c in chars):
        out = [c for c in out if c["id"] != _CHAR_ID_WARRANTY]
        out.append({"id": _CHAR_ID_WARRANTY, "value": [_WARRANTY_VALUE]})

    if any(c["charcID"] == _CHAR_ID_VAT for c in chars):
        out = [c for c in out if c["id"] != _CHAR_ID_VAT]
        out.append({"id": _CHAR_ID_VAT, "value": [_VAT_VALUE]})
    return out


# ── WB API вызовы ──────────────────────────────────────────────────────

def _wb_cards_upload_one(subject_id: int, variant: dict) -> dict:
    """20.07.2026: тот же пробел, что был у _wb_media_save (см. её докстринг) —
    найден живьём на Archer T1300U(EU): ConnectTimeout на САМ cards/upload
    (не на опрос появления, не на media/save) ронял create_one() необработанным
    исключением. Тот же ретрай-паттерн."""
    body = [{"subjectID": subject_id, "variants": [variant]}]
    last_err = ""
    for attempt in range(3):
        try:
            r = _wb_session.post(f"{BASE}/content/v2/cards/upload", headers=_headers(), json=body, timeout=60)
            return {"status": r.status_code, "body": r.text[:1000]}
        except requests.RequestException as e:
            last_err = str(e)
            log.warning(f"cards/upload (попытка {attempt + 1}/3): {e}")
            if attempt < 2:
                time.sleep(10)
    return {"status": 0, "body": f"сетевая ошибка после 3 попыток: {last_err[:200]}"}


def _wb_error_for(article: str) -> str:
    """Ищет ошибку создания конкретного vendorCode в cards/error/list.

    13.08.2026: живой случай (артикул 21QG003UFW) — WB реально вернул
    ошибку ("Поле Описание не должно содержать запрещенные символы: ® ™"),
    но эта функция читала ответ по несуществующей схеме
    (`r.json()["cards"]`) и всегда возвращала "" — карточка отклонена, а
    бот пишет "ошибок в error/list нет, проверь вручную". Реальная схема
    ответа — `{"data": {"items": [{"vendorCodes": [...], "errors":
    {vendorCode: [...]}}, ...]}}`, ошибка лежит в errors-словаре по
    vendorCode внутри каждого item, а не в плоском cards[]."""
    try:
        r = _wb_session.post(
            f"{BASE}/content/v2/cards/error/list",
            headers=_headers(),
            json={"cursor": {"limit": 100}, "order": {"ascending": False}},
            timeout=30,
        )
        for item in r.json().get("data", {}).get("items", []):
            errs = item.get("errors", {}).get(article)
            if errs:
                return "; ".join(errs)[:400]
    except Exception as e:
        return f"(cards/error/list недоступен: {e})"
    return ""


def _wb_media_save(nm_id: int, urls: list[str]) -> dict:
    """20.07.2026: раньше единственный requests.post без ретрая падал
    необработанным исключением при таймауте content-api.wildberries.ru
    (наблюдалось живьём — карточка создавалась, а весь create_one() всё
    равно вылетал с трейсбеком вместо аккуратного 'фото не прикрепились').
    Теперь — тот же ретрай-паттерн, что уже был у get/cards/list (см. ниже
    по файлу, попытки/пауза), исключения гасятся до статуса, а не наружу."""
    body = {"nmId": nm_id, "data": urls}
    last_err = ""
    for attempt in range(4):
        try:
            r = _wb_session.post(f"{BASE}/content/v3/media/save", headers=_headers(), json=body, timeout=30)
            return {"status": r.status_code, "body": r.text[:300]}
        except requests.RequestException as e:
            last_err = str(e)
            log.warning(f"[nmID {nm_id}] media/save (попытка {attempt + 1}/4): {e}")
            if attempt < 3:
                time.sleep(10)
    return {"status": 0, "body": f"сетевая ошибка после 4 попыток: {last_err[:200]}"}


# ── Группировка цветовых вариантов (20.07.2026) ────────────────────────
# Батчи часто содержат одну модель в нескольких расцветках (BELKIN Grip Case
# Pink/Sand/Sage, iPhone 17e Black/White/Soft Pink...) — раньше каждый цвет
# гонял ПОЛНЫЙ пайплайн (веб-поиск 30-90с + resolve_subject + LLM-вызовы),
# хотя различие — только слово цвета (идея записана ещё после батча JBL Tune).
# Ключ группы = название без цветовых слов; при совпадении ключа с уже
# обработанной позицией переиспользуются: категория, веб-контекст (с заменой
# слова цвета), справочники характеристик/брендов, ТН ВЭД. Описание и
# SEO-название генерируются НАСТОЯЩИМИ промптами заново (wb_context-режим
# generate_full_card — 0 поисковых запросов), фото ищутся на каждый цвет свои.

_COLOR_PHRASES_2W = [
    "soft pink", "sky blue", "blue mist", "eclipse black", "sunset orange",
    "cedar green", "matte black", "midnight black", "space gray", "space grey",
]
_COLOR_WORDS = {
    "black", "white", "pink", "sand", "sage", "lavender", "navy", "blue",
    "red", "green", "purple", "beige", "gray", "grey", "silver", "gold",
    "brown", "walnut", "cherry", "ebony", "denim", "orange", "yellow",
    "cream", "ivory", "graphite", "violet",
    "черный", "чёрный", "белый", "розовый", "синий", "голубой", "красный",
    "зеленый", "зелёный", "фиолетовый", "серый", "серебристый", "золотой",
    "коричневый", "бежевый", "оранжевый", "желтый", "жёлтый",
}


def _split_color_variant(name: str) -> tuple[str, str]:
    """('ключ модели без цвета', 'цветовая фраза') — ('', '') если цвета
    в названии нет. Ключ токенизированный: пунктуация/регистр не мешают."""
    raw_tokens = re.findall(r"[a-zа-яё0-9/+.-]+", name.lower())
    tokens = [t for t in raw_tokens if re.search(r"[a-zа-яё0-9]", t)]
    colors: list[str] = []
    rest: list[str] = []
    i = 0
    while i < len(tokens):
        pair = " ".join(tokens[i:i + 2])
        if pair in _COLOR_PHRASES_2W:
            colors.append(pair)
            i += 2
            continue
        if tokens[i] in _COLOR_WORDS:
            colors.append(tokens[i])
            i += 1
            continue
        rest.append(tokens[i])
        i += 1
    if not colors:
        return "", ""
    return " ".join(rest), " ".join(colors)


def _swap_color_words(text: str, old_phrase: str, new_phrase: str) -> str:
    """Замена слов старого цвета на новый по границам слов, без регистра
    ('Sand' не заденет 'SanDisk'). Лишние слова более длинной старой фразы
    ('soft pink' → 'sand') затираются первым словом новой."""
    out = text
    old_words = old_phrase.split()
    new_words = new_phrase.split()
    for old_w, new_w in zip(old_words, new_words):
        out = re.sub(rf"(?i)\b{re.escape(old_w)}\b", new_w, out)
    for extra in old_words[len(new_words):]:
        out = re.sub(rf"(?i)\b{re.escape(extra)}\b", new_words[0] if new_words else "", out)
    return out


# ── Один товар: контент → карточка → фото ──────────────────────────────

async def create_one(article: str, name: str, llm: LLMProvider, user_id: int,
                     send_text, group_cache: dict | None = None,
                     skip_quick_dims_check: bool = False,
                     weight_override_kg: float | None = None) -> dict:
    """Полный цикл создания одной карточки WB. Бросает ValueError с понятным
    текстом при любой ошибке этапа. group_cache — словарь на время батча для
    переиспользования поиска между цветовыми вариантами одной модели
    (см. _split_color_variant); None — поведение как раньше.

    skip_quick_dims_check (27.08.2026) — пропускает ранний грубый LLM-фильтр
    веса/длины по голому названию (см. ниже). Живой случай: "AV-ресивер
    Onkyo TX-RZ30" — грубая оценка стабильно (не разово) даёт ~30 кг вместо
    реальных ~14-17 кг (продавец сверил вручную), пайплайн падал на этом
    шаге дважды подряд. Точная проверка веса ПОСЛЕ generate_full_card (по
    реальным исследованным характеристикам, не по догадке) всё равно
    отработает и отсечёт настоящий крупногабарит — этот флаг только для
    ручного повторного запуска, когда оператор уже проверил реальный вес и
    знает, что грубая оценка ошиблась. НЕ используется в обычном /wb_batch.

    weight_override_kg (28.08.2026) — подменяет «Вес с упаковкой (кг)» из
    LLM-извлечённых характеристик перед финальной (точной) весовой проверкой.
    Живой случай: 90248 «Creality i7 Color Combo» — извлечение дало 45.0 кг
    (упёрлось в лимит), продавец вручную сверил реальный вес — 14.5 кг.
    Ручной override, а не автоматика: LLM-извлечению веса из веб-текста уже
    доверять нельзя без проверки (тот же класс риска, что и грубая оценка
    выше), поэтому значение задаёт оператор, а не код. НЕ используется в
    обычном /wb_batch."""
    _t_start = time.monotonic()

    # 28.07.2026: проверка дубля ПЕРВЫМ делом, до всего остального пайплайна.
    # Раньше дубль обнаруживался только на cards/upload — уже ПОСЛЕ полного
    # (дорогого) прохода resolve_subject + generate_full_card + поиска фото.
    # Стоило это живьём: при повторной отправке того же батч-файла (например,
    # после незапланированного рестарта бота посреди обработки) все уже
    # созданные позиции заново гоняли весь пайплайн только чтобы в конце
    # словить "vendor code is used in other cards". Теперь — один быстрый
    # запрос к WB до всего остального, дубль отсекается почти бесплатно.
    existing = await asyncio.to_thread(_find_card, article)
    if existing and existing.get("nmID"):
        raise ValueError(
            f"артикул «{article}» уже создан на WB — карточка УЖЕ существует "
            f"(nmID {existing['nmID']}). Пропускаю, пайплайн не запускался."
        )

    cache_key, color_phrase = ("", "")
    cached: dict | None = None
    if group_cache is not None:
        cache_key, color_phrase = _split_color_variant(name)
        if cache_key and color_phrase:
            cached = group_cache.get(cache_key)

    if cached:
        subject_id, subject_name = cached["subject_id"], cached["subject_name"]
        await send_text(
            f"Цветовой вариант базовой позиции {html.escape(cached['article'])} "
            f"(«{cached['color_phrase']}» → «{color_phrase}») — веб-поиск и "
            f"категория переиспользуются, фото и тексты свои."
        )
        ctx = _swap_color_words(cached["context"], cached["color_phrase"], color_phrase)
        result = await generate_full_card(
            name, llm, desc_only=False, need_characteristics=False,
            wb_context=ctx, wb_context_rich=True,
        )
    else:
        # 28.07.2026: грубая оценка веса/габаритов ОДНИМ дешёвым LLM-вызовом
        # до resolve_subject/generate_full_card (~5-6 LLM-вызовов суммарно) —
        # отсекает явно крупногабаритные/тяжёлые товары (мебель, крупная
        # бытовая техника, стройматериалы) до того как тратить самую дорогую
        # часть пайплайна. НЕ заменяет точную проверку ниже (та считает по
        # реальным характеристикам, не по догадке) — только ранний грубый
        # фильтр, с намеренным запасом (порог ×1.15) в пользу товара при
        # любой неопределённости, чтобы не рубить пограничные случаи, которые
        # точная проверка ниже могла бы пропустить.
        quick_resp = None if skip_quick_dims_check else await llm.chat(
            "", _QUICK_DIMS_PROMPT_TMPL.format(name=name),
            max_tokens=60, enable_thinking=False,
        )
        if quick_resp is not None:
            await save_cost(user_id, "wb_create_quick_dims", response=quick_resp)
        qtext = quick_resp.text.strip() if quick_resp is not None else "неизвестно"
        if "неизвестно" not in qtext.lower():
            qweight = qlength = None
            for line in qtext.splitlines():
                if ":" not in line:
                    continue
                k, _, v = line.partition(":")
                num = _num(v)
                if num is None:
                    continue
                k_low = k.strip().lower()
                if "вес" in k_low:
                    qweight = num
                elif "длина" in k_low:
                    qlength = num
            if qweight is not None and qweight > MAX_WEIGHT_KG * 1.15:
                raise ValueError(
                    f"предварительная оценка веса ~{qweight:.1f} кг заметно "
                    f"превышает лимит {MAX_WEIGHT_KG:.1f} кг — пропускаю до "
                    f"генерации контента (грубая оценка по названию, не точный "
                    f"расчёт — если ошибся, проверь вручную)"
                )
            if qlength is not None and qlength > MAX_PRODUCT_LENGTH_CM * 1.15:
                raise ValueError(
                    f"предварительная оценка длины ~{qlength:.0f} см заметно "
                    f"превышает лимит {MAX_PRODUCT_LENGTH_CM:.0f} см — пропускаю "
                    f"до генерации контента (грубая оценка по названию, не точный "
                    f"расчёт — если ошибся, проверь вручную)"
                )

        # 17.07.2026: было параллельно (asyncio.gather) — resolve_subject()
        # (1-2 LLM-вызова) и generate_full_card() (~4 LLM-вызова + веб-поиск)
        # формально не зависят друг от друга по входным данным.
        #
        # 10.08.2026: снова сделано ПОСЛЕДОВАТЕЛЬНО, по жалобе на дикие
        # промахи категории — категория определялась вслепую по сырому
        # названию продавца, хотя через пару секунд бот уже реально
        # ИССЛЕДУЕТ товар (веб-поиск, характеристики). resolve_subject
        # обычно завершается за секунды, а generate_full_card продолжается
        # ещё минуты — то есть подбор категории и так прячется внутри
        # гораздо более долгого исследования и добавляет по факту секунды на
        # карточку, не удваивает время. Взамен resolve_subject получает
        # result.product (нормализованное имя, не сырой ввод продавца) и
        # result.context (что бот реально узнал о товаре) для промптов
        # проверки/выбора — не только название.
        result = await generate_full_card(name, llm, desc_only=False, need_characteristics=False)
        subject_id, subject_name = await resolve_subject(
            result.product or name, llm, user_id, context=result.context, raw_name=name,
        )

    for i, resp in enumerate(result.llm_responses):
        await save_cost(user_id, "wb_create_card", response=resp,
                        exa_requests=result.exa_requests if i == 0 else 0)

    if cached:
        chars_meta, brand_map = cached["chars_meta"], cached["brand_map"]
    else:
        chars_meta = await asyncio.to_thread(_fetch_subject_charcs, subject_id)
        brand_map = await asyncio.to_thread(_fetch_subject_brands, subject_id)

    # 18.07: LLM извлекла реальный бренд, а его нет в справочнике выбранной
    # категории — сильный сигнал, что subject подобран неверно (см. случай
    # Edifier/"Акустика": кандидат совпал по слову, но раздел был не тот;
    # живой пример — «Контроллеры ИБП» не содержал бренд «Nintendo»,
    # переподобрало на «Геймпады»). Для цветового варианта пропускаем —
    # базовая позиция уже прошла проверку.
    # Раньше пробовали только ОДНУ альтернативную категорию — если бренда не
    # было и там, откатывались на пустой бренд, даже если он нашёлся бы в
    # третьем-четвёртом кандидате. Перебираем оставшихся кандидатов по
    # очереди (кап в 4 попытки — защита от разгона LLM-вызовов на артикулах
    # с длинным хвостом fuzzy-совпадений), пока не найдётся категория со
    # своим брендом в справочнике или кандидаты не кончатся (ValueError из
    # resolve_subject).
    if cached is None and result.brand and not _resolve_brand(result.brand, brand_map):
        tried_ids = {subject_id}
        for _ in range(4):
            try:
                retry_id, retry_name = await resolve_subject(
                    result.product or name, llm, user_id, exclude=tried_ids, context=result.context,
                    raw_name=name,
                )
            except ValueError:
                break
            tried_ids.add(retry_id)
            retry_chars = await asyncio.to_thread(_fetch_subject_charcs, retry_id)
            retry_brands = await asyncio.to_thread(_fetch_subject_brands, retry_id)
            if _resolve_brand(result.brand, retry_brands):
                await send_text(
                    f"⚠️ Категория «{html.escape(subject_name)}» не содержит "
                    f"бренд «{html.escape(result.brand)}» в справочнике — "
                    f"переподобрал категорию: «{html.escape(retry_name)}»."
                )
                subject_id, subject_name = retry_id, retry_name
                chars_meta, brand_map = retry_chars, retry_brands
                break

    await send_text(f"Категория: {html.escape(subject_name)} (id {subject_id})")

    wb_name_hint = f"Товар: {result.product}\nБренд: {result.brand}\n\n{result.context[:800]}"
    chars_prompt = _build_chars_prompt(subject_name, chars_meta)

    # wb_name_resp и chars_resp тоже не зависят друг от друга (оба берут уже
    # готовые result.context/chars_meta) — тоже параллелим.
    wb_name_resp, chars_resp = await asyncio.gather(
        llm.chat(wb_name_hint, WB_NAME_PROMPT, max_tokens=150, enable_thinking=False),
        # 01.09.2026: потолок размышления — без него этот вызов тратил
        # 2138-3629 токенов «мыслей» на 438-627 токенов ответа (замер по
        # категориям «Компьютеры»/«Мыши компьютерные»), а платим за них по
        # цене output. budget=512 даёт тот же результат в 2.7-4.8x дешевле;
        # полное отключение пробовали — теряет до 30% заполненных полей.
        llm.chat(result.context, chars_prompt, thinking_budget=512),
    )
    await save_cost(user_id, "wb_create_name", response=wb_name_resp)
    wb_title = truncate_title(wb_name_resp.text.strip().strip('"').strip("'").splitlines()[0])
    # 17.08.2026: та же ловушка, что и в описании (см. generator.py) — LLM
    # может утащить "TG" (Tempered Glass) прямо в название из сырого имени
    # продавца, WB отклоняет карточку как "упоминание мессенджера". Название
    # не проходит через _format_supplier_line, поэтому чистим отдельно здесь.
    wb_title = _normalize_spacing(_wb_sanitize_text(wb_title))
    await save_cost(user_id, "wb_create_chars", response=chars_resp)
    values = _parse_chars_response(chars_resp.text)
    pack_values = _parse_chars_response(result.packaging or "")
    if weight_override_kg is not None:
        pack_values["Вес с упаковкой (кг)"] = str(weight_override_kg)
    characteristics = _to_wb_characteristics(
        {**values, **pack_values}, chars_meta,
        product_color=result.color, product_color_en=result.color_en,
    )

    # 14.08.2026 (живой кейс AA01A/S5525, выпрямители): обязательное поле с
    # длинным именем («Мощность устройства (Вт)») LLM в общем проходе просто
    # пропускает, хотя значение есть в собранном контексте — и карточка
    # падает с "missing required characteristics". Вторая, прицельная
    # попытка: спрашиваем ТОЛЬКО недостающие обязательные поля. Один дешёвый
    # запрос, и только когда есть что спрашивать; «нет данных»-фолбэки
    # текстовых полей WB принимает, поэтому здесь всплывают только
    # числовые/справочные поля.
    _present_ids = {c["id"] for c in characteristics}
    _missing_req = [
        c for c in chars_meta
        if c.get("required") and not c.get("existNamedField")
        and c["charcID"] not in _present_ids
        and "тн вэд" not in c["name"].lower()
        and "тнвэд" not in c["name"].lower().replace(" ", "")
    ]
    if _missing_req:
        _miss_list = "\n".join(
            f"- {c['name']}" + (f" (в {c['unitName']})" if c.get("unitName") else "")
            for c in _missing_req
        )
        retry_resp = await llm.chat(
            result.context,
            "Из контекста выше извлеки ЗНАЧЕНИЯ ТОЛЬКО этих характеристик "
            f"товара «{result.product}»:\n{_miss_list}\n\n"
            "Формат ответа — строго по строке на характеристику:\n"
            "Название: значение\n"
            "Только факты из контекста. Если значения в контексте нет — "
            "ПРОПУСТИ эту строку целиком, не пиши её и ничего не придумывай.",
            max_tokens=300, enable_thinking=False,
        )
        await save_cost(user_id, "wb_create_chars_retry", response=retry_resp)
        retry_values = _parse_chars_response(retry_resp.text)
        if retry_values:
            log.info(f"Обязательные поля, добранные вторым проходом: {list(retry_values)}")
            characteristics = _to_wb_characteristics(
                {**values, **pack_values, **retry_values}, chars_meta,
                product_color=result.color, product_color_en=result.color_en,
            )

    tnved_code = cached["tnved_code"] if cached else ""
    tnved_char_id = _find_tnved_char_id(chars_meta)
    if not tnved_code and tnved_char_id is not None:
        # 04.08.2026: официальный справочник WB (content/v2/directory/tnved) —
        # приоритетный источник, как и раньше. Если он недоступен (сетевая
        # ошибка) или для subjectID нет ни одного кандидата, откатываемся на
        # свою библиотеку wb_tnved_codes (services/tnved.py) — копится
        # автоматически из прошлых успешных резолвов через сам справочник.
        try:
            tnved_candidates = await asyncio.to_thread(_fetch_tnved_candidates, subject_id)
        except Exception as e:
            log.warning(f"ТН ВЭД: справочник WB недоступен для subject {subject_id}: {e}")
            tnved_candidates = []
        if len(tnved_candidates) == 1:
            tnved_code = tnved_candidates[0]["tnved"]
        elif len(tnved_candidates) > 1:
            codes = ", ".join(c["tnved"] for c in tnved_candidates)
            # 04.08.2026: к этому моменту характеристики карточки (values) уже
            # определены (chars_resp отработал выше) — передаём их LLM, а не
            # только название товара. Коды ТН ВЭД внутри одной WB-категории
            # часто расходятся именно по материалу/техническим параметрам
            # (напр. "Кроссовки" — 44 разных кода по материалу верха), так что
            # реальные извлечённые характеристики дают LLM то, чего не было в
            # одном названии товара.
            chars_hint = "\n".join(f"{k}: {v}" for k, v in values.items() if v) or "(характеристики не извлечены)"
            # 28.08.2026: официальный справочник WB отдаёт только голые коды
            # (без текстовых описаний позиций — проверено живьём), так что LLM
            # выбирает вслепую по характеристикам без семантической опоры.
            # Подмешиваем прошлые собственные решения для ЭТОЙ категории —
            # wb_tnved_candidates копится по факту применения (см. ниже),
            # description там — не официальный текст, а характеристики
            # товара, для которого этот код был выбран раньше. Отфильтровано
            # по текущему живому набору кодов справочника, чтобы не тащить
            # примеры для кода, которого в этой выдаче уже нет.
            lib_examples = [
                c for c in await get_wb_tnved_candidates(subject_id)
                if c["tnved"] in {c2["tnved"] for c2 in tnved_candidates} and c["description"]
            ]
            history_hint = (
                "\n\nРанее для товаров этой категории уже применялись коды:\n" +
                "\n".join(f"Код {c['tnved']} — товар с характеристиками: {c['description']}"
                          for c in lib_examples)
            ) if lib_examples else ""
            tnved_resp = await llm.chat(
                "",
                f"Товар: {result.product}\nКатегория WB: {subject_name}\n"
                f"Характеристики товара:\n{chars_hint}\n\n"
                f"Доступные коды ТН ВЭД для этой категории: {codes}"
                f"{history_hint}\n\n"
                "Выбери ОДИН наиболее подходящий код для этого конкретного товара, "
                "опираясь на характеристики выше (материал, тип, технические параметры — "
                "коды внутри одной категории WB часто различаются именно по ним). "
                "Ответь ТОЛЬКО кодом, без пояснений.",
                max_tokens=20, enable_thinking=False,
            )
            await save_cost(user_id, "wb_create_tnved", response=tnved_resp)
            picked = "".join(ch for ch in tnved_resp.text if ch.isdigit())
            valid_codes = {c["tnved"] for c in tnved_candidates}
            tnved_code = picked if picked in valid_codes else tnved_candidates[0]["tnved"]
            # Копим контекст решения в библиотеку кандидатов — на будущее
            # (следующий товар этой категории увидит это как history_hint
            # выше), не только в старую одноячеечную wb_tnved_codes ниже
            # (та хранит один код на subject_id и перезаписывается каждым
            # новым товаром — теряет, какой товар получил какой код).
            await set_wb_tnved_candidates(
                subject_id, subject_name, [(tnved_code, chars_hint)],
                source=f"справочник WB, применено к артикулу {article}",
            )

        if tnved_code:
            await set_wb_tnved(subject_id, subject_name, tnved_code, note="из справочника WB")
        else:
            # 14.08.2026: справочник WB пуст для этой категории — пер-товарный
            # выбор из своей библиотеки КАНДИДАТОВ (коды + официальные описания
            # с классификаторов), а не один замороженный код на категорию:
            # внутри одной WB-категории коды часто различаются по материалу/
            # техпризнакам (рюкзаки 4202 91/92 по материалу поверхности,
            # кроссовки — десятки кодов по верху). Старая wb_tnved_codes
            # (один код) остаётся вырожденным случаем для однородных категорий.
            # Если кандидатов ещё нет — веб-поиск классификаторов сеет их один
            # раз, дальше категория работает из библиотеки без сети.
            # Автоприменение вместо «подсказки в чат» одобрено пользователем
            # 14.08: ТН ВЭД не критичен для продажи на WB, цена редкой ошибки
            # ниже цены ручной проверки каждой карточки.
            lib_candidates = await get_wb_tnved_candidates(subject_id)
            if not lib_candidates:
                tnved_code = await get_wb_tnved(subject_id) or ""
            if not tnved_code and not lib_candidates:
                try:
                    found = await search_tnved_candidates(subject_name)
                except Exception as e:
                    log.warning(f"search_tnved_candidates({subject_name!r}) failed: {e}")
                    found = []
                # Заземление: в кандидаты идут только коды, найденные на ≥2
                # независимых доменах-классификаторах — одиночное упоминание
                # на странице-списке слишком часто оказывается кодом соседней
                # позиции (порог согласован с прежним search_tnved_code).
                found = [c for c in found if len(c.get("domains", [])) >= 2]
                if found:
                    await set_wb_tnved_candidates(
                        subject_id, subject_name,
                        [(c["tnved"], c["description"]) for c in found],
                        source="веб-поиск классификаторов",
                    )
                    lib_candidates = found
                    await send_text(
                        f"📦 Код ТН ВЭД для «{html.escape(subject_name)}» подобран "
                        f"веб-поиском классификаторов (справочник WB пуст) — "
                        f"кандидаты сохранены в библиотеку."
                    )
            if not tnved_code and lib_candidates:
                if len(lib_candidates) == 1:
                    tnved_code = lib_candidates[0]["tnved"]
                else:
                    chars_hint = "\n".join(f"{k}: {v}" for k, v in values.items() if v) or "(характеристики не извлечены)"
                    cand_text = "\n\n".join(
                        f"Код {c['tnved']}:\n{(c['description'] or '(описание отсутствует)')[:600]}"
                        for c in lib_candidates
                    )
                    tnved_resp = await llm.chat(
                        "",
                        f"Товар: {result.product}\nКатегория WB: {subject_name}\n"
                        f"Характеристики товара:\n{chars_hint}\n\n"
                        f"Кандидаты кодов ТН ВЭД с официальными описаниями:\n{cand_text}\n\n"
                        "Выбери ОДИН код, чьё официальное описание подходит этому "
                        "конкретному товару (материал, тип, технические параметры). "
                        "Не придумывай код вне списка. Ответь ТОЛЬКО кодом, без пояснений.",
                        max_tokens=20, enable_thinking=False,
                    )
                    await save_cost(user_id, "wb_create_tnved", response=tnved_resp)
                    picked = "".join(ch for ch in tnved_resp.text if ch.isdigit())
                    valid_codes = {c["tnved"] for c in lib_candidates}
                    if picked in valid_codes:
                        tnved_code = picked
                    else:
                        # LLM не выбрала валидный код (нет различающего признака
                        # в характеристиках) — берём «прочие»-код позиции, если
                        # он есть среди кандидатов (широкая корзина заведомо в
                        # тему), иначе первый по порядку — та же стратегия, что
                        # у пути официального справочника выше.
                        other = next(
                            (c["tnved"] for c in lib_candidates
                             if "проч" in (c["description"] or "").lower()),
                            "",
                        )
                        tnved_code = other or lib_candidates[0]["tnved"]
            if not tnved_code:
                await send_text(
                    f"⚠️ Код ТН ВЭД не проставлен — не нашёлся ни в справочнике "
                    f"WB, ни в библиотеке, ни веб-поиском для категории "
                    f"«{html.escape(subject_name)}» (id {subject_id}), заполни вручную."
                )
                await save_wb_tnved_miss(user_id, article, subject_id, subject_name, name)
        if tnved_code:
            characteristics.append({"id": tnved_char_id, "value": [tnved_code]})

    dimensions = await _build_dimensions(pack_values, subject_id)

    if dimensions["weightBrutto"] > MAX_WEIGHT_KG:
        raise ValueError(
            f"вес в упаковке {dimensions['weightBrutto']:.1f} кг превышает "
            f"лимит {MAX_WEIGHT_KG:.1f} кг — карточку не создаём"
        )

    max_len_cm = _max_length_cm(values)
    if max_len_cm is not None and max_len_cm > MAX_PRODUCT_LENGTH_CM:
        raise ValueError(
            f"длина товара {max_len_cm:.0f} см превышает лимит "
            f"{MAX_PRODUCT_LENGTH_CM:.0f} см (не упаковка) — карточку не создаём"
        )

    # Успешно догенерировали базовую позицию с цветом в названии — запоминаем
    # дорогие куски для следующих цветов той же модели в этом батче.
    if group_cache is not None and cached is None and cache_key and color_phrase:
        group_cache[cache_key] = {
            "article": article,
            "subject_id": subject_id,
            "subject_name": subject_name,
            "chars_meta": chars_meta,
            "brand_map": brand_map,
            "context": result.context,
            "color_phrase": color_phrase,
            "tnved_code": tnved_code,
        }

    # Фото — строго последовательно относительно остального пайплайна
    # (GPU и браузеры; лимит браузеров держит BROWSER_SEMAPHORE)
    effective_n = _MANY_PHOTOS_N if result.category in _MANY_PHOTOS_CATEGORIES else 3
    effective_max_candidates = (
        _MANY_PHOTOS_MAX_CANDIDATES if result.category in _MANY_PHOTOS_CATEGORIES else 10
    )
    photos = await find_product_images(
        result.product, n=effective_n, max_candidates=effective_max_candidates, brand=result.brand,
        color=result.color, color_en=result.color_en,
        category=result.category, llm=llm, vision_validate=True,
        article=article,
    )

    # Фото не нашлись по описательному названию — пробуем ещё раз по
    # артикулу/P/N напрямую (17.07): реальный код производителя часто
    # индексируется точнее описательного названия (дистрибьюторские сайты,
    # Google по SKU) и не требует угадывания LLM — это ДЕТЕРМИНИРОВАННЫЙ шаг,
    # его пробуем раньше LLM-угадывания ниже, а не вместо него.
    # 20.07.2026 (живой батч БП): "чисто числовой" article — это внутренний
    # SKU дистрибьютора (напр. "194750"), а не код производителя — у него
    # нет собственного смысла в вебе. Поймано живьём: поиск по "194750"
    # зацепил золотое кольцо на kaspi.kz (число совпало с ЕЁ внутренним ID),
    # Vision это пропустил (сама по себе валидная фото-карточка, просто не
    # того товара) — карточка блока питания чуть не ушла с фото кольца.
    # Буквенно-числовые article (модели вроде "RB962UiGS-5HacT2HnT") этой
    # проблемы не имеют — оставляем фолбэк только для них.
    if not photos and article and not article.isdigit():
        await send_text(f"Фото не найдено по исходному названию — пробую по артикулу «{html.escape(article)}»")
        photos = await find_product_images(
            article, n=effective_n, max_candidates=effective_max_candidates, brand=result.brand,
            color=result.color, color_en=result.color_en,
            category=result.category, llm=llm, vision_validate=True,
            article=article,
        )

    if not photos and not result.brand:
        # Название — сырой дамп характеристик без реального бренда (OEM/ODM
        # артикул вроде «NBLN V15» или «UMA Ultra 7 255U ...») — по нему
        # физически нет фото в сети. Пробуем угадать РЕАЛЬНОЕ рыночное имя
        # (часто такие устройства — ребренд известной линейки, определяется
        # по чипсету/сериям в спеках) и поискать ещё раз по нему.
        guess_resp = await llm.chat(
            "",
            "Дан технический дамп характеристик устройства без явного бренда "
            f"в начале названия:\n{result.product}\n\n"
            "Определи, под каким РЕАЛЬНЫМ рыночным именем (бренд + серия/модель) "
            "это устройство скорее всего продаётся — по чипсету, процессору, "
            "названию серии в самих характеристиках (например если это чип "
            "Intel Core Ultra в форм-факторе G1i — вероятно это ребренд "
            "существующей линейки ноутбуков известного бренда).\n"
            "Если распознать реальный бренд невозможно — ответь ровно 'НЕТ'.\n"
            "Иначе ответь ОДНОЙ строкой: Бренд Модель (кратко, для поиска "
            "фото в интернете), без пояснений."
        )
        await save_cost(user_id, "wb_create_photo_guess", response=guess_resp)
        guessed = guess_resp.text.strip().strip('"').strip("'").splitlines()[0][:80]
        if guessed and guessed.upper() != "НЕТ":
            await send_text(f"Фото не найдено по исходному названию — пробую по предполагаемому «{html.escape(guessed)}»")
            photos = await find_product_images(
                guessed, n=effective_n, max_candidates=effective_max_candidates,
                color=result.color, color_en=result.color_en,
                category=result.category, llm=llm, vision_validate=True,
                article=article,
            )

    photo_urls = []
    for img_bytes in photos:
        url = await upload_image(img_bytes, ext="jpg")
        if url:
            photo_urls.append(url)

    resolved_brand = _resolve_brand(result.brand, brand_map)
    if result.brand and not resolved_brand:
        await send_text(f"⚠️ Бренд «{html.escape(result.brand)}» не найден в справочнике WB — карточка уйдёт без бренда.")

    # 17.08.2026: _format_supplier_line/wb_title чистят только свои куски —
    # это не спасает, когда САМА LLM пишет "TG" в прозе описания (для неё
    # это законная часть модели, "Wintek ... V909-B TG формата..."), а WB
    # всё равно режет "Запрещено указывать мессенджеры в поле Описание".
    # Живой случай: артикул 193925 упал именно на этом, УЖЕ после фикса
    # supplier-line/title. Чистим финальный текст целиком, а не источник.
    # НЕ через _normalize_spacing — она схлопывает \n в пробел, а тут
    # многоабзацный текст, переносы абзацев нужно сохранить.
    wb_description = _wb_sanitize_text(result.description)
    wb_description = re.sub(r"[ \t]+", " ", wb_description)

    variant = {
        "vendorCode": article,
        "title": wb_title,
        "description": truncate_description(wb_description),
        "brand": resolved_brand,
        "dimensions": dimensions,
        "characteristics": characteristics,
    }

    # 23.07.2026: временная диагностика тихих сбоев cards/upload (200 OK, но
    # карточка так и не появляется, error/list пуст) — см. память
    # wb_create_silent_upload_failures_2026-07-22, диагноз "сравнить payload"
    # так и не был доведён до конца. Логируем весь payload на КАЖДУЮ карточку
    # (успешную и нет), чтобы при следующем таком случае было с чем сравнивать
    # постфактум — без этого лога данные терялись безвозвратно.
    log.info(f"[{article}] cards/upload payload: subjectID={subject_id} "
              + json.dumps(variant, ensure_ascii=False))

    resp = await asyncio.to_thread(_wb_cards_upload_one, subject_id, variant)
    if resp["status"] != 200:
        raise ValueError(f"cards/upload HTTP {resp['status']}: {resp['body'][:300]}")

    # Ждём появления карточки (создание асинхронное на стороне WB).
    # 20.07.2026: раньше — 6 × фикс. 20с, т.е. минимум 20с ожидания даже когда
    # WB создаёт карточку за 3-5с. Ступенчатые паузы ловят быстрый случай на
    # первых секундах, худший случай (сумма ~135с) не хуже прежних 120с.
    _POLL_DELAYS = (5, 5, 10, 15, 20, 30, 50)
    nm_id = None
    for attempt, delay in enumerate(_POLL_DELAYS):
        await asyncio.sleep(delay)
        try:
            card = await asyncio.to_thread(_find_card, article)
        except Exception as e:
            log.warning(f"[{article}] get/cards/list (попытка {attempt + 1}/{len(_POLL_DELAYS)}): {e}")
            continue
        if card and card.get("nmID"):
            nm_id = card["nmID"]
            break

    if not nm_id:
        err = await asyncio.to_thread(_wb_error_for, article)
        raise ValueError(f"карточка не появилась на WB. {('Ошибка WB: ' + err) if err else 'Ошибок в error/list нет — проверь позже вручную.'}")

    # 17.07.2026: раньше статус media/save просто дописывался цифрой
    # ("media/save 500") — успех (200) и провал (500) выглядели почти
    # одинаково в потоке сообщений, легко пропустить. Теперь явно.
    media_status = ""
    if photo_urls:
        media = await asyncio.to_thread(_wb_media_save, nm_id, photo_urls)
        if media["status"] == 200:
            media_status = f", фото {len(photo_urls)} шт. — прикреплены"
        else:
            media_status = (
                f", ⚠️ фото {len(photo_urls)} шт. НЕ прикрепились "
                f"(ошибка WB {media['status']}: {media['body'][:100]})"
            )
    else:
        media_status = ", фото не найдены"

    from utils.billing import save_wb_card_created
    from utils import hw_stats
    await save_wb_card_created(
        user_id, article, nm_id, wb_title, subject_name,
        photos_count=len(photo_urls) if photo_urls else 0,
        elapsed_sec=time.monotonic() - _t_start,
        hw=await hw_stats.snapshot(),
    )

    return {
        "nm_id": nm_id,
        "title": wb_title,
        "subject": subject_name,
        "summary": f"nmID {nm_id}, «{wb_title}», {len(characteristics)} характеристик{media_status}",
    }


async def explain_error(article: str, error: Exception) -> str:
    """Человеческая расшифровка типовых ошибок WB API для /wb_create
    (17.07.2026) — чтобы не разбирать сырой JSON вручную каждый раз.
    Возвращает готовый текст для сообщения пользователю (сам текст ошибки
    внутри уже есть — вызывающий код не должен дублировать str(error))."""
    text = str(error)

    if "vendor code is used in other cards" in text:
        # Почти всегда значит, что карточка УЖЕ реально создана (более
        # ранний прогон/дубль в списке), а не настоящий сбой — проверяем
        # прямо, вместо того чтобы просто показывать пугающий JSON.
        try:
            card = await asyncio.to_thread(_find_card, article)
        except Exception:
            card = None
        if card and card.get("nmID"):
            return (
                f"ℹ️ Артикул «{article}» уже занят — карточка на самом деле "
                f"УЖЕ существует (nmID {card['nmID']}). Это не сбой, а дубль "
                f"в списке — ничего делать не нужно.\n{text}"
            )
        return (
            f"⚠️ Артикул «{article}» уже занят на WB, но карточка с ним не "
            f"находится через API — возможно, в архиве или на модерации. "
            f"Стоит проверить вручную в личном кабинете.\n{text}"
        )

    if "media/save" in text or "Ошибка сохранения товара" in text:
        return (
            "⚠️ Карточка создалась, но WB не смог сохранить фото (серверная "
            f"ошибка на их стороне) — нужно будет повторить прикрепление позже.\n{text}"
        )

    if "cards/upload HTTP 500" in text or "Internal server error" in text:
        return f"⚠️ Временная ошибка сервера WB при создании карточки — стоит повторить позже.\n{text}"

    if "429" in text or "Too Many Requests" in text:
        return f"⚠️ WB ограничил частоту запросов — подождите пару минут и повторите.\n{text}"

    if "категория WB не найдена" in text:
        return f"⚠️ Не смог определить категорию WB по названию — переформулируй начало (первым словом — тип товара).\n{text}"

    if "вес в упаковке" in text and "лимит" in text:
        return f"⚠️ Товар слишком тяжёлый для правил магазина — карточку не создаём.\n{text}"

    return text
