import colorsys
import io
import logging
import random

from PIL import Image

from .gemini import generate_infographic_bg, generate_richcontent_bg
from . import infographic as _infographic
from . import richcontent as _richcontent
from . import local_bg as _local_bg
from . import infographic_gaming as _gaming
from .infographic_gaming import make_gaming_bg as _make_gaming_bg
from . import infographic_speakers_widget as _speakers
from . import infographic_mice as _mice
from . import infographic_ram as _ram
from . import infographic_gpu as _gpu
from . import infographic_watch as _watch
from . import infographic_signal as _signal
from . import infographic_chair as _chair
from . import infographic_simple as _simple
from . import infographic_slide2 as _slide2

log = logging.getLogger(__name__)


async def _bill(resp, operation: str):
    """Пишет стоимость LLM-вызова в costs. user_id тут не прокинут,
    поэтому относим на admin_id (бот фактически однопользовательский)."""
    try:
        from config import settings
        from utils.billing import save_cost
        await save_cost(settings.admin_id, operation, response=resp)
    except Exception as e:
        log.warning(f"billing failed ({operation}): {e}")

_INFOGRAPHIC_W, _INFOGRAPHIC_H = 768, 1024
_RICHCONTENT_W, _RICHCONTENT_H = 1600, 1000

# Источник фона. False (по умолчанию) — локальный генератор градиентов (бесплатно,
# мгновенно). True — старая генерация через Gemini (код цел, но НЕ вызывается,
# пока флаг False; врубается одной строкой когда понадобится).
USE_GEMINI_BG = False

# Палитры для DeepSeek-подбора (ключи из _local_bg.PALETTES).
_PALETTE_PROMPT = """Ты подбираешь цветовую палитру фона для карточки товара по его характеру.
Верни РОВНО один ключ из списка (без пояснений и кавычек):

Холодные: arctic (лёд/премиум-синий), ocean (морская волна), mint_sky (мята-небо),
  aqua_lilac (аква-сирень), teal_blue (бирюза-синий)
Тёплые: peach_pink (персик-розовый), coral_rose (коралл), sunset (закат),
  apricot (абрикос), lemon_mint (лимон-мята)
Сиренево-розовые: lavender (нежная лаванда), violet_cyan (фиолет-циан, тех),
  iris (ирис), magenta (насыщенный розово-сиреневый)
Зелёные/земляные: sage (шалфей), emerald (изумруд), olive_cream (олива-крем)
Нейтраль+акцент: cloud_blue (облачно-синий), sand (песок), graphite (графит, строгая техника)
Премиум: champagne (шампань-золото), rose_gold (розовое золото)
Игровое/тех-неон: cyber (сине-фиолетовый неон), neon_aqua (аква-неон)

Подбирай по сути товара (флагман→премиум/холодные, бюджет→свежие/тёплые,
игровое→неон, эко/здоровье→зелёные, женское→розовые и т.п.).
Ответь ОДНИМ ключом."""


async def _generate_bg_palette(product: str, features: list[tuple[str, str]],
                               llm) -> str | None:
    """DeepSeek выбирает ключ палитры под характер товара. None → ротация."""
    feat = ", ".join(f"{t}: {v}" for t, v in features[:4])
    try:
        resp = await llm.chat(f"Товар: {product}\nХарактеристики: {feat}", _PALETTE_PROMPT)
        await _bill(resp, "infographic_palette")
        key = resp.text.strip().split()[0].strip().lower().strip('`"\'.,') if resp.text.strip() else ""
        if key in _local_bg.PALETTES:
            log.info(f"BG palette: {key}")
            return key
        log.info(f"BG palette: не распознан '{resp.text.strip()[:30]}' → ротация")
    except Exception as e:
        log.warning(f"palette pick failed: {e}")
    return None

_BG_PROMPT_SYSTEM = """Ты генерируешь описание стиля фона для карточки товара на маркетплейсе.

Фон будет сгенерирован нейросетью — ТОЛЬКО цвета, градиенты, текстуры, геометрические фигуры.
ЗАПРЕЩЕНО: реальные объекты, фото товара, люди, техника, логотипы.

ВАЖНО: фон должен быть СВЕТЛЫМ — не тёмным. Яркость фона > 50% (светлые, пастельные, средние тона).
Тёмный фон запрещён — товар должен хорошо читаться поверх фона.
Передавай характер товара через цвета, текстуры и акцентные элементы.

Гиперболизируй характер товара через визуальные образы:
— Игровая видеокарта → белый фон, неоновые RGB-лучи, геометрические сетки, электрические искры
— Смартфон Glacier Blue → ледяной белый фон, арктические кристаллы, холодные голубые блики
— Механическая клавиатура → светлый фон, тонкие линии сетки, мягкое неоновое свечение
— Наушники Hi-Fi → белый фон, концентрические звуковые волны, пастельные переливы
— Бюджетный телефон → чистый белый, нежный пастель, минимализм

Ответь ОДНОЙ строкой на английском — описание стиля фона (20-35 слов).
Будь конкретен: укажи светлые цвета, тип текстуры/градиента, акцентные элементы.

Примеры:
- light blue-to-cyan gradient with electric RGB neon grid lines and purple glow streaks, gaming aesthetic
- pale ice-blue with translucent crystal shards and cold silver shimmer, frozen premium feel
- soft warm beige with gold geometric hexagon pattern and subtle amber glow, premium feel
- light lavender gradient with faint star particles and aurora-green shimmer at edges
- mint-to-white gradient with silver geometric lines, clean tech aesthetic

Только одна строка, без кавычек и пояснений."""


async def _extract_header_title_model(product: str, llm, category: str = "") -> tuple[str, str]:
    """Тип товара для шапки общего шаблона: сверху крупно тип устройства
    («Точка доступа», «Монитор», «Мышка»), снизу — модель.

    Если категория уже надёжно определена keyword-детектором
    (services/card/category.py, per-товару, без угадывания) — используем её
    напрямую и НЕ спрашиваем LLM: TYPE_MODEL_PROMPT видит только голую
    строку названия и может галлюцинировать тип по созвучию (16.07: "Lamzu
    Energon Pro" — коврик для мыши — LLM решил, что это "Внешний
    аккумулятор", видимо из-за слова Energon). LLM-угадывание остаётся
    фоллбэком только когда category пуста (детектор не распознал товар)."""
    if category:
        type_label = _gaming._CATEGORY_SINGULAR.get(category, category)
        return type_label, product
    from prompts import TYPE_MODEL_PROMPT
    try:
        resp = await llm.chat(product, TYPE_MODEL_PROMPT)
        await _bill(resp, "infographic_header")
        type_label, model_label = "", ""
        for line in resp.text.strip().splitlines():
            line = line.strip().strip('"').strip("'")
            upper = line.upper()
            if upper.startswith("TYPE:"):
                type_label = line.split(":", 1)[1].strip()
            elif upper.startswith("MODEL:"):
                model_label = line.split(":", 1)[1].strip()
        return type_label, model_label
    except Exception as e:
        log.warning(f"type/model split failed: {e}")
        return "", ""


async def _generate_bg_style(product: str, features: list[tuple[str, str]],
                              llm) -> str:
    """DeepSeek генерирует описание стиля фона под конкретный товар."""
    feat_text = ", ".join(f"{t}: {v}" for t, v in features[:4])
    user_msg = f"Товар: {product}\nХарактеристики: {feat_text}"
    try:
        resp = await llm.chat(user_msg, _BG_PROMPT_SYSTEM)
        await _bill(resp, "infographic_bg_style")
        style = resp.text.strip().strip('"').strip("'")
        log.info(f"BG style generated: {style}")
        return style
    except Exception as e:
        log.warning(f"BG style generation failed: {e}")
        return ""


async def pick_bg_palette(product: str, features: list[tuple[str, str]],
                           llm) -> str:
    """Ключ палитры локального фона — выбирается ОДИН раз на карточку и
    передаётся в make_infographic + make_second_slide, чтобы оба слайда
    были в одной гамме. LLM промолчал/не передан → ротация."""
    key = await _generate_bg_palette(product, features, llm) if llm else None
    return key or _local_bg.next_palette_key()


async def make_infographic(
    product: str,
    features: list[tuple[str, str]],
    img_bytes: bytes | None,
    llm=None,
    brand: str = "",
    slogan: str = "",
    palette: str | None = None,
    category: str = "",
) -> tuple[bytes | None, str]:
    """
    DeepSeek генерирует стиль → Gemini рендерит фон → PIL накладывает всё.
    Возвращает (bytes | None, warning_msg).
    warning_msg пустая если всё прошло нормально, иначе — описание что пошло не так.
    """
    warning = ""
    bg = None

    if USE_GEMINI_BG:
        # ── Gemini-путь (ВЫКЛЮЧЕН флагом USE_GEMINI_BG; код сохранён) ──
        bg_style = ""
        if llm:
            bg_style = await _generate_bg_style(product, features, llm)
            if not bg_style:
                warning = "DeepSeek не сгенерировал стиль фона"
        if bg_style:
            bg = await generate_infographic_bg(product, features, None, bg_style=bg_style)
            if not bg:
                warning = "Gemini не сгенерировал фон"
        if not bg:
            if not warning:
                warning = "LLM не передан"
            canvas = Image.new("RGB", (_INFOGRAPHIC_W, _INFOGRAPHIC_H), (255, 255, 255))
            buf = io.BytesIO()
            canvas.save(buf, format="JPEG", quality=95)
            bg = buf.getvalue()
            log.warning(f"Infographic fallback to white: {warning}")
    else:
        # ── Локальный градиент (по умолчанию) ──
        if palette is None:
            palette = await _generate_bg_palette(product, features, llm) if llm else None
        bg = _local_bg.generate_background(_INFOGRAPHIC_W, _INFOGRAPHIC_H, palette)

    header_title, header_model = await _extract_header_title_model(product, llm, category) if llm or category else ("", "")

    result = await _infographic.build(bg, product, features, img_bytes, brand=brand, slogan=slogan,
                                       header_title=header_title, header_model=header_model)
    return result, warning


async def make_second_slide(
    product: str,
    feature: tuple[str, str],
    slogan: str,
    img_bytes: bytes,
    palette: str | None = None,
) -> bytes | None:
    """Второй слайд: фото второго ракурса + одна главная характеристика +
    слоган (переиспользуется rich_slogan из рич-контента). palette — тот же
    ключ, что у первого слайда (см. pick_bg_palette). Без LLM/Gemini-вызовов:
    единственный расход — rembg второго фото."""
    try:
        bg = _local_bg.generate_background(_INFOGRAPHIC_W, _INFOGRAPHIC_H, palette)
        return await _slide2.build_slide2(bg, feature, slogan, img_bytes)
    except Exception as e:
        log.error(f"Second slide failed for {product}: {e}", exc_info=True)
        return None


async def make_richcontent(
    product: str,
    features: list[tuple[str, str]],
    tips: list[tuple[str, str]],
    img_bytes: bytes | None,
    extra_photos: list[bytes] | None = None,
    llm=None,
    gaming_accent: tuple[int, int, int] | None = None,
    category: str = "",
) -> bytes | None:
    """
    Gemini генерирует ЧИСТЫЙ фон 1700×1000 (без объектов). Поверх в левую зону
    кладём реальные фото товара через compose_left_zone (main + до 2 thumbs).
    Затем overlay текста.

    img_bytes — главное фото (обязательно для левой зоны)
    extra_photos — дополнительные ракурсы для миниатюр под главным фото
    """
    if gaming_accent is not None:
        # ── Gaming: тёмный фон с accent glow, как на инфографике ──
        bg_img = _make_gaming_bg(_RICHCONTENT_W, _RICHCONTENT_H, gaming_accent)
        buf = io.BytesIO()
        bg_img.save(buf, format="JPEG", quality=92)
        bg = buf.getvalue()
    elif USE_GEMINI_BG:
        # ── Gemini-путь (ВЫКЛЮЧЕН флагом USE_GEMINI_BG; код сохранён) ──
        bg = await generate_richcontent_bg(product, img_bytes)
        if not bg:
            log.warning(f"Rich content: Gemini returned nothing for {product}")
            return None
    else:
        # ── Локальный градиент (по умолчанию) ──
        palette = await _generate_bg_palette(product, features, llm) if llm else None
        bg = _local_bg.generate_background(_RICHCONTENT_W, _RICHCONTENT_H, palette)

    if img_bytes:
        from .photos_compose import compose_left_zone, estimate_content_ar
        try:
            bg_img = Image.open(io.BytesIO(bg)).convert("RGBA")
            W, H = bg_img.size

            # Определяем aspect ratio продукта чтобы выбрать размер зоны.
            # Широкие товары (ноутбуки, мониторы, AR > 1.35) получают чуть
            # большую зону — иначе 16:9 девайс выглядит крошечным. AR — по
            # реальному содержимому фото (estimate_content_ar), не по
            # размеру файла-канваса: у товаров вроде длинной узкой
            # веб-камеры-балки файл может быть портретным (много белых
            # полей), хотя сам товар на нём — широкая полоса.
            _par = estimate_content_ar(img_bytes, default=1.0)
            if _par > 2.2:
                zone_frac, padding = 0.42, 20
            elif _par > 1.35:
                zone_frac, padding = 0.34, 25
            else:
                zone_frac, padding = 0.30, 40
            zone_w    = int(W * zone_frac)
            # 11.09.2026: zone_frac 0.34/0.42 (широкие товары) превышал
            # mid_x=0.33*W из richcontent.py::overlay — у вытянутых товаров
            # (планки ОЗУ, видеокарты, узкие веб-камеры) зона товара
            # геометрически заходила под полупрозрачные карточки колонки
            # ХАРАКТЕРИСТИКИ (alpha=220/255, поэтому обрезанный товар
            # проступал сквозь них - живой баг, найден на партии из 41
            # карточки). Жёстко ограничиваем zone_w, не давая ему долезть
            # до колонки характеристик, независимо от исходного zone_frac.
            zone_w    = min(zone_w, int(W * 0.33) - 24)

            composed = await compose_left_zone(
                main_bytes=img_bytes,
                extra_bytes=extra_photos or [],
                zone_w=zone_w,
                zone_h=H,
                padding=padding,
                category=category,
            )
            bg_img.paste(composed, (0, 0), composed)

            buf = io.BytesIO()
            bg_img.convert("RGB").save(buf, format="JPEG", quality=92)
            bg = buf.getvalue()
            n_extra = len(extra_photos or [])
            log.info(f"Rich content: composed left zone (main + {n_extra} thumbs)")
        except Exception as e:
            log.warning(f"Rich content: compose left zone failed: {e}")

    return _richcontent.build(bg, product, features, tips, accent=gaming_accent)


# Известные игровые/техно-бренды → фирменный акцентный цвет (поиск по подстроке в названии товара).
_GAMING_BRAND_COLORS: dict[str, tuple[int, int, int]] = {
    "hyperx":      (238, 17, 51),    # red
    "bloody":      (255, 34, 68),    # red
    "razer":       (0, 238, 85),     # green
    "xbox":        (102, 204, 34),   # lime
    "playstation": (0, 204, 255),    # cyan
    "ps5":         (0, 204, 255),
    "ps4":         (0, 204, 255),
    "steelseries": (255, 102, 0),    # orange
    "corsair":     (255, 187, 0),    # gold
    "logitech":    (0, 85, 238),     # blue
    "rog":         (255, 119, 0),    # orange (ASUS ROG/TUF)
    "tuf":         (255, 179, 0),
    "nintendo":    (255, 51, 153),   # pink
    "jabra":       (0, 102, 221),    # blue
    "sennheiser":  (238, 0, 102),    # pink
}

# Неоновый пул — RGB-эстетика "железных" категорий (мыши, видеокарты, ОЗУ),
# где яркая подсветка — часть стиля самого устройства.
_NEON_POOL: list[tuple[int, int, int]] = [
    # Чуть смягчены (saturation×0.78, value×0.88) — чистый неон триггерил
    # предупреждение WB про слишком яркий фон карточки.
    (209, 58, 81),    # red
    (224, 119, 49),   # orange
    (224, 178, 49),   # gold
    (46, 209, 104),   # green
    (151, 209, 46),   # lime
    (39, 180, 156),   # teal
    (49, 189, 224),   # cyan
    (73, 119, 224),   # blue
    (154, 84, 224),   # purple
    (189, 84, 224),   # magenta
    (224, 84, 154),   # pink
    (224, 197, 49),   # yellow-gold
]

# Приглушённые/благородные тона — добавляются для "бытовых" категорий
# (акустика, часы, общий gaming-стиль), где реальный цвет товара чаще
# спокойный и неон рядом с ним смотрится чужеродно.
_MUTED_POOL: list[tuple[int, int, int]] = [
    (229, 111, 80),   # терракота
    (224, 155, 56),   # янтарь
    (163, 216, 97),   # шалфей
    (71, 204, 188),   # глубокий тил
    (97, 137, 216),   # графитово-синий
    (80, 95, 229),    # индиго
    (198, 97, 216),   # сливовый
    (229, 114, 143),  # пыльная роза
]

# Общий пул для "бытовых" категорий: неон + приглушённые вперемешку.
_LIFESTYLE_POOL: list[tuple[int, int, int]] = _NEON_POOL + _MUTED_POOL

# Редакционный пул для simple-стиля — спокойные, ненасыщенные тона
# (S=35-55%, V=55-68%). Бытовые товары (кабели, зарядки, расходники):
# карточка должна выглядеть как в каталоге, а не как RGB-реклама.
_SIMPLE_POOL: list[tuple[int, int, int]] = [
    (72, 108, 165),   # стальной синий
    (68, 140, 100),   # лесной зелёный
    (162, 88, 78),    # мягкая терракота
    (112, 88, 158),   # пыльный фиолет
    (62, 132, 150),   # стальной тил
    (178, 138, 65),   # тёплый янтарь
    (168, 98, 122),   # пыльная роза
    (88, 98, 172),    # сланцевый индиго
    (95, 148, 108),   # шалфей
    (155, 108, 82),   # мокко-коралл
]

# Категории, где сохраняем только неоновый пул (RGB-стиль железа).
_HARDWARE_CATEGORIES = {"Мыши", "Видеокарты", "Оперативная память"}

# 18.08.2026: диспетчер стиля карточной инфографики (не richcontent) —
# по просьбе пользователя gaming только для наушников/комп-железа,
# всё остальное (бытовые товары, периферия общего назначения, оргтехника
# и т.п.) идёт в simple. Стиль "default" (services/image/infographic.py::build)
# сюда не участвует — пользователь его не использует.
_GAMING_INFOGRAPHIC_CATEGORIES = {
    "Наушники", "Мыши", "Клавиатуры", "Коврики для мыши",
    "Комплект клавиатура и мышь", "Видеокарты", "Процессоры",
    "Материнские платы", "Оперативная память", "Блоки питания",
    "Корпуса для ПК", "Охлаждение", "Охлаждение корпуса", "SSD накопители",
}


def pick_infographic_style(category: str) -> str:
    """"gaming" для наушников/комп-железа, иначе "simple"."""
    return "gaming" if category in _GAMING_INFOGRAPHIC_CATEGORIES else "simple"


def _pool_for_category(category: str) -> list[tuple[int, int, int]]:
    return _NEON_POOL if category in _HARDWARE_CATEGORIES else _LIFESTYLE_POOL


# Опорный тон (0-360°) для распознанных названий цвета товара — для подбора
# наиболее близкого по тону цвета из пула (может попасть и в неон,
# и в приглушённый — какой ближе к цвету товара).
_COLOR_HUE_HINTS: dict[str, float] = {
    "red": 355, "crimson": 355, "maroon": 355, "scarlet": 355,
    "orange": 25,
    "amber": 42, "gold": 42, "yellow": 45, "mustard": 42,
    "green": 130, "olive": 125,
    "lime": 85,
    "teal": 175, "mint": 175, "turquoise": 175, "seafoam": 170,
    "cyan": 195, "sky": 195, "azure": 195,
    "blue": 220, "navy": 220, "denim": 220,
    "indigo": 233,
    "purple": 270, "violet": 270, "lavender": 270, "lilac": 270,
    "plum": 290,
    "magenta": 310, "fuchsia": 310,
    "pink": 335, "rose": 335,
}


def _hue_deg(rgb: tuple[int, int, int]) -> float:
    h, _, _ = colorsys.rgb_to_hsv(rgb[0] / 255, rgb[1] / 255, rgb[2] / 255)
    return h * 360


def _color_to_accent(color_en: str, category: str) -> tuple[int, int, int] | None:
    c = color_en.lower()
    target = None
    for key, hue in _COLOR_HUE_HINTS.items():
        if key in c:
            target = hue
            break
    if target is None:
        return None

    def hue_dist(rgb: tuple[int, int, int]) -> float:
        d = abs(_hue_deg(rgb) - target)
        return min(d, 360 - d)

    return min(_pool_for_category(category), key=hue_dist)


# "Мешок без повторов" на каждый пул: цвета выдаются в случайном порядке, но
# каждый — не чаще раза за полный круг пула, чтобы один и тот же акцент не
# попадался слишком часто в соседних карточках.
_accent_bags: dict[str, list[tuple[int, int, int]]] = {}
_last_accents: dict[str, tuple[int, int, int]] = {}


def _next_pool_accent(category: str) -> tuple[int, int, int]:
    pool_key = "hardware" if category in _HARDWARE_CATEGORIES else "lifestyle"
    bag = _accent_bags.get(pool_key) or []
    if not bag:
        bag = _pool_for_category(category).copy()
        random.shuffle(bag)
        last = _last_accents.get(pool_key)
        if len(bag) > 1 and bag[-1] == last:
            bag[0], bag[-1] = bag[-1], bag[0]
    color = bag.pop()
    _accent_bags[pool_key] = bag
    _last_accents[pool_key] = color
    return color


async def _pick_gaming_accent(product: str, features: list[tuple[str, str]],
                               llm, color_en: str = "", category: str = "") -> tuple[int, int, int]:
    """Цвет товара → подходящий акцент; иначе фирменный цвет бренда;
    иначе из пула (по категории) без частых повторов."""
    accent = _color_to_accent(color_en, category)
    if accent:
        log.info(f"Gaming accent: product color '{color_en}' -> {accent}")
        return accent

    p_low = product.lower()
    for key, color in _GAMING_BRAND_COLORS.items():
        if key in p_low:
            log.info(f"Gaming accent: brand '{key}' -> {color}")
            return color

    color = _next_pool_accent(category)
    log.info(f"Gaming accent: pool -> {color}")
    return color


# "Мешок без повторов" для simple-пула — отдельный, чтобы не мешаться
# с gaming/lifestyle ротацией.
_simple_bag: list[tuple[int, int, int]] = []
_simple_last: tuple[int, int, int] | None = None


def _next_simple_accent() -> tuple[int, int, int]:
    global _simple_bag, _simple_last
    if not _simple_bag:
        _simple_bag = _SIMPLE_POOL.copy()
        random.shuffle(_simple_bag)
        if len(_simple_bag) > 1 and _simple_bag[-1] == _simple_last:
            _simple_bag[0], _simple_bag[-1] = _simple_bag[-1], _simple_bag[0]
    color = _simple_bag.pop()
    _simple_last = color
    return color


def _color_to_simple_accent(color_en: str) -> tuple[int, int, int] | None:
    """Ближайший по тону цвет из _SIMPLE_POOL (мягкая версия для simple)."""
    c = color_en.lower()
    target = None
    for key, hue in _COLOR_HUE_HINTS.items():
        if key in c:
            target = hue
            break
    if target is None:
        return None

    def hue_dist(rgb: tuple[int, int, int]) -> float:
        d = abs(_hue_deg(rgb) - target)
        return min(d, 360 - d)

    return min(_SIMPLE_POOL, key=hue_dist)


async def _pick_simple_accent(product: str, features: list[tuple[str, str]],
                               llm, color_en: str = "") -> tuple[int, int, int]:
    """Мягкий акцент для simple-стиля: берётся из _SIMPLE_POOL, а не из
    _LIFESTYLE_POOL, чтобы не выдавать ядовитые неоновые тона для бытовых
    товаров (кабели, зарядки, расходники)."""
    accent = _color_to_simple_accent(color_en)
    if accent:
        log.info(f"Simple accent: product color '{color_en}' -> {accent}")
        return accent
    color = _next_simple_accent()
    log.info(f"Simple accent: pool -> {color}")
    return color


async def pick_simple_accent(
    product: str,
    features: list[tuple[str, str]],
    llm,
    color_en: str = "",
    category: str = "",
) -> tuple[int, int, int]:
    """Публичная обёртка: мягкий акцент для simple-стиля."""
    return await _pick_simple_accent(product, features, llm, color_en=color_en)


def pick_alt_accent(category: str, exclude: tuple[int, int, int] | None = None) -> tuple[int, int, int]:
    """Принудительно другой акцент для /altcolor — в отличие от
    pick_gaming_accent, ИГНОРИРУЕТ детект по цвету товара/бренду (он
    детерминирован, повторный вызов вернул бы тот же цвет) и всегда берёт
    из пула. exclude — текущий акцент; если пул вернул его же (бывает на
    границе цикла "мешка без повторов"), пробуем ещё раз (до 3х)."""
    color = _next_pool_accent(category)
    tries = 0
    while color == exclude and tries < 3:
        color = _next_pool_accent(category)
        tries += 1
    return color


async def pick_gaming_accent(
    product: str,
    features: list[tuple[str, str]],
    llm,
    color_en: str = "",
    category: str = "",
) -> tuple[int, int, int]:
    """Публичная обёртка: выбирает accent-цвет для gaming-стиля."""
    return await _pick_gaming_accent(product, features, llm, color_en, category)


async def make_gaming_infographic(
    product: str,
    features: list[tuple[str, str]],
    img_bytes: bytes | None,
    llm=None,
    brand: str = "",
    category: str = "",
    accent: tuple[int, int, int] | None = None,
) -> tuple[bytes | None, str]:
    """
    Gaming-style infographic: dark bg, vivid accent, pill blocks on the right.
    Returns (jpeg_bytes | None, warning_str).
    accent — если передан, пропускаем LLM-подбор (уже выбран снаружи).
    """
    if accent is None:
        accent = await _pick_gaming_accent(product, features, llm) if llm else (215, 30, 55)
    try:
        result = await _gaming.build_gaming(
            product, features, img_bytes,
            brand=brand, category=category, accent=accent,
        )
        return result, ""
    except Exception as e:
        log.error(f"Gaming infographic failed: {e}", exc_info=True)
        return None, str(e)


async def make_speakers_infographic(
    product: str,
    features: list[tuple[str, str]],
    img_bytes: bytes | None,
    llm=None,
    brand: str = "",
    category: str = "",
    accent: tuple[int, int, int] | None = None,
    raw_specs: str = "",
) -> tuple[bytes | None, str]:
    """Speaker infographic: dark bg + accent glow + power/equalizer widget."""
    if accent is None:
        accent = await _pick_gaming_accent(product, features, llm) if llm else _speakers.DEFAULT_ACCENT
    try:
        result = await _speakers.build_speaker_widget(
            product, features, img_bytes,
            brand=brand, category=category, accent=accent,
            raw_specs=raw_specs,
        )
        return result, ""
    except Exception as e:
        log.error(f"Speakers infographic failed: {e}", exc_info=True)
        return None, str(e)


async def make_mice_infographic(
    product: str,
    features: list[tuple[str, str]],
    img_bytes: bytes | None,
    llm=None,
    brand: str = "",
    category: str = "",
    accent: tuple[int, int, int] | None = None,
    raw_specs: str = "",
) -> tuple[bytes | None, str]:
    """Mice-style infographic: dark bg + accent glow + DPI dial gauge."""
    if accent is None:
        accent = await _pick_gaming_accent(product, features, llm) if llm else _mice.DEFAULT_ACCENT
    try:
        result = await _mice.build_mice(
            product, features, img_bytes,
            brand=brand, category=category, accent=accent,
            raw_specs=raw_specs,
        )
        return result, ""
    except Exception as e:
        log.error(f"Mice infographic failed: {e}", exc_info=True)
        return None, str(e)


async def make_ram_infographic(
    product: str,
    features: list[tuple[str, str]],
    img_bytes: bytes | None,
    llm=None,
    brand: str = "",
    category: str = "",
    accent: tuple[int, int, int] | None = None,
    raw_specs: str = "",
) -> tuple[bytes | None, str]:
    """RAM infographic: dark bg + accent glow + frequency widget with XMP/EXPO badges."""
    if accent is None:
        accent = await _pick_gaming_accent(product, features, llm) if llm else _ram.DEFAULT_ACCENT
    try:
        result = await _ram.build_ram(
            product, features, img_bytes,
            brand=brand, category=category, accent=accent,
            raw_specs=raw_specs,
        )
        return result, ""
    except Exception as e:
        log.error(f"RAM infographic failed: {e}", exc_info=True)
        return None, str(e)


async def make_gpu_infographic(
    product: str,
    features: list[tuple[str, str]],
    img_bytes: bytes | None,
    llm=None,
    brand: str = "",
    category: str = "",
    accent: tuple[int, int, int] | None = None,
    raw_specs: str = "",
) -> tuple[bytes | None, str]:
    """GPU infographic: dark bg + accent glow + compact I/O ports widget."""
    if accent is None:
        accent = await _pick_gaming_accent(product, features, llm) if llm else _gpu.DEFAULT_ACCENT
    try:
        result = await _gpu.build_gpu(
            product, features, img_bytes,
            brand=brand, category=category, accent=accent,
            raw_specs=raw_specs,
        )
        return result, ""
    except Exception as e:
        log.error(f"GPU infographic failed: {e}", exc_info=True)
        return None, str(e)


async def make_watch_infographic(
    product: str,
    features: list[tuple[str, str]],
    img_bytes: bytes | None,
    llm=None,
    brand: str = "",
    category: str = "",
    accent: tuple[int, int, int] | None = None,
    raw_specs: str = "",
) -> tuple[bytes | None, str]:
    """Smart watch infographic: dark bg + accent glow + sensor grid widget."""
    if accent is None:
        accent = await _pick_gaming_accent(product, features, llm) if llm else _watch.DEFAULT_ACCENT
    try:
        result = await _watch.build_watch(
            product, features, img_bytes,
            brand=brand, category=category, accent=accent,
            raw_specs=raw_specs,
        )
        return result, ""
    except Exception as e:
        log.error(f"Watch infographic failed: {e}", exc_info=True)
        return None, str(e)


async def make_signal_infographic(
    product: str,
    features: list[tuple[str, str]],
    img_bytes: bytes | None,
    llm=None,
    brand: str = "",
    category: str = "",
    accent: tuple[int, int, int] | None = None,
    raw_specs: str = "",
) -> tuple[bytes | None, str]:
    """Радиомост/усилитель сигнала/Wi-Fi-адаптер: светлая пастельная палитра
    (как default) + signal-бары вместо циферблата."""
    if accent is None:
        accent = await _pick_gaming_accent(product, features, llm) if llm else _signal.DEFAULT_ACCENT
    try:
        result = await _signal.build_signal(
            product, features, img_bytes,
            brand=brand, category=category, accent=accent,
            raw_specs=raw_specs,
        )
        return result, ""
    except Exception as e:
        log.error(f"Signal infographic failed: {e}", exc_info=True)
        return None, str(e)


async def make_chair_infographic(
    product: str,
    features: list[tuple[str, str]],
    img_bytes: bytes | None,
    llm=None,
    brand: str = "",
    category: str = "",
    accent: tuple[int, int, int] | None = None,
    raw_specs: str = "",
) -> tuple[bytes | None, str]:
    """Игровое кресло: сетка из 4 иконок возможностей (подголовник/поясница/
    4D-подлокотники/наклон) — иконки сгенерированы через Gemini, см.
    infographic_chair.py и tools/generate_chair_icons.py."""
    if accent is None:
        accent = await _pick_gaming_accent(product, features, llm) if llm else _chair.DEFAULT_ACCENT
    try:
        result = await _chair.build_chair(
            product, features, img_bytes,
            brand=brand, category=category, accent=accent,
            raw_specs=raw_specs,
        )
        return result, ""
    except Exception as e:
        log.error(f"Chair infographic failed: {e}", exc_info=True)
        return None, str(e)


async def make_simple_infographic(
    product: str,
    features: list[tuple[str, str]],
    tips: list[tuple[str, str]],
    img_bytes: bytes | None,
    llm=None,
    brand: str = "",
    category: str = "",
    accent: tuple[int, int, int] | None = None,
) -> tuple[bytes | None, str]:
    """Стиль "simple" — для нетехнических товаров (расходники, бытовые
    товары и т.п.): сплошной яркий фон, бейджи-баннеры, нижняя строка иконок.
    См. infographic_simple.py — раскладка собрана по референсу карточки
    конкурента, донастраивается по реальным результатам."""
    if accent is None:
        accent = await _pick_gaming_accent(product, features, llm) if llm else _simple.DEFAULT_ACCENT
    header_title, header_model = await _extract_header_title_model(product, llm, category) if llm or category else ("", "")
    try:
        result = await _simple.build_simple(
            product, features, tips, img_bytes,
            brand=brand, accent=accent, header_title=header_title, header_model=header_model,
            category=category,
        )
        return result, ""
    except Exception as e:
        log.error(f"Simple infographic failed: {e}", exc_info=True)
        return None, str(e)


__all__ = ["make_infographic", "make_second_slide", "make_gaming_infographic",
           "make_speakers_infographic",
           "make_mice_infographic", "make_ram_infographic", "make_gpu_infographic",
           "make_watch_infographic", "make_signal_infographic", "make_chair_infographic",
           "make_simple_infographic", "make_richcontent",
           "pick_gaming_accent", "pick_simple_accent", "pick_alt_accent",
           "pick_bg_palette", "pick_infographic_style"]
