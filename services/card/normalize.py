import logging
import re
from dataclasses import dataclass

from services.llm.base import LLMProvider
from .brands import find_known_brand

log = logging.getLogger(__name__)

# AMD/Intel/NVIDIA/Radeon/GeForce/Ryzen/Core в названиях ОЗУ/SSD/кулеров/БП почти
# всегда означают платформу/совместимость, а не бренд-производителя модуля.
# Если LLM всё же принял такое слово за BRAND и товар не видеокарта/процессор
# (где это реальный бренд) — откатываемся на исходное название целиком.
# UMA (Unified Memory Architecture — встроенная графика с общей с CPU памятью)
# — техническое обозначение в спеках ноутбуков (напр. «UMA Ultra 7 255U»),
# никогда не бренд ни в каком контексте — исключение по _GPU_CPU_PREFIX_RE
# для него не нужно (см. крах 14.07.2026: LLM принял «UMA» за бренд ноутбука).
_PLATFORM_TAGS = {"amd", "intel", "nvidia", "radeon", "geforce", "ryzen", "core", "uma"}
_GPU_CPU_PREFIX_RE = re.compile(r'^(видеокарта|процессор|gpu|cpu)\b', re.IGNORECASE)


@dataclass
class ProductInfo:
    brand: str
    model: str
    full_name: str  # "brand model" — для отображения
    color: str = ""           # человекочитаемый цвет ("Glacier Blue", "Black")
    color_en: str = ""        # английский тег для поиска ("blue", "black")
    memory: str = ""          # RAM/объём: "4/128GB", "8/256GB" — для точного поиска

    @property
    def search_name(self) -> str:
        """Полное название для поиска: full_name + memory (если есть).
        «Infinix HOT 12 4/128GB» ищет точнее, чем просто «Infinix HOT 12»."""
        return f"{self.full_name} {self.memory}".strip() if self.memory else self.full_name

    @classmethod
    def from_raw(cls, raw: str) -> "ProductInfo":
        """Фоллбэк без LLM — первое слово как бренд."""
        raw = raw.strip()
        tokens = raw.split()
        if not tokens:
            return cls(brand="", model="", full_name=raw)
        return cls(brand=tokens[0], model=" ".join(tokens[1:]), full_name=raw)


_PARSE_PROMPT = """Ты разбираешь название товара на бренд, модель, память и цвет.

Задача:
- БРЕНД — компания-производитель. Может быть из нескольких слов (Lian Li, JBL, Z by HP, Be Quiet).
- МОДЕЛЬ — без цвета, объёма памяти, артикула склада, SKU, страны поставки, кодов чипсетов (T606, G85, G81, A22 и т.п.). Чипсеты в MODEL не включать.
- ПАМЯТЬ — объём RAM и/или хранилища в формате как в исходнике: «4/128GB», «8/256GB», «512GB», «16GB».
  Если памяти нет — пустая строка.
- ЦВЕТ — название цвета как в исходнике (Glacier Blue, Awesome Navy, Black, Чёрный). Если в названии не указан цвет — пустая строка.
- ЦВЕТ_EN — простой английский тег базового цвета для поиска. Разрешённые значения (выбери ОДНО, ближайшее):
  black, white, grey, light grey, silver, blue, navy, green, red, orange, yellow, purple, pink, gold, beige, brown.
  Это базовый цвет, НЕ маркетинговое название.

ОЧЕНЬ ВАЖНО про ЦВЕТ:
- Цвет может стоять как СРАЗУ ПОСЛЕ МОДЕЛИ (часто через запятую: «Infinix HOT 12, Legend White, G85 4/128GB»), так и в КОНЦЕ строки. Ищи слово-цвет в ЛЮБОМ месте, не только в конце.
- Маркетинговые и образные названия ОБЯЗАТЕЛЬНО приводи к базовому цвету. Примеры соответствий:
  Ink / Midnight / Obsidian / Starry / Cosmic / Phantom → black (или navy, если явно синеватый)
  Graphite / Titanium / Space Gray / Carbon / Steel / Meteorite → grey
  Light Gray / Light Grey / Pearl / Mist / Fog / Smoke / Platinum / Ash → light grey
  Glacier / Sky / Ocean / Aqua / Sea / Ice / Cyan / Aurora → blue
  Sapphire / Indigo / Denim → navy
  Mint / Forest / Emerald / Sage / Lime → green
  Sunset / Coral / Crimson / Ruby / Scarlet → red
  Lavender / Violet / Lilac / Amethyst → purple
  Rose / Blush / Sakura / Magenta → pink
  Sand / Sandy / Cream / Ivory / Champagne / Khaki / Wheat / Caramel → beige
  Bronze / Copper / Amber / Sunrise → gold (или brown/orange по оттенку)
  Pearl / Snow / Frost → white
- НИКОГДА не оставляй ЦВЕТ_EN пустым, если в названии есть хоть какое-то слово-цвет (включая образные). Пусто — только если цвета реально нет.
- Слова про цвет НЕ должны попадать в MODEL — выноси их в COLOR.

ВАЖНО — слова-категории (НЕ бренды), игнорируй их в начале строки:
Смартфон, Телефон, Планшет, Ноутбук, Компьютер, Монитор, Наушники, Гарнитура,
Мышь, Мышка, Клавиатура, Колонка, Акустика, Видеокарта, Процессор, Материнская плата,
Оперативная память, RAM, Блок питания, PSU, Корпус, Кулер, СЖО, Cooler,
Роутер, Маршрутизатор, Принтер, Внешний жёсткий диск, Зарядное устройство, Кабель, Моноблок,
Smartphone, Phone, Tablet, Laptop, Monitor, Headphones, Mouse, Keyboard, GPU, CPU.

ОСОБЫЙ СЛУЧАЙ — НОУТБУКИ без явного бренда-производителя:
Слово UMA в спеках ноутбука (например «UMA Ultra 7 255U», «UMA Ultra 5 125H») —
это Unified Memory Architecture (встроенная графика, использующая общую с
процессором память), ТЕХНИЧЕСКИЙ ТЕРМИН, а не бренд. Если единственное
«похожее на бренд» слово в начале названия — UMA (или dGPU/iGPU) — BRAND
оставь пустым, MODEL = всё название целиком (как в примере с ОЗУ ниже).

UMA Ultra 7 255U 4 16 inch G1i / 16.0 WUXGA UWVA 300 FHDC 60Hz / 16GB DDR5
BRAND:
MODEL: UMA Ultra 7 255U 4 16 inch G1i / 16.0 WUXGA UWVA 300 FHDC 60Hz / 16GB DDR5
MEMORY:
COLOR:
COLOR_EN:

ОСОБЫЙ СЛУЧАЙ — ОЗУ, SSD, кулеры, БП без явного бренда-производителя:
Слова AMD, Intel, NVIDIA, Radeon, GeForce, Ryzen, Core в названиях ОЗУ/SSD/кулеров/БП
почти всегда означают ПЛАТФОРМУ/СОВМЕСТИМОСТЬ (например, память «для AMD Ryzen» или
с разгоном «под AMD Radeon»/«Intel XMP»), а НЕ производителя модуля. Настоящие бренды
памяти — Kingston, Corsair, Crucial, ADATA, G.Skill, Patriot, TeamGroup, Samsung,
Hynix, Apacer, GeIL, Netac, Kingmax, Silicon Power и т.п.
Если в названии НЕТ настоящего бренда-производителя — BRAND оставь пустым, а MODEL =
всё название целиком (без ведущего слова категории, MEMORY не выносить отдельно —
оставить как часть MODEL). НЕ выноси AMD/Intel/NVIDIA/Radeon/GeForce в BRAND — иначе
поиск фото и описания уйдёт на видеокарты/процессоры этого бренда, а не на сам товар.

Оперативная память для ноутбука 16GB DDR5 5200MHz AMD Radeon
BRAND:
MODEL: Оперативная память для ноутбука 16GB DDR5 5200MHz AMD Radeon
MEMORY:
COLOR:
COLOR_EN:

Примеры:

Samsung Galaxy A55 5G SM-A556E 8/256GB Awesome Navy
BRAND: Samsung
MODEL: Galaxy A55 5G SM-A556E
COLOR: Awesome Navy
COLOR_EN: navy

Oscal Flat 2 T606 6/256GB Glacier Blue
BRAND: Oscal
MODEL: Flat 2
COLOR: Glacier Blue
COLOR_EN: blue

Infinix SMART 6 Plus Tranquil Sea Blue
BRAND: Infinix
MODEL: SMART 6 Plus
COLOR: Tranquil Sea Blue
COLOR_EN: blue

Tecno Spark 50 Magic Skin Green
BRAND: Tecno
MODEL: Spark 50
COLOR: Magic Skin Green
COLOR_EN: green

Смартфон Infinix SMART 6 Plus
BRAND: Infinix
MODEL: SMART 6 Plus
COLOR:
COLOR_EN:

HP ProOne 240 G10,i5-1334U,23.8 FHD,16GB,512GB
BRAND: HP
MODEL: ProOne 240 G10
COLOR:
COLOR_EN:

Видеокарта MSI GeForce RTX 4070 GAMING X TRIO 12GB
BRAND: MSI
MODEL: GeForce RTX 4070 GAMING X TRIO
COLOR:
COLOR_EN:

Lian Li Uni Fan SL Wireless 120 ARGB Black
BRAND: Lian Li
MODEL: Uni Fan SL Wireless 120 ARGB
COLOR: Black
COLOR_EN: black

Xiaomi Redmi Note 13 8/256GB Midnight Black
BRAND: Xiaomi
MODEL: Redmi Note 13
COLOR: Midnight Black
COLOR_EN: black

Realme C55 6/128GB Sunshower
BRAND: Realme
MODEL: C55
COLOR: Sunshower
COLOR_EN: gold

Samsung Galaxy S24 Ultra 12/256 Titanium Gray
BRAND: Samsung
MODEL: Galaxy S24 Ultra
COLOR: Titanium Gray
COLOR_EN: grey

Tecno Camon 20 Pro Serenity Blue
BRAND: Tecno
MODEL: Camon 20 Pro
COLOR: Serenity Blue
COLOR_EN: blue

Смартфон Infinix HOT 12, Legend White, G85 4/128GB, 6.82"IPS, 2xSIM+microSD
BRAND: Infinix
MODEL: HOT 12
COLOR: Legend White
COLOR_EN: white

Смартфон Oscal Flat 2, Phantom Black, T606, 6/256GB, 6.56"
BRAND: Oscal
MODEL: Flat 2
MEMORY: 6/256GB
COLOR: Phantom Black
COLOR_EN: black

Выдай РОВНО пять строк:
BRAND: ...
MODEL: ...
MEMORY: ...
COLOR: ...
COLOR_EN: ..."""


def _parse_response(text: str) -> tuple[str, str, str, str, str]:
    brand, model, memory, color, color_en = "", "", "", "", ""
    for line in text.strip().splitlines():
        line = line.strip().strip('"').strip("'")
        upper = line.upper()
        if upper.startswith("BRAND:"):
            brand = line.split(":", 1)[1].strip()
        elif upper.startswith("MODEL:"):
            model = line.split(":", 1)[1].strip()
        elif upper.startswith("MEMORY:"):
            memory = line.split(":", 1)[1].strip()
        elif upper.startswith("COLOR_EN:"):
            color_en = line.split(":", 1)[1].strip().lower()
        elif upper.startswith("COLOR:"):
            color = line.split(":", 1)[1].strip()
    return brand, model, memory, color, color_en


async def parse_product_info(raw: str, llm: LLMProvider) -> ProductInfo:
    """
    Разбирает название через DeepSeek на (brand, model, color, color_en).
    full_name = 'brand model' — нормализованная строка для поиска (без цвета).
    color/color_en сохраняются отдельно для уточнения поиска фото.
    При ошибке — фоллбэк на разбиение по первому слову.
    """
    raw = raw.strip()
    try:
        resp = await llm.chat(raw, _PARSE_PROMPT)
        brand, model, memory, color, color_en = _parse_response(resp.text)

        # Кросс-проверка по справочнику известных брендов (16.07: бренд
        # держался целиком на угадывании LLM без сверки — не ловил ни
        # пропущенный бренд, который явно есть в названии, ни редкую
        # галлюцинацию бренда, которого в названии вовсе нет).
        known_brand = find_known_brand(raw)
        if known_brand:
            norm_raw = re.sub(r"[\s\-.]+", "", raw).lower()
            norm_llm_brand = re.sub(r"[\s\-.]+", "", brand).lower()
            if not brand:
                log.info(f"Brand from reference: LLM brand empty → '{known_brand}' found in raw")
                brand = known_brand
            elif norm_llm_brand not in norm_raw:
                log.info(f"Brand override: LLM gave '{brand}' (not in raw text) → '{known_brand}'")
                brand = known_brand
            if brand == known_brand and model.lower().startswith(brand.lower()):
                model = model[len(brand):].strip()

        is_platform_tag = brand.lower() in _PLATFORM_TAGS and not _GPU_CPU_PREFIX_RE.match(raw)
        if not brand or is_platform_tag:
            # Нет настоящего бренда-производителя (или LLM принял платформенный
            # тег вроде AMD/Radeon за бренд) — используем исходное название
            # целиком, иначе теряем спецификации и поиск фото/описания уходит
            # на видеокарты/процессоры этого бренда вместо самого товара.
            reason = f", brand='{brand}' — платформенный тег" if is_platform_tag else ""
            log.info(f"Parsed: '{raw}' → без бренда-производителя{reason}, full_name=raw")
            return ProductInfo(brand="", model=raw, full_name=raw,
                               color=color, color_en=color_en, memory=memory)

        full_name = f"{brand} {model}".strip() if model else brand
        log.info(f"Parsed: '{raw}' → brand='{brand}' model='{model}' memory='{memory}' color='{color}' (en='{color_en}')")
        return ProductInfo(brand=brand, model=model, full_name=full_name,
                           color=color, color_en=color_en, memory=memory)
    except Exception as e:
        log.warning(f"parse_product_info failed: {e}")

    return ProductInfo.from_raw(raw)


async def normalize_product_name(raw: str, llm: LLMProvider) -> str:
    """Совместимость со старым API — возвращает full_name."""
    info = await parse_product_info(raw, llm)
    return info.full_name
