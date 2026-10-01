"""
Стиль "simple" — инфографика для обычных (не технических) товаров, где
двухколоночная сетка ХАРАКТЕРИСТИКИ/ПРЕИМУЩЕСТВА (см. infographic.py)
выглядит избыточно — расходники, бытовые товары и т.п. без длинного списка
техн. характеристик.

v3: цветная шапка (заголовок) + пастельное (НЕ белое — белые товары/упаковка
иначе сливаются с фоном) тело карточки: товар, бейджи доверия и 3 блока
характеристик (крупный шрифт, как в обычной инфографике) вместо мелкой
строки иконок.

Контент берётся из уже извлечённых features/tips (без нового LLM-промпта):
бейджи — короткая фраза-слоган от начала текста первых tips (см.
_badge_phrase — просто заголовок-существительное читался бессмысленно без
контекста, баг 02.09.2026), блоки характеристик — features (они уже
отсортированы по приоритету категории в _extract_features, поэтому первые
3 — самые важные), либо хвост tips, если features не набралось.
"""
import colorsys
import io
import random
import re

import pymorphy3
from PIL import Image, ImageDraw, ImageFilter

from .fonts import fit_font, wrap_text
from .infographic_widgets import W, H, prepare_product
from .background import place_on_transparent
from .logo import paste_brand_watermark
from .brand_logos import get_local_brand_logo
from . import local_bg as _local_bg

DEFAULT_ACCENT = (13, 94, 224)   # синий — цвет шапки/акцентов
BADGE_COLOR    = (196, 68, 60)   # терракот — спокойнее яркого "стоп-сигнального" красного
BODY_LABEL     = (90, 100, 120)  # серый — подписи в блоках
BODY_VALUE     = (25, 30, 42)    # тёмный — значения в блоках

BAND_Y2 = 174   # высота цветной шапки (ещё -20% от 218)

TITLE_X1, TITLE_X2 = 36, W - 36
TITLE_Y1, TITLE_Y2 = 18, BAND_Y2 - 12

LOGO_W, LOGO_H = 165, 72   # зона лого бренда — верхний правый угол шапки

# Блоки характеристик опущены на 55px (CHR_Y1/Y2) — освободившееся место
# целиком отдано зоне товара (PRD_Y2 выросла на столько же): зона товара —
# единственный реальный рычаг увеличить размер фото (place_on_transparent
# масштабирует строго под высоту зоны, ширины с запасом и так достаточно).
# PRD_Y1 дополнительно поднят на те же 44px, что отрезали от шапки.
PRD_X1, PRD_X2 = 20, W - 60   # сдвинуто на 20px левее (было 40, W-40)
PRD_Y1, PRD_Y2 = 186, 722

BADGE_X1, BADGE_X2 = 440, W - 36
BADGE_Y1 = 216

CHR_X1, CHR_X2 = 30, W - 30
CHR_Y1, CHR_Y2 = 737, 910   # +55px (было 682, 855) — НИЖЕ безопасной для WB
                             # границы 860 (если эта карточка пойдёт и на WB,
                             # не только Ozon — стоит проверить, не накроет
                             # ли нижнюю плашку)
CHR_GAP = 16


def _label_color(accent: tuple[int, int, int]) -> tuple[int, int, int]:
    """Принудительно затемнённая/насыщенная версия accent — для подписи
    характеристики (мелкий текст) на пастельной карточке. Если сам accent
    светлый (как часто бывает у "холодных" акцентов вроде циана), он почти
    не отличим от пастельной подложки того же тона — поэтому не берём
    accent как есть, а ограничиваем яркость сверху независимо от исходной."""
    h, s, _ = colorsys.rgb_to_hsv(*(c / 255 for c in accent))
    r, g, b = colorsys.hsv_to_rgb(h, min(1.0, s * 1.15), 0.55)
    return int(r * 255), int(g * 255), int(b * 255)


def _pastel(accent: tuple[int, int, int], light: float = 0.85, sat: float = 0.38) -> tuple[int, int, int]:
    """Пастельный тон цвета accent (та же логика, что у local_bg.py для
    остальных стилей — светлый, но ЗАМЕТНО тонированный, не близкий к
    белому: иначе товары в белой упаковке сливаются с телом карточки)."""
    h, _, _ = colorsys.rgb_to_hsv(*(c / 255 for c in accent))
    r, g, b = colorsys.hsv_to_rgb(h, sat, light)
    return int(r * 255), int(g * 255), int(b * 255)


# Подмножество decor-библиотеки (local_bg.py) — крупные читаемые формы.
# Полный _DECOR_POOL заточен под "живой" фон под полупрозрачными белыми
# карточками (infographic.py) и содержит мелкие/разрежённые узоры (glitter,
# confetti, dot_grid, sparkles) — на пастельном теле "simple"-стиля они
# либо не видны вообще, либо выглядят как мусор/пыль на скане.
_SIMPLE_DECOR_NAMES = ["bubbles", "rings", "waves", "hexagons", "triangles",
                       "web", "arcs", "swoosh", "cross_hatch"]


def _decor_accents(accent: tuple[int, int, int]) -> dict:
    """acc1 — полнотонный (не пастельный) accent: на пастельном теле того же
    тона нужен явно темнее/насыщеннее цвет декора, иначе фигуры тонут в
    собственном фоне. acc2 — тот же тон, сдвинутый по hue для разнообразия."""
    h, s, v = colorsys.rgb_to_hsv(*(c / 255 for c in accent))
    r2, g2, b2 = colorsys.hsv_to_rgb((h + 0.08) % 1.0, min(1.0, s * 0.9), min(1.0, v * 1.05))
    return {"acc1": accent, "acc2": (int(r2 * 255), int(g2 * 255), int(b2 * 255))}


def make_simple_bg(accent: tuple[int, int, int]) -> Image.Image:
    """Цветная шапка (0..BAND_Y2) + пастельное тело с лёгким тонированным
    пятном за зоной товара. 2 слоя декора из подобранного под этот стиль
    подмножества фигур (см. _SIMPLE_DECOR_NAMES) — гарантированно, не 40%
    шанс как раньше, иначе они слишком часто оказывались не видны."""
    body = _pastel(accent)
    img = Image.new("RGB", (W, H), body)

    rnd = random.Random()
    p = _decor_accents(accent)
    name1, name2 = rnd.sample(_SIMPLE_DECOR_NAMES, 2)
    img = _local_bg._apply_decor(img, name1, rnd, p)
    img = _local_bg._apply_decor(img, name2, rnd, p)

    band = Image.new("RGB", (W, BAND_Y2), accent)
    img.paste(band, (0, 0))

    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    cx, cy = W // 2, (PRD_Y1 + PRD_Y2) // 2
    light_tint = _pastel(accent, light=0.94, sat=0.22)
    for i in range(10, 0, -1):
        alpha = int(60 * (i / 10))
        rx = int(W * 0.50 * (i / 10))
        ry = int((PRD_Y2 - PRD_Y1) * 0.55 * (i / 10))
        gd.ellipse((cx - rx, cy - ry, cx + rx, cy + ry), fill=(*light_tint, alpha))
    glow = glow.filter(ImageFilter.GaussianBlur(50))
    return Image.alpha_composite(img.convert("RGBA"), glow).convert("RGB")


def _draw_brand_logo(img: Image.Image, logo: Image.Image) -> None:
    """Лого бренда (если есть в локальной библиотеке, см. brand_logos.py) —
    верхний правый угол шапки, перекрашено в белый (как водяной знак магазина
    на тёмном фоне), вписано в LOGO_W×LOGO_H с сохранением пропорций.
    Центр лого — по центру шапки (как и название+модель)."""
    lw, lh = logo.size
    scale = min(LOGO_W / lw, LOGO_H / lh)
    logo = logo.resize((max(1, int(lw * scale)), max(1, int(lh * scale))), Image.LANCZOS)

    r, g, b, a = logo.split()
    white = Image.new("L", logo.size, 255)
    logo = Image.merge("RGBA", (white, white, white, a))

    x = TITLE_X2 - logo.width
    y = TITLE_Y1 + ((TITLE_Y2 - TITLE_Y1) - logo.height) // 2
    img.paste(logo, (x, y), logo)


def _draw_title(img: Image.Image, title: str, model: str = "", logo_reserve: int = 0) -> None:
    """title — тип товара (крупно, жирно), model — модель/бренд+название
    под ним мельче, ВПЛОТНУЮ под title (небольшой фикс. отступ, не своя
    "плита" высоты, иначе строки визуально разъезжаются). Весь блок
    название+модель центрируется по вертикали в зоне шапки TITLE_Y1..Y2.
    logo_reserve — сколько px справа освободить под лого бренда."""
    draw = ImageDraw.Draw(img)
    avail_w = TITLE_X2 - TITLE_X1 - logo_reserve
    avail_h = TITLE_Y2 - TITLE_Y1

    if not model:
        lines, font = wrap_text(draw, title.upper(), avail_w, "title", 62, max_lines=3)
        line_h = int(font.size * 1.15)
        total_h = line_h * len(lines)
        y = TITLE_Y1 + max(0, (avail_h - total_h) // 2)
        for line in lines:
            draw.text((TITLE_X1, y), line, font=font, fill=(250, 250, 255), anchor="la")
            y += line_h
        return

    lines, font = wrap_text(draw, title.upper(), avail_w, "title", 51, max_lines=2)
    line_h = int(font.size * 1.12)
    gap = 6
    f_model = fit_font(draw, model, avail_w, "label", 32)
    total_h = line_h * len(lines) + gap + f_model.size

    y = TITLE_Y1 + max(0, (avail_h - total_h) // 2)
    for line in lines:
        draw.text((TITLE_X1, y), line, font=font, fill=(250, 250, 255), anchor="la")
        y += line_h
    draw.text((TITLE_X1, y + gap), model, font=f_model, fill=(225, 230, 248), anchor="la")


_BADGE_STOPWORDS = {"для", "и", "с", "на", "не", "по", "или", "без", "от", "до", "в", "к", "из", "а"}
# Части речи, которые грамматически "требуют" продолжения — обрезать фразу
# прямо на них означает оставить висящее прилагательное/предлог без того,
# к чему оно относится (живой пример 02.09.2026: 'Особенностью модели
# является ограничение максимальной' обрывалось на голом прилагательном).
_BADGE_DANGLING_POS = {"ADJF", "PRTF", "CONJ", "PREP", "COMP", "NPRO"}
# Сказуемое (глагол) — граница именной группы: обрубаем ПЕРЕД ним, а не
# после max_words слов. 02.09.2026: пословная обрезка тезиса на 6 слов
# давала связный, но слишком длинный (2 строки) кусок текста — пользователь
# попросил "что-то более ёмкое". Подлежащее+определения (именная группа до
# первого глагола) обычно короче (2-4 слова) и само по себе осмысленно как
# тег ("НОЖНИЧНЫЙ МЕХАНИЗМ КЛАВИШ", "ТЕХНОЛОГИЯ RAPID IPS"), в отличие от
# обрубка на предикате.
_BADGE_VERB_POS = {"VERB", "INFN", "GRND", "PRTS"}
_badge_morph = pymorphy3.MorphAnalyzer()


def _is_boundary(word: str) -> bool:
    """Сказуемое или предлог/союз — граница именной группы (обрубаем ПЕРЕД
    ним, см. _badge_phrase)."""
    clean = word.strip(".,;:—- ").lower()
    if not clean:
        return True
    if clean in _BADGE_STOPWORDS:
        return True
    if re.search(r"[а-яё]", clean):
        tag = _badge_morph.parse(clean)[0].tag
        if any(pos in tag for pos in _BADGE_VERB_POS):
            return True
    return False


def _is_weak_opener(word: str) -> bool:
    """Слово, с которого плохо НАЧИНАТЬ бейдж: предлог/союз, или
    местоимение/местоименное прилагательное ('его', 'такая' — отсылка к
    контексту предыдущего предложения, которого в бейдже нет — живой пример
    02.09.2026: 'Такая схема...' читалось бессмысленно в отрыве)."""
    clean = word.strip(".,;:—- ").lower()
    if not clean:
        return True
    if clean in _BADGE_STOPWORDS:
        return True
    if re.search(r"[а-яё]", clean):
        tag = _badge_morph.parse(clean)[0].tag
        if "NPRO" in tag or "Apro" in tag:
            return True
    return False


_BADGE_BARE_NUMBER_RE = re.compile(r"^\d+([.,]\d+)?$")


def _dangling(word: str) -> bool:
    clean = word.strip(".,;:—- ").lower()
    if not clean:
        return True
    if clean in _BADGE_STOPWORDS:
        return True
    # Голое число без единицы измерения на конце ('...200') — обрубок,
    # единица (Гц/мс/Вт) почти всегда идёт следующим словом и не влезла.
    if _BADGE_BARE_NUMBER_RE.match(clean):
        return True
    if re.search(r"[а-яё]", clean):
        tag = _badge_morph.parse(clean)[0].tag
        if any(pos in tag for pos in _BADGE_DANGLING_POS):
            return True
    return False


def _capture_phrase(words: list[str], start: int, max_words: int) -> list[str]:
    """От words[start:], пропустив слабое открытие (предлог/местоимение),
    до первой границы (сказуемое/предлог/союз), не больше max_words слов."""
    i = start
    while i < len(words) and _is_weak_opener(words[i]):
        i += 1
    cut = len(words)
    for j in range(i, len(words)):
        if _is_boundary(words[j]):
            cut = j
            break
    return words[i:cut][:max_words]


def _badge_phrase(value: str, max_words: int = 4) -> str:
    """Короткий тег-слоган для красной плашки, а не одно абстрактное слово и
    не длинный обрубок предложения (02.09.2026, живые замечания подряд:
    сперва 'КОНТРАСТНОСТИ'/'СПОСОБСТВУЮЩЕЕ' — непонятные одиночные ярлыки,
    потом 6-словные фразы — 'слишком длинно', потом 'Такая схема' и вовсе
    пустая плашка). Берём именную группу от начала тезиса — слова до первого
    сказуемого, капаем max_words сверху ('НОЖНИЧНЫЙ МЕХАНИЗМ КЛАВИШ
    обеспечивает...' → 'НОЖНИЧНЫЙ МЕХАНИЗМ КЛАВИШ'). Если предложение
    начинается с предлога прямо перед сказуемым ('На входе установлен
    разъём...' — до глагола вообще ничего содержательного нет), пробуем
    именную группу ПОСЛЕ первого сказуемого вместо пустой плашки."""
    words = re.findall(r"\S+", value.strip())
    picked = _capture_phrase(words, 0, max_words)

    if not picked:
        verb_idx = next(
            (j for j, w in enumerate(words) if _is_boundary(w) and re.search(r"[а-яё]", w, re.IGNORECASE)),
            None,
        )
        if verb_idx is not None:
            picked = _capture_phrase(words, verb_idx + 1, max_words)

    while picked and _dangling(picked[-1]):
        picked.pop()

    if len(picked) == 1:
        # Одно слово без соседей, за чей падеж можно "спрятаться" —
        # ставим в именительный (иначе может остаться в косвенном падеже
        # исходного предложения, напр. 'входе' вместо 'вход'). На фразы
        # из 2+ слов НЕ распространяем — сломает согласование ('клавиш' в
        # 'механизм клавиш' должно остаться родительным).
        clean = picked[0].strip(",.;:—- ")
        if re.search(r"[а-яё]", clean, re.IGNORECASE):
            picked = [_badge_morph.parse(clean.lower())[0].normal_form]

    return " ".join(picked).rstrip(",.;:—- ")


def _draw_badges(img: Image.Image, badges: list[str]) -> None:
    if not badges:
        return
    draw = ImageDraw.Draw(img)
    bw = BADGE_X2 - BADGE_X1
    gap = 16
    y = BADGE_Y1
    for text in badges[:2]:
        lines, font = wrap_text(draw, text.upper(), bw - 36, "label", 24, max_lines=2)
        line_h = int(font.size * 1.25)
        bh = line_h * len(lines) + 28
        draw.rounded_rectangle([BADGE_X1 + 3, y + 4, BADGE_X2 + 3, y + bh + 4],
                                radius=14, fill=(0, 0, 0, 40))
        draw.rounded_rectangle([BADGE_X1, y, BADGE_X2, y + bh], radius=14, fill=BADGE_COLOR)
        ty = y + (bh - line_h * len(lines)) // 2
        cx = (BADGE_X1 + BADGE_X2) // 2
        for line in lines:
            draw.text((cx, ty), line, font=font, fill=(255, 255, 255), anchor="ma")
            ty += line_h
        y += bh + gap


# Таблица UK → RU (мужские/унисекс размеры, половинки включены)
_UK_TO_RU: dict[str, str] = {
    "3": "35.5", "3.5": "36", "4": "36.5", "4.5": "37", "5": "37.5",
    "5.5": "38", "6": "38.5", "6.5": "39", "7": "40", "7.5": "40.5",
    "8": "41", "8.5": "42", "9": "42.5", "9.5": "43", "10": "44",
    "10.5": "44.5", "11": "45", "11.5": "45.5", "12": "46", "12.5": "47",
    "13": "47.5", "14": "48.5",
}


def _enrich_size_value(label: str, value: str) -> str:
    """Если поле — размер обуви с UK, дописывает российский эквивалент."""
    if "размер" not in label.lower():
        return value
    m = re.search(r"(\d+(?:\.\d+)?)\s*UK", value, re.IGNORECASE)
    if not m:
        return value
    uk = m.group(1)
    ru = _UK_TO_RU.get(uk)
    if ru and f"RU" not in value and ru not in value:
        return f"{value}\n{ru} RU"
    return value


def _soft_breaks(text: str) -> str:
    """wrap_text бьёт строки только по пробелам — длинные списки вида
    "115/315/410/415/419" или "SBC, AAC, L2HC" без пробела после разделителя
    превращаются в одно "слово" и либо вылезают за карточку, либо обрезаются
    "…". Добавляем пробел после /,; (если его там нет) — тогда wrap_text
    сможет красиво перенести список на следующую строку."""
    return re.sub(r"([/,;])(?!\s)", r"\1 ", text)


def _truncate_to_width(draw: ImageDraw.ImageDraw, text: str, font, max_w: int) -> str:
    """Финальная защита — если даже после _soft_breaks отдельный сегмент
    (например, очень длинное число) всё равно не влезает, обрезаем с "…"."""
    if draw.textlength(text, font=font) <= max_w:
        return text
    while text and draw.textlength(text + "…", font=font) > max_w:
        text = text[:-1]
    return f"{text}…" if text else "…"


def _draw_char_blocks(img: Image.Image, items: list[tuple[str, str]],
                       accent: tuple[int, int, int]) -> None:
    """До 3 характеристик — отдельными карточками. Подложка — полупрозрачная
    пастельная (тон accent, светлее тела), а не сплошная белая: так блоки
    выглядят частью общей цветовой подачи, а не наклеенными белыми бумажками."""
    n = min(3, len(items))
    if n == 0:
        return
    total_w = CHR_X2 - CHR_X1
    block_w = (total_w - CHR_GAP * (n - 1)) // n
    block_h = CHR_Y2 - CHR_Y1
    pad = 16

    card_color = _pastel(accent, light=0.97, sat=0.28)
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    box_x: list[int] = []
    for i in range(n):
        bx = CHR_X1 + i * (block_w + CHR_GAP)
        box_x.append(bx)
        od.rounded_rectangle([bx + 3, CHR_Y1 + 5, bx + block_w + 3, CHR_Y1 + block_h + 5],
                              radius=18, fill=(0, 0, 0, 30))
        od.rounded_rectangle([bx, CHR_Y1, bx + block_w, CHR_Y1 + block_h],
                              radius=18, fill=(*card_color, 215))
    img.paste(Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB"), (0, 0))

    draw = ImageDraw.Draw(img)
    for i, (label, value) in enumerate(items[:n]):
        bx = box_x[i]
        cx = bx + block_w // 2
        text_w = block_w - pad * 2

        # Перенос в 2 строки вместо схлопывания шрифта в один — длинные
        # лейблы вида "КОЛИЧЕСТВО ПРЕДМЕТОВ В УПАКОВКЕ (ШТ.)" раньше
        # ужимались в одну строку почти нечитаемым мелким кеглем, пока
        # соседний короткий лейбл ("СОСТАВ") оставался крупным (живое
        # замечание 02.09.2026).
        lbl = label.upper()
        lbl_lines, f_lbl = wrap_text(draw, lbl, text_w, "label", 17, max_lines=2)
        lbl_line_h = int(f_lbl.size * 1.15)
        lbl_y = CHR_Y1 + pad + 6
        for j, lbl_line in enumerate(lbl_lines):
            draw.text((cx, lbl_y + j * lbl_line_h), lbl_line, font=f_lbl,
                      fill=_label_color(accent), anchor="ma")

        val_top = lbl_y + lbl_line_h * len(lbl_lines) + 10
        val_bottom = CHR_Y1 + block_h - pad
        val_h = val_bottom - val_top

        value = _enrich_size_value(label, value)
        lines, f_val = wrap_text(draw, _soft_breaks(value), text_w, "value", 32, max_lines=4)
        line_h = int(f_val.size * 1.18)
        # обрезаем строки которые не влезают по высоте
        max_visible = max(1, val_h // line_h)
        lines = lines[:max_visible]
        total_h = line_h * len(lines)
        ty = val_top + max(0, (val_h - total_h) // 2) + line_h // 2
        for line in lines:
            line = _truncate_to_width(draw, line, f_val, text_w)
            draw.text((cx, ty), line, font=f_val, fill=BODY_VALUE, anchor="mm")
            ty += line_h


async def build_simple(
    product: str,
    features: list[tuple[str, str]],
    tips: list[tuple[str, str]],
    product_img_bytes: bytes | None,
    brand: str = "",
    accent: tuple[int, int, int] = DEFAULT_ACCENT,
    header_title: str = "",
    header_model: str = "",
    category: str = "",
) -> bytes:
    """Инфографика стиля "simple" для нетехнических товаров.
    Бейджи — короткая фраза-слоган от текста первых tips (_badge_phrase),
    блоки характеристик — features (если есть) либо хвост tips."""
    img = make_simple_bg(accent)

    product_rgba, _ = await prepare_product(product_img_bytes, category=category)
    if product_rgba is not None:
        zone_w, zone_h = PRD_X2 - PRD_X1, PRD_Y2 - PRD_Y1
        zone = place_on_transparent(product_rgba, zone_w, zone_h, padding=10)
        shadow_alpha = zone.split()[3].filter(ImageFilter.GaussianBlur(20))
        shadow = Image.new("RGBA", zone.size, (0, 0, 0, 0))
        shadow.putalpha(shadow_alpha.point(lambda x: int(x * 0.30)))
        img_rgba = img.convert("RGBA")
        img_rgba.paste(shadow, (PRD_X1 + 14, PRD_Y1 + 18), shadow)
        img_rgba.paste(zone, (PRD_X1, PRD_Y1), zone)
        img = img_rgba.convert("RGB")

    logo = get_local_brand_logo(brand) if brand else None
    _draw_title(img, header_title or product, model=header_model,
                logo_reserve=(LOGO_W + 16) if logo else 0)
    if logo:
        _draw_brand_logo(img, logo)

    block_items = features[:3] if features else tips[2:5]
    _draw_char_blocks(img, block_items, accent)

    result = paste_brand_watermark(img, corner="bottom-left", scale=0.32, bg_dark=False)
    buf = io.BytesIO()
    result.save(buf, format="JPEG", quality=92)
    return buf.getvalue()
