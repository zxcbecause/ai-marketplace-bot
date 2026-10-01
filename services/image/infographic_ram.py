"""
RAM infographic template — крупная частота + бейджи XMP/EXPO.
Layout общий с GPU/Watch (infographic_widgets): header | виджет «ЧАСТОТА»
(left, upper) | характеристики (left, lower) | зона товара (right).
"""
import re

from PIL import ImageDraw

from .fonts import fit_font, best_lines, get_font
from .infographic_widgets import (
    W, H, WIDGET_X1, WIDGET_Y1, WIDGET_X2, WIDGET_Y2, WIDGET_W,
    make_widget_bg, draw_widget_boxes, draw_widget_header, draw_chars_3,
    pick_chars, prepare_product, paste_product, finalize,
)

DEFAULT_ACCENT = (170, 90, 255)   # сирень — память/скорость

_FREQ_RE = re.compile(r'(\d{3,5})\s*(?:mhz|МГц|мгц)', re.IGNORECASE)
_DDR_FREQ_RE = re.compile(r'ddr\s*\d\s*[-/]?\s*(\d{3,5})', re.IGNORECASE)
_DDR_RE = re.compile(r'ddr\s*(\d)', re.IGNORECASE)
_SODIMM_RE = re.compile(r'so-?dimm|ноутбук|laptop|notebook', re.IGNORECASE)

_RAM_CHAR_PRIORITY = ["ОБЪЁМ", "ТАЙМИНГ", "ФОРМ-ФАКТОР", "ТИП"]


def _find_freq_index(features: list[tuple[str, str]]) -> int:
    """Индекс характеристики с частотой — для крупного числа в виджете."""
    for i, (label, value) in enumerate(features):
        if _FREQ_RE.search(value) or "ЧАСТОТ" in label.upper():
            return i
    return 0 if features else -1


def _parse_freq(value: str) -> tuple[str | None, str]:
    """Достаёт частоту («6000») и поколение («DDR5») из строки вида «DDR5-6000» / «6000 MHz».
    Поколение по умолчанию не определяется здесь — вызывающая сторона ищет его шире."""
    m = _FREQ_RE.search(value) or _DDR_FREQ_RE.search(value)
    freq = m.group(1) if m else None
    ddr_m = _DDR_RE.search(value)
    ddr = f"DDR{ddr_m.group(1)}" if ddr_m else ""
    return freq, ddr


def _ram_badges(ddr: str, full_text: str) -> list[str]:
    """Бейджи совместимости с XMP/EXPO. Если в характеристиках упомянуты
    конкретные версии — используем их, иначе дефолт по поколению памяти.
    SO-DIMM (ноутбучная память) обычно не имеет XMP/EXPO профилей —
    показываем форм-фактор и стандарт JEDEC вместо overclock-бейджей."""
    if _SODIMM_RE.search(full_text):
        return ["SO-DIMM", "JEDEC"]
    if "xmp" in full_text and "expo" in full_text:
        return ["INTEL XMP", "AMD EXPO"]
    if ddr == "DDR5":
        return ["INTEL XMP 3.0", "AMD EXPO"]
    if ddr == "DDR4":
        return ["INTEL XMP 2.0", "AMD EXPO"]
    return ["XMP", "EXPO"]


def _draw_ram_widget(img, freq_value: str, ddr: str, full_text: str,
                      accent: tuple[int, int, int]) -> None:
    draw = ImageDraw.Draw(img)
    cx = (WIDGET_X1 + WIDGET_X2) // 2
    box_w = WIDGET_W

    f_cap = fit_font(draw, "ЧАСТОТА", box_w - 24, "label", 18)
    draw.text((cx, WIDGET_Y1 + 16), "ЧАСТОТА", font=f_cap, fill=accent, anchor="mt")

    freq, freq_ddr = _parse_freq(freq_value)
    ddr = freq_ddr or ddr

    if freq:
        f_big = fit_font(draw, freq, box_w - 40, "title", 88)
        draw.text((cx, WIDGET_Y1 + 50), freq, font=f_big, fill=(245, 245, 255), anchor="mt")
        unit = f"MHz · {ddr}" if ddr else "MHz"
        f_unit = fit_font(draw, unit, box_w - 40, "label", 22)
        draw.text((cx, WIDGET_Y1 + 50 + f_big.size + 6), unit, font=f_unit, fill=accent, anchor="mt")
    else:
        # нет числовой частоты — выводим значение текстом по центру виджета
        lines, sz = best_lines(draw, freq_value, box_w - 40, "value", 40)
        f_val = get_font(sz, "value")
        line_h = sz * 1.15
        start_y = WIDGET_Y1 + 56 + (3 - len(lines)) * line_h / 2
        for j, line in enumerate(lines):
            draw.text((cx, start_y + j * line_h), line, font=f_val, fill=(245, 245, 255), anchor="mt")

    # бейджи совместимости — снизу виджета
    badges = _ram_badges(ddr, full_text)
    gap = 12
    pad = 16
    bw = (box_w - 2 * pad - gap) // 2
    bh = 46
    by1 = WIDGET_Y2 - bh - 16
    for i, txt in enumerate(badges):
        bx1 = WIDGET_X1 + pad + i * (bw + gap)
        draw.rounded_rectangle([bx1, by1, bx1 + bw, by1 + bh], radius=10,
                                outline=accent, width=2)
        f_b = fit_font(draw, txt, bw - 12, "label", 16)
        draw.text((bx1 + bw / 2, by1 + bh / 2), txt, font=f_b,
                  fill=(220, 222, 235), anchor="mm")


async def build_ram(
    product: str,
    features: list[tuple[str, str]],
    product_img_bytes: bytes | None,
    brand: str = "",
    category: str = "",
    accent: tuple[int, int, int] = DEFAULT_ACCENT,
    raw_specs: str = "",
) -> bytes:
    """Build RAM infographic (frequency + XMP/EXPO badges) and return JPEG bytes."""

    img = make_widget_bg(accent)
    product_rgba, product_dark = await prepare_product(product_img_bytes)
    rotated = False
    if product_rgba is not None:
        # Планка ОЗУ часто снята лёжа (широкий длинный прямоугольник),
        # а зона товара портретная — повернув вертикально, занимаем
        # зону почти полностью вместо тонкой полоски с пустотой.
        pw, ph = product_rgba.size
        if pw > ph * 1.4:
            product_rgba = product_rgba.rotate(90, expand=True)
            rotated = True
    draw_widget_boxes(img, accent, product_dark=product_dark)
    if product_rgba is not None:
        # После поворота планка заполняет зону впритык по высоте —
        # уменьшаем немного, чтобы остался "воздух" сверху/снизу.
        img = paste_product(img, product_rgba, accent, scale=0.8 if rotated else 1.0)

    draw_widget_header(img, "ОПЕРАТИВНАЯ ПАМЯТЬ", product, brand, accent)

    idx = _find_freq_index(features)
    if idx >= 0:
        _, freq_value = features[idx]
    else:
        freq_value = "—"

    # Если в характеристике с частотой нет числа (LLM подставил описательный
    # текст вроде «Для ноутбуков и мини-ПК») — достаём частоту из названия
    # товара, где она почти всегда указана («...DDR5 5200MHz...»).
    freq_num, _ = _parse_freq(freq_value)
    if not freq_num:
        freq_num, _ = _parse_freq(product)
        if freq_num:
            freq_value = f"{freq_num} MHz"

    full_text = (raw_specs + " " + product + " " + " ".join(f"{l} {v}" for l, v in features)).lower()
    _, ddr = _parse_freq(freq_value)
    if not ddr:
        ddr_m = _DDR_RE.search(full_text)
        ddr = f"DDR{ddr_m.group(1)}" if ddr_m else "DDR5"

    _draw_ram_widget(img, freq_value, ddr, full_text, accent)

    chars = pick_chars(features, _RAM_CHAR_PRIORITY, exclude_idx=idx)
    draw_chars_3(img, chars, accent)

    return finalize(img)
