# -*- coding: utf-8 -*-
"""Бесплатная (без единого LLM-вызова) генерация инфографики/rich-content
по УЖЕ существующей живой карточке WB (01.09.2026).

Идея: title/description/характеристики/категория уже готовы на карточке —
не нужно заново спрашивать LLM про features/tips/slogan, всё это можно
вытащить из того, что уже есть:
  - features  — сопоставляем PRIORITY_CHARS-лейблы (services/card/category.py)
    с реальными характеристиками карточки по пересечению слов (fuzzy, без сети)
  - slogan    — первое предложение description, обрезанное до ~8 слов
  - tips      — ещё 1-2 содержательных предложения из description
  - category  — subjectName с самой карточки (уже верная, раз карточка живая)
  - фон       — уже бесплатный локальный градиент (USE_GEMINI_BG=False)

Результат сохраняется локально (не заливается на карточку автоматически —
сначала посмотреть глазами, качество не проверялось визуально).
"""
import asyncio
import re
import sys
from pathlib import Path

import pymorphy3

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

_MORPH = pymorphy3.MorphAnalyzer()

from services.wb_content import _find_card, get_wb_card_data
from services.card.category import PRIORITY_CHARS
from services.image import make_infographic, make_richcontent
from services.search.searxng import SearxngSearch
from services.llm.gemini_provider import GeminiProvider
from utils.billing import save_cost

from config import settings
_ADMIN_ID = settings.admin_id
_fallback_search = SearxngSearch()
_fallback_llm = GeminiProvider()

_FEATURE_LINE_RE = re.compile(r"^\s*[-*•]?\s*([^:]{2,30}):\s*(.+?)\s*$")

OUT_DIR = ROOT / "data" / "free_infographic_test"
OUT_DIR.mkdir(parents=True, exist_ok=True)

_STOPWORDS = {"для", "и", "с", "на", "не", "по", "или", "без", "от", "до", "в", "к", "из"}
_WEAK_TOKENS = {"тип", "вид", "количество", "сжо"}
_VOLUME_IN_TITLE_RE = re.compile(r"(\d+)\s*(ГБ|GB|ТБ|TB)\b", re.IGNORECASE)
_SENTENCE_END_RE = re.compile(r"[.!?…](?=\s|$)")


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[а-яё]{3,}", text.lower()) if w not in _STOPWORDS}


def _match_char_value(label: str, characteristics: list[dict]) -> str | None:
    """Сопоставляет лейбл инфографики (напр. 'ПРОЦЕССОР') реальной
    характеристике карточки по пересечению слов — без LLM, без сети."""
    label_toks = _tokens(label)
    if not label_toks:
        return None
    best, best_score = None, 0
    for c in characteristics:
        name = str(c.get("name", ""))
        name_l = name.lower()
        score = len(label_toks & _tokens(name))
        if label.lower() in name_l or name_l in label.lower():
            score += 2
        if score > best_score:
            best_score, best = score, c
    if best_score == 0 or best is None:
        return None
    val = best.get("value")
    if isinstance(val, list):
        val = ", ".join(str(v) for v in val)
    return str(val).strip() if val not in (None, "", []) else None


_META_CHAR_KEYWORDS = (
    "гарантийный", "страна", "штрихкод", "ставка ндс", "комплектация",
    "сертификат", "декларац", "код тру", "тру ", "возрастная группа",
    "дата окончания", "дата изготовления", "срок годности", "артикул",
    "код по", "укпг", "хрупкост", "код упаковки", "ntin", "икпу",
)
_PLACEHOLDER_VALUES = {"не указано", "не заполнено", "нет данных", "не указан", "не применимо", "n/a", "-"}

_NUMERIC_RE = re.compile(r"^-?\d+([.,]\d+)?$")
_UNIT_SUFFIX_RULES = (
    # 28.09.2026: RAM/гарнитуры шли голыми числами ("32", "5600", "1.1") —
    # специфичные правила выше общих, порядок важен (первое совпадение).
    ("объем оперативной памяти", " ГБ"),
    ("объем одного модуля", " ГБ"),
    ("частота оперативной памяти", " МГц"),
    ("напряжение", " В"),
    ("максимальная температура", " °C"),
    ("импеданс", " Ом"),
    ("диагональ", '"'),
    ("частота обновления", " Гц"),
    ("частота процессора", " МГц"),
    ("тактовая частота", " МГц"),
    ("время отклика", " мс"),
    ("яркость", " кд/м²"),
    ("контрастность", ":1"),
    ("емкость аккумулятора", " мАч"),
    ("ёмкость аккумулятора", " мАч"),
    ("аккумулятор", " мАч"),
    ("оперативная память", " ГБ"),
    ("встроенная память", " ГБ"),
    ("камера", " Мп"),
    ("разрешение камеры", " Мп"),
    ("вес товара", " г"),
    ("dpi", " DPI"),
)


# 28.09.2026: лейблы PRIORITY_CHARS для RAM ("ОБЪЁМ", "ТИП/ЧАСТОТА") не
# матчились с реальными WB-названиями (ё vs е, "/" в лейбле) — инфографика
# планки DDR5 8GB показала tRCD/кол-во модулей/макс.температуру вместо
# объёма и частоты. Переопределение только для инфографики, общий
# PRIORITY_CHARS (services/card/category.py) не трогаем.
_INFOGRAPHIC_LABELS: dict[str, list[str]] = {
    "Оперативная память": ["ОБЪЕМ ПАМЯТИ", "ЧАСТОТА ПАМЯТИ", "НАПРЯЖЕНИЕ", "ПОДСВЕТКА"],
    # "Гарнитуры" в PRIORITY_CHARS нет вовсе — плашки брались первыми по
    # порядку, а WB отдаёт характеристики в нестабильном порядке.
    "Гарнитуры": ["ТИП СОЕДИНЕНИЯ", "ШУМОПОДАВЛЕНИЕ", "ВИД НАУШНИКОВ", "ИМПЕДАНС"],
}


def _apply_unit(name_l: str, value: str) -> str:
    """Числовые значения WB-характеристик хранятся без единиц измерения
    (напр. 'Диагональ' = '27', без 'дюймов') — на инфографике голое число
    в большой плашке выглядит незаконченным. Единица определяется по
    названию характеристики, известному заранее (по PRIORITY_CHARS)."""
    if not _NUMERIC_RE.match(value.strip()):
        return value
    # Чувствительность микрофона/наушников на WB — отрицательные дБ (-45);
    # у мышей "чувствительность" бывает в DPI, поэтому только для минуса.
    if "чувствительность" in name_l and value.strip().startswith("-"):
        return f"{value} дБ"
    for kw, suffix in _UNIT_SUFFIX_RULES:
        if kw in name_l:
            return f"{value}{suffix}"
    return value


def _is_meta_char(name: str) -> bool:
    """Служебные характеристики, непригодные для инфографики. ТН ВЭД
    проверяем ОБОИМИ вариантами написания — у части категорий (напр.
    мониторы, роботы-пылесосы, см. charcID 15000001) WB называет поле
    'ТНВЭД' слитно, без пробела, обычный 'тн вэд' его не ловит."""
    name_l = name.lower()
    if "тн вэд" in name_l or "тнвэд" in name_l.replace(" ", ""):
        return True
    return any(k in name_l for k in _META_CHAR_KEYWORDS)


def _is_placeholder_value(value: str) -> bool:
    """'Не указано' и подобное — WB считает это НЕпустым значением
    (характеристика формально заполнена этой строкой), но по факту данных
    нет: живой пример — наушники HyperX, где 'Возрастная группа: детская'
    и 'Дата окончания сертификата: не указано' попали в инфографику как
    настоящие specs."""
    return value.strip().lower() in _PLACEHOLDER_VALUES


def build_features(category: str, characteristics: list[dict], limit: int = 4) -> list[tuple[str, str]]:
    labels = _INFOGRAPHIC_LABELS.get(category) or PRIORITY_CHARS.get(category, [])
    features: list[tuple[str, str]] = []
    used_ids = set()
    for label in labels:
        # "тип"/"вид"/"количество" — служебные слова, встречающиеся почти в
        # любом названии характеристики; без фильтра одно такое слово может
        # "выиграть" совпадение у случайной неродственной характеристики
        # (живой баг 03.09: "ТИП ПОДСВЕТКИ СЖО" на карточке-компоненте без
        # подсветки утянул "Тип компонента для СЖО"). "сжо" — общий
        # аббревиатурный суффикс категории СЖО, та же проблема локально.
        label_toks = _tokens(label) - _WEAK_TOKENS
        best, best_score, best_val = None, 0, None
        for c in characteristics:
            if id(c) in used_ids:
                continue
            name_l = str(c.get("name", "")).lower()
            if _is_meta_char(name_l):
                continue
            v = c.get("value")
            if isinstance(v, list):
                v = ", ".join(str(x) for x in v)
            if v in (None, "", []) or _is_placeholder_value(str(v)):
                continue
            score = len(label_toks & (_tokens(name_l) - _WEAK_TOKENS))
            if label.lower() == name_l:
                score += 3
            elif label.lower() in name_l or name_l in label.lower():
                score += 1
            if score > best_score:
                best_score, best, best_val = score, c, v
        # score==1 значит ровно одно случайное общее слово БЕЗ вхождения
        # строк друг в друга — недостаточно надёжно (живой баг 03.09:
        # "ЕМКОСТЬ АККУМУЛЯТОРА" на карточке без этого поля утянула по
        # общему слову "аккумулятора" совсем другую характеристику "Время
        # работы от аккумулятора" и подписала часы как мАч). Настоящие
        # совпадения почти всегда дают >=2 (два общих слова, либо один
        # токен + бонус за вхождение подстроки) — порог отсекает случайные
        # склейки, не трогая обычные однословные лейблы вроде "ПРОЦЕССОР",
        # которые получают +3 за точное совпадение строки целиком.
        if best is not None and best_score >= 2:
            features.append((label, _apply_unit(str(best.get("name", "")).lower(), str(best_val).strip())))
            used_ids.add(id(best))
        if len(features) >= limit:
            break

    if len(features) < limit:
        # Категория не покрыта PRIORITY_CHARS (или мало совпало) — берём
        # первые содержательные характеристики карточки как есть.
        for c in characteristics:
            if id(c) in used_ids:
                continue
            name = str(c.get("name", ""))
            if _is_meta_char(name):
                continue
            v = c.get("value")
            if isinstance(v, list):
                v = ", ".join(str(x) for x in v)
            if v in (None, "", []) or _is_placeholder_value(str(v)):
                continue
            features.append((name.upper(), _apply_unit(name.lower(), str(v).strip())))
            used_ids.add(id(c))
            if len(features) >= limit:
                break
    return features[:limit]


async def build_features_combined(
    category: str, characteristics: list[dict], title: str, brand: str,
    article: str = "", limit: int = 4, min_before_fallback: int = 2,
) -> list[tuple[str, str]]:
    """build_features() + веб-фоллбэк, когда родных характеристик МАЛО, а
    не только когда их НОЛЬ. Живой баг 03.09.2026: у Flash-накопителя на
    WB реально заполнена только характеристика "Цвет" — build_features()
    вернул 1 непустой результат, и старое условие `if not features`
    фоллбэк не запускало, хотя самое важное для флешки (объём в GB, он же
    прямо в названии товара) осталось за кадром. Дополняет, не заменяет —
    родная характеристика "Цвет" остаётся, к ней добавляются недостающие
    слоты — сначала бесплатным разбором объёма памяти из названия (у
    флешек/карт памяти он почти всегда там, напр. "TS16GJF700 16GB"), и
    только если после этого всё ещё мало — платным веб-поиском."""
    features = build_features(category, characteristics, limit=limit)
    if len(features) >= min_before_fallback:
        return features
    existing_labels = {lbl for lbl, _ in features}

    m = _VOLUME_IN_TITLE_RE.search(title)
    if m and "ОБЪЕМ" not in existing_labels and len(features) < limit:
        unit = "ТБ" if m.group(2).lower().startswith(("t", "т")) else "ГБ"
        features.append(("ОБЪЕМ", f"{m.group(1)} {unit}"))
        existing_labels.add("ОБЪЕМ")
        if len(features) >= min_before_fallback:
            return features

    extra = await build_features_fallback(
        title, brand, category, article=article, limit=limit - len(features),
    )
    for lbl, val in extra:
        if lbl not in existing_labels and len(features) < limit:
            features.append((lbl, val))
            existing_labels.add(lbl)
    return features


async def build_features_fallback(
    title: str, brand: str, category: str, article: str = "", limit: int = 4,
) -> list[tuple[str, str]]:
    """Фолбэк для карточек, у которых build_features() вернул пусто (карточка
    реально бедна характеристиками на WB — 0.90% карточек бота, 19/2102 на
    01.09.2026). Бесплатный SearxNG-поиск спецификации товара в сети + один
    короткий вызов Gemini 2.5 Flash (thinking_budget=0, см. память
    ai_bot_v2_llm_cost_optimization_2026-09-01) вытаскивает до `limit` пар
    лейбл/значение. Оценочная стоимость ~$0.001 за карточку (расчёт
    01.09.2026, память ai_bot_v2_free_infographic_tool_2026-09-01).
    Возвращает [] если поиск/парсинг не дали ничего — вызывающий код должен
    сам решить, показывать ли инфографику без features."""
    query = f"{brand} {title} характеристики".strip()
    try:
        resp = await _fallback_search.search_text(query, max_results=3, max_chars=1500)
    except Exception as e:
        print(f"[fallback features] поиск не удался: {e}")
        return []
    context = "\n\n".join(f"{r.title}\n{r.text}" for r in resp.results if r.text)
    if not context.strip():
        return []

    prompt = (
        f"Товар: {title} (бренд: {brand or 'неизвестен'}, категория: {category or 'неизвестна'}).\n\n"
        f"Ниже — тексты со страниц о товаре:\n{context[:3000]}\n\n"
        f"Выпиши до {limit} самых важных технических характеристик этого товара "
        "СТРОГО в формате «ЛЕЙБЛ: значение», по одной на строку, без вступлений "
        "и пояснений. Лейбл — короткое существительное в именительном падеже "
        "(напр. «ЁМКОСТЬ», «МОЩНОСТЬ»), значение — короткое (число+единица или "
        "1-3 слова). Если по тексту нельзя уверенно определить характеристику — "
        "пропусти её, не выдумывай."
    )
    try:
        llm_resp = await _fallback_llm.chat("", prompt, enable_thinking=False)
    except Exception as e:
        print(f"[fallback features] Gemini не ответил: {e}")
        return []
    await save_cost(_ADMIN_ID, "free_infographic_features_fallback", response=llm_resp)

    features: list[tuple[str, str]] = []
    for line in llm_resp.text.splitlines():
        m = _FEATURE_LINE_RE.match(line)
        if not m:
            continue
        label, value = m.group(1).strip(), m.group(2).strip()
        if label and value:
            features.append((label.upper(), value))
        if len(features) >= limit:
            break
    return features


def _split_sentences(text: str) -> list[str]:
    """25.09.2026: сплитим сначала по строкам, а уже внутри каждой строки —
    по знакам конца предложения. Живой баг (Bloody G575P): первая строка
    описания — короткий заголовок без точки в конце ('Система поддержки
    "Парящее крыло"'), вторая строка — настоящее предложение. Старая версия
    гоняла regex по всему тексту разом — раз в заголовке нет точки, он
    склеивался с следующим предложением в ОДНО, и sentences[1:] (источник
    tips для rich-content) оказывался пустым, хотя реального текста было
    достаточно. Разбивка по строкам ПЕРЕД поиском точек не даёт заголовку
    без пунктуации проглотить следующее предложение."""
    text = text.strip()
    if not text:
        return []
    result = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        parts, start = [], 0
        for m in _SENTENCE_END_RE.finditer(line):
            parts.append(line[start:m.end()].strip())
            start = m.end()
        if start < len(line):
            parts.append(line[start:].strip())
        result.extend(p for p in parts if p)
    return result


def _trim_words(text: str, max_words: int) -> str:
    words = text.split()
    if len(words) <= max_words:
        return text
    cut = " ".join(words[:max_words]).rstrip(".,;:—- ")
    return cut + "."


def _strip_spec_header(description: str) -> str:
    """Первая строка описания у этого проекта ИНОГДА — техническая шапка вида
    '<Товар> [Black, IPS, 1920x1080@120Hz, ...]' (см. промпт DESCRIPTION_PROMPT
    в services/card/generator.py), не настоящая маркетинговая проза. Реальный
    текст тогда начинается после первого пустого перевода строки — без этого
    'первое предложение' обрывается прямо посреди списка характеристик
    в скобках (нет точки внутри шапки, поэтому наивный сплиттер полз до
    первой реальной точки уже ВНУТРИ первого абзаца).

    25.09.2026: живой баг (EarPods A1748) — первый абзац описания оказался
    ОБЫЧНОЙ прозой на 3 полноценных предложения, а не технической шапкой,
    но старая версия всё равно безусловно выбрасывала его целиком (раз есть
    "\\n\\n" — значит шапка), оставляя для tips всего 1 предложение из 4.
    Отличаем шапку от настоящего абзаца просто: у шапки нет точки в конце
    (это список характеристик, не предложение), у прозы — есть. Если первый
    блок сам оканчивается на .!?… — это уже нормальный текст, не трогаем его."""
    parts = description.split("\n\n", 1)
    if len(parts) <= 1:
        return description
    first = parts[0].strip()
    if _SENTENCE_END_RE.search(first):
        return description
    return parts[1].strip()


_LABEL_STOPWORDS = _STOPWORDS | {
    "его", "это", "эти", "эта", "тем", "как", "что", "чтобы", "также", "более",
    "самым", "самой", "очень", "будет", "будут", "может", "могут", "которые",
    "которая", "который", "имеет", "обеспечивает", "позволяет", "благодаря",
    "устройство", "модель", "изделие", "товар",
}


def _lemmatize_label(word: str) -> str:
    """Ставит слово в именительный падеж ед.ч. (pymorphy3), чтобы бейдж не
    наследовал падеж из середины предложения ('КОНТРАСТНОСТИ' вместо
    'КОНТРАСТНОСТЬ', 'АККУМУЛЯТОРА' вместо 'АККУМУЛЯТОР'). Составные
    англ/рус токены вида 'IPS-матрица' лемматизируем только по русской
    части после дефиса — pymorphy не разбирает латиницу."""
    if "-" in word and re.search(r"[A-Za-z]", word):
        prefix, _, rest = word.rpartition("-")
        if re.search(r"[а-яё]", rest, re.IGNORECASE):
            return f"{prefix}-{_MORPH.parse(rest)[0].normal_form}"
        return word
    if not re.search(r"[а-яё]", word, re.IGNORECASE):
        return word
    return _MORPH.parse(word)[0].normal_form


def _keyword_label(sentence: str, category: str) -> str:
    """Заголовок бейджа/тезиса без LLM: сперва пробуем реальный PRIORITY_CHARS-
    лейбл категории, если его тема упомянута в предложении; иначе берём самое
    длинное содержательное слово — обычно это и есть техническая деталь
    предложения ('IPS-матрица', 'аккумулятор' и т.п.), лемматизируем его в
    именительный падеж и капитализируем. Не идеально на нестандартных
    текстах (составные термины, омонимы), но лучше generic
    'ГЛАВНОЕ'/'ПРЕИМУЩЕСТВО' или падеж прямиком из середины фразы."""
    sent_toks = _tokens(sentence)
    for label in PRIORITY_CHARS.get(category, []):
        if _tokens(label) & sent_toks:
            return label
    words = re.findall(r"[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё-]{3,}", sentence)
    candidates = [w for w in words[:10] if w.lower() not in _LABEL_STOPWORDS]
    if not candidates:
        return "ОСОБЕННОСТЬ"
    # Существительное на бейдже читается естественнее глагола/прилагательного
    # в инфинитиве после лемматизации ('ПОДДЕРЖИВАЕТ' -> 'ПОДДЕРЖИВАТЬ' режет
    # глаз) — если среди кандидатов есть хоть одно, берём самое длинное из них.
    nouns = [w for w in candidates if re.search(r"[а-яё]", w, re.IGNORECASE)
             and "NOUN" in _MORPH.parse(w)[0].tag]
    pick = max(nouns, key=len) if nouns else max(candidates, key=len)
    return _lemmatize_label(pick).upper()


_BENEFIT_STEMS = (
    "позволя", "обеспеч", "защища", "экономи", "увеличива", "снижа",
    "повыша", "облегча", "удобн", "легк", "надежн", "надёжн", "быстр",
    "долговечн", "эффективн", "комфортн", "безопасн", "гаранти", "сохраня",
    "улучша", "продлева", "упроща", "предотвраща", "избежать", "экономит",
    "благодаря", "не требу",
)


def _benefit_score(sentence: str) -> int:
    """Сколько маркеров выгоды/пользы в предложении — 'эта единица содержит
    тонер' (нейтральное определение) получает 0, 'позволяет сэкономить...'
    получает 1+. Используется, чтобы блок 'ПРЕИМУЩЕСТВА' в rich-content
    показывал реальные выгоды, а не первые попавшиеся справочные фразы из
    описания (02.09.2026: живой пример на тонере читался как энциклопедия,
    не как продающий текст)."""
    low = sentence.lower()
    return sum(1 for stem in _BENEFIT_STEMS if stem in low)


_TRIM_DANGLING_POS = {"ADJF", "PRTF", "CONJ", "PREP", "COMP"}


def _smart_trim_sentence(text: str, max_words: int = 18) -> str:
    """Обрезка ДЛИННОГО предложения без фейковой точки на предлоге —
    02.09.2026: снятие обрезки совсем (голое предложение целиком) привело к
    тому, что рендер (_fit_fixed_size, фиксированный размер шрифта на всю
    колонку) сам резал длинные предложения посреди слова многоточием
    ('...в корпусе и…') — визуально куда хуже, чем аккуратная обрезка
    заранее. Обрезаем до max_words, снимаем висящий предлог/союз/
    прилагательное с конца (как в badge_phrase), ставим «…» только если
    реально обрезали."""
    words = text.split()
    if len(words) <= max_words:
        return text
    words = words[:max_words]

    def _dangling(w: str) -> bool:
        clean = w.strip(".,;:—- ").lower()
        if not clean or clean in _STOPWORDS:
            return True
        if re.search(r"[а-яё]", clean):
            tag = _MORPH.parse(clean)[0].tag
            if any(pos in tag for pos in _TRIM_DANGLING_POS):
                return True
        return False

    while words and _dangling(words[-1]):
        words.pop()
    return " ".join(words).rstrip(",.;:—- ") + "…"


_TIPS_REWRITE_PROMPT = (
    "Ниже — 1-2 предложения из описания товара (категория: {category}), "
    "каждое описывает одно реальное преимущество/выгоду для покупателя.\n\n"
    "{sentences}\n\n"
    "Для КАЖДОГО предложения сделай продающую пару строго в формате "
    "«МЕТКА: короткая фраза», по одной паре на строку, без нумерации и "
    "пояснений:\n"
    "- МЕТКА — 1-2 слова заглавными буквами, называющие саму выгоду "
    "(напр. «IPX7», «ГАРАНТИЯ», «УНИВЕРСАЛЬНОСТЬ»), не «ПРЕИМУЩЕСТВО 1».\n"
    "- Фраза — 3-8 слов, живым продающим языком передаёт суть предложения "
    "(не копирует его дословно целиком), обычным регистром.\n"
    "Не выдумывай факты, которых нет в предложении."
)


async def _rewrite_tips_llm(
    sentences: list[str], category: str, llm,
) -> list[tuple[str, str]] | None:
    """Пересобирает 1-2 предложения в короткие продающие пары метка/фраза
    через один дешёвый LLM-вызов (thinking_budget=0) — см. живой референс
    (карточка Kingston SD, LLM-пайплайн): 'IPX7' / 'Защита от воды, пыли и
    рентгена', а не нарезка сырого предложения. None — LLM недоступна/сбой,
    вызывающий код откатывается на бесплатный вариант."""
    if not sentences:
        return None
    prompt = _TIPS_REWRITE_PROMPT.format(
        category=category or "не указана",
        sentences="\n".join(f"{i+1}. {s}" for i, s in enumerate(sentences)),
    )
    try:
        resp = await llm.chat("", prompt, enable_thinking=False)
    except Exception as e:
        print(f"[tips rewrite] Gemini не ответил: {e}")
        return None
    await save_cost(_ADMIN_ID, "free_infographic_tips_rewrite", response=resp)

    out: list[tuple[str, str]] = []
    for line in resp.text.splitlines():
        line = line.replace("*", "").replace("#", "")
        # 16.09.2026: промпт просит "без нумерации", но Gemini иногда всё
        # равно нумерует строки ("1. ПРОИЗВОДСТВО: ...") — раньше только
        # "-"/"•" срезались как маркер списка, цифра+точка утекала прямо в
        # МЕТКУ на картинке (живой случай: карточка <артикул>, плашка
        # "1. ПРОИЗВОДСТВО" вместо "ПРОИЗВОДСТВО"). Срезаем и её тоже.
        line = re.sub(r"^\s*\d+[.)]\s*", "", line)
        m = re.match(r"\s*[-•]?\s*([^:]{2,25}):\s*(.+?)\s*$", line)
        if not m:
            continue
        label, phrase = m.group(1).strip(), m.group(2).strip()
        # Gemini иногда шлёт заголовок "Для предложения N:" вместо пары
        # МЕТКА/фраза, несмотря на запрет в промпте — regex по формату
        # "текст: текст" его тоже матчит, отсекаем явно по содержимому.
        if "предложен" in label.lower():
            continue
        if label and len(phrase) >= 3:
            out.append((label.upper(), phrase))
        if len(out) >= len(sentences):
            break
    return out or None


async def build_slogan_and_tips(
    description: str, category: str = "", llm=None,
) -> tuple[str, list[tuple[str, str]]]:
    body = _strip_spec_header(description)
    sentences = _split_sentences(body)
    slogan = _trim_words(sentences[0], 8) if sentences else ""

    rest = sentences[1:]
    scored = [(i, s) for i, s in enumerate(rest) if _benefit_score(s) > 0]
    if scored:
        # Топ-2 по выгоде, но в исходном порядке появления в тексте —
        # иначе блоки читаются рвано, скачками по документу.
        scored.sort(key=lambda pair: -_benefit_score(pair[1]))
        picked = sorted(scored[:2], key=lambda pair: pair[0])
        chosen = [s for _, s in picked]
    else:
        # Ни одного явно "выгодного" предложения не нашлось — старое
        # поведение (первые по порядку), чем совсем ничего не показать.
        chosen = rest[:2]

    if llm is not None:
        rewritten = await _rewrite_tips_llm(chosen, category, llm)
        if rewritten:
            return slogan, rewritten

    tips = []
    for s in chosen:
        label = _keyword_label(s, category)
        tips.append((label, _smart_trim_sentence(s.strip())))
    return slogan, tips


async def build_free_infographic(article: str) -> dict:
    card = await asyncio.to_thread(_find_card, article)
    if not card:
        raise ValueError(f"Карточка {article!r} не найдена на WB")

    title = card.get("title") or ""
    description = card.get("description") or ""
    brand = card.get("brand") or ""
    category = card.get("subjectName") or ""
    characteristics = card.get("characteristics") or []

    features = build_features(category, characteristics)
    if not features:
        features = await build_features_fallback(title, brand, category, article=article)
    slogan, tips = await build_slogan_and_tips(description, category, llm=_fallback_llm)

    data = await get_wb_card_data(article)
    photos = (data or {}).get("photos") or []
    if not photos:
        raise ValueError(f"У карточки {article!r} нет фото — нечего класть в шаблон")
    img_bytes = photos[0]

    print(f"Категория: {category!r}")
    print(f"Features: {features}")
    print(f"Slogan: {slogan!r}")
    print(f"Tips: {tips}")

    infographic, warning = await make_infographic(
        title, features, img_bytes, llm=_fallback_llm, brand=brand, slogan=slogan, category=category,
    )
    if warning:
        print(f"Предупреждение инфографики: {warning}")

    richcontent = await make_richcontent(
        title, features, tips, img_bytes, llm=_fallback_llm, category=category,
    )

    out = {}
    if infographic:
        p = OUT_DIR / f"{article}_infographic.jpg"
        p.write_bytes(infographic)
        out["infographic"] = str(p)
    if richcontent:
        p = OUT_DIR / f"{article}_richcontent.jpg"
        p.write_bytes(richcontent)
        out["richcontent"] = str(p)
    return out


async def main():
    if len(sys.argv) < 2:
        print("Использование: python tools/free_infographic_from_card.py <артикул>")
        return
    article = sys.argv[1]
    result = await build_free_infographic(article)
    print("\nСохранено:")
    for k, v in result.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    asyncio.run(main())
