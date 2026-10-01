"""
Профили категорий для пайплайна Ozon (services/ozon_pipeline.py) — то, что
раньше было захардкожено под SSD. Новую категорию добавляют сюда: путь к
официальному Excel-шаблону Ozon, внутреннее имя категории бота (для
PRIORITY_CHARS/инфографики), фиксированный "Тип*", код ТН ВЭД и
description_category_id/type_id из Ozon (см. /v1/description-category/tree).
"""
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class CategoryProfile:
    key: str                       # ключ для выбора профиля (CLI/команда)
    template: Path                 # официальный Ozon Excel-шаблон категории
    out: Path                      # куда сохранять заполненный файл
    category: str                  # внутреннее имя категории бота (PRIORITY_CHARS и т.п.)
    fixed_type: str                # значение колонки "Тип*" — фиксировано на категорию
    tnved: str                     # код ТН ВЭД (см. лист validation самого шаблона)
    description_category_id: int   # Ozon: /v1/description-category/tree
    type_id: int                   # Ozon: /v1/description-category/tree


# Папка с Excel-шаблонами Ozon задаётся в .env (OZON_TEMPLATES_DIR).
_TPL_DIR = Path(os.getenv("OZON_TEMPLATES_DIR", "./data/templates/ozon"))

PROFILES: dict[str, CategoryProfile] = {
    "ssd": CategoryProfile(
        key="ssd",
        template=_TPL_DIR / (r"Внутренний SSD-диск_18.06.2026.xlsx"),
        out=_TPL_DIR / (r"Внутренний SSD-диск_filled.xlsx"),
        category="SSD накопители",
        fixed_type="Внутренний SSD-диск",
        tnved="8523519300",
        description_category_id=17028626,
        type_id=91431,
    ),
    "keyboards": CategoryProfile(
        key="keyboards",
        template=_TPL_DIR / (r"Клавиатура_18.06.2026 (1).xlsx"),
        out=_TPL_DIR / (r"Клавиатура_filled.xlsx"),
        category="Клавиатуры",
        fixed_type="Клавиатура",
        tnved="8471606000",
        description_category_id=17028646,
        type_id=91801,
    ),
    "cctv": CategoryProfile(
        key="cctv",
        template=_TPL_DIR / (r"Камера видеонаблюдения_19.06.2026.xlsx"),
        out=_TPL_DIR / (r"Камера видеонаблюдения_filled.xlsx"),
        category="Камеры видеонаблюдения",
        fixed_type="Камера видеонаблюдения",
        tnved="8525891900",
        description_category_id=17028914,
        type_id=95694,
    ),
    "kb_mouse_combo": CategoryProfile(
        key="kb_mouse_combo",
        template=_TPL_DIR / (r"Комплект клавиатура, мышь_19.06.2026.xlsx"),
        out=_TPL_DIR / (r"Комплект клавиатура, мышь_filled.xlsx"),
        category="Комплект клавиатура и мышь",
        fixed_type="Комплект клавиатура, мышь",
        tnved="8471606000",
        description_category_id=17028646,
        type_id=91804,
    ),
    "headphones": CategoryProfile(
        key="headphones",
        template=_TPL_DIR / (r"Наушники_10.07.2026.xlsx"),
        out=_TPL_DIR / (r"Наушники_filled.xlsx"),
        category="Наушники",
        fixed_type="Наушники",
        tnved="8518309500",
        description_category_id=17028929,
        type_id=504866264,
    ),
    "soundbar": CategoryProfile(
        key="soundbar",
        template=_TPL_DIR / (r"Саундбар_29.06.2026.xlsx"),
        out=_TPL_DIR / (r"Саундбар_filled.xlsx"),
        category="Акустика",
        fixed_type="Саундбар",
        tnved="8518220009",
        description_category_id=17028908,
        type_id=95305,
    ),
    "pc_speakers": CategoryProfile(
        key="pc_speakers",
        template=_TPL_DIR / (r"Компьютерная акустика_01.07.2026.xlsx"),
        out=_TPL_DIR / (r"Компьютерная акустика_filled.xlsx"),
        category="Акустика",
        fixed_type="Компьютерная акустика",
        tnved="8518220009",
        description_category_id=17028908,
        type_id=95318,
    ),
    "hifi": CategoryProfile(
        key="hifi",
        template=_TPL_DIR / (r"Акустическая система_29.06.2026.xlsx"),
        out=_TPL_DIR / (r"Акустическая система_filled.xlsx"),
        category="Акустика",
        fixed_type="Акустическая система",
        tnved="8518220009",
        description_category_id=17028908,
        type_id=95315,
    ),
    "monitors": CategoryProfile(
        key="monitors",
        template=_TPL_DIR / (r"Монитор_05.07.2026.xlsx"),
        out=_TPL_DIR / (r"Монитор_filled.xlsx"),
        category="Мониторы",
        fixed_type="Монитор",
        # 8528521000 — «Мониторы, используемые исключительно или главным
        # образом в вычислительных системах» (лист validation шаблона)
        tnved="8528521000",
        description_category_id=17028926,
        type_id=91494,
    ),
    "brackets": CategoryProfile(
        key="brackets",
        template=_TPL_DIR / (r"Кронштейн для монитора_07.07.2026 (1).xlsx"),
        out=_TPL_DIR / (r"Кронштейн для монитора_filled.xlsx"),
        category="Кронштейны",
        fixed_type="Кронштейн для монитора",
        # 8302500000 — «Вешалки для шляп, крючки для шляп, кронштейны и
        # аналогичные изделия» (лист validation шаблона)
        tnved="8302500000",
        description_category_id=17028922,
        type_id=95902,
    ),
    "acoustic": CategoryProfile(
        key="acoustic",
        template=_TPL_DIR / (r"Беспроводная колонка_29.06.2026.xlsx"),
        out=_TPL_DIR / (r"Беспроводная колонка_filled.xlsx"),
        category="Акустика",
        fixed_type="Беспроводная колонка",
        tnved="8518220009",
        description_category_id=17028908,
        type_id=95320,
    ),
    "monoblock": CategoryProfile(
        key="monoblock",
        template=_TPL_DIR / (r"Моноблок_10.07.2026.xlsx"),
        out=_TPL_DIR / (r"Моноблок_filled.xlsx"),
        category="Моноблоки",
        fixed_type="Моноблок",
        # 8471410000 — «Машины вычислительные прочие, содержащие в одном
        # корпусе центральный блок обработки данных и устройство ввода и
        # вывода» (лист validation шаблона, стандартный код для AIO)
        tnved="8471410000",
        description_category_id=17028619,
        type_id=91475,
    ),
    "mousepad": CategoryProfile(
        key="mousepad",
        template=_TPL_DIR / (r"Коврик для мышки_13.07.2026.xlsx"),
        out=_TPL_DIR / (r"Коврик для мышки_filled.xlsx"),
        category="Коврики для мыши",
        fixed_type="Коврик для мышки",
        # 3926909200 — «прочие изделия из пластмасс, не включённые в другие
        # категории» (из листа validation самого шаблона, 260 вариантов —
        # взят ближайший общий код для резинотканевых ковриков, фолбэк,
        # если WB-карточка/база tnved_codes не дадут точнее)
        tnved="3926909200",
        description_category_id=18262715,
        type_id=96808,
    ),
}


def get_profile(key: str) -> CategoryProfile:
    profile = PROFILES.get(key)
    if not profile:
        raise KeyError(f"Нет профиля категории {key!r}. Доступные: {list(PROFILES)}")
    return profile


# Ключевые слова для авто-определения типа акустики
_SPEAKER_DETECT: list[tuple[list[str], str]] = [
    (["саундбар", "soundbar"],                                        "soundbar"),
    (["bluetooth", "bt ", " bt,", "wi-fi", "wifi", "беспроводн",
      "wireless", "портативн", "портатив"],                           "acoustic"),
    (["hi-fi", "hifi", "hi fi", "акустическая система"],              "hifi"),
    (["2.0", "2.1", "4.1", "5.1", "7.1", "компьютерн", "колонки"],   "pc_speakers"),
]
_SPEAKER_DEFAULT = "pc_speakers"

# Профили входящие в авто-режим "speakers"
SPEAKER_PROFILES = {"acoustic", "pc_speakers", "soundbar", "hifi"}


def detect_speaker_profile(product_name: str) -> str:
    """Определяет профиль акустики по названию товара."""
    lower = product_name.lower()
    for keywords, profile_key in _SPEAKER_DETECT:
        if any(kw in lower for kw in keywords):
            return profile_key
    return _SPEAKER_DEFAULT
