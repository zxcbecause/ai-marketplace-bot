"""
Speaker/audio infographic template (draft) — крупная мощность + эквалайзер.
Layout общий с RAM/GPU/Watch (infographic_widgets): header | виджет «МОЩНОСТЬ»
(left, upper, EQ-бары) | характеристики (left, lower) | зона товара (right).
"""
import re

from PIL import ImageDraw

from .fonts import fit_font
from .infographic_widgets import (
    WIDGET_X1, WIDGET_Y1, WIDGET_X2, WIDGET_Y2, WIDGET_W,
    make_widget_bg, draw_widget_boxes, draw_widget_header, draw_chars_3,
    pick_chars, prepare_product, paste_product, finalize,
)

DEFAULT_ACCENT = (255, 110, 60)   # оранжевый — звук/энергия

_STEREO_POWER_RE = re.compile(r'(\d{1,3})\s*[xх×]\s*(\d{1,4})\s*(?:вт|w)\b', re.IGNORECASE)
_POWER_RE = re.compile(r'(\d{1,4})\s*(?:вт|w|watt)\b', re.IGNORECASE)

_SPEAKER_CHAR_PRIORITY = ["АВТОНОМНОСТ", "ПОДКЛЮЧЕНИЕ", "ДИАПАЗОН", "КОНФИГУРАЦИЯ"]
_POWER_KEYWORDS = ["МОЩНОСТЬ", "POWER"]

# высоты столбиков эквалайзера (доля от доступной высоты) — фиксированный "профиль"
_EQ_HEIGHTS = [0.35, 0.6, 0.85, 1.0, 0.7, 0.9, 0.5, 0.75, 0.4]


def _find_power_index(features: list[tuple[str, str]]) -> int:
    for i, (label, value) in enumerate(features):
        if any(kw in label.upper() for kw in _POWER_KEYWORDS):
            return i
    for i, (_, value) in enumerate(features):
        if _POWER_RE.search(value):
            return i
    return -1


def _parse_power(text: str) -> str | None:
    m = _STEREO_POWER_RE.search(text)
    if m:
        return f"{m.group(1)}×{m.group(2)}"
    m = _POWER_RE.search(text)
    if m:
        return m.group(1)
    return None


def _draw_eq_bars(draw, x1, y1, x2, y2, accent, dim) -> None:
    """Декоративный эквалайзер — столбики разной высоты, дном на y2."""
    n = len(_EQ_HEIGHTS)
    gap = 6
    bar_w = (x2 - x1 - gap * (n - 1)) / n
    max_h = y2 - y1
    for i, frac in enumerate(_EQ_HEIGHTS):
        bx1 = x1 + i * (bar_w + gap)
        bx2 = bx1 + bar_w
        bh = max_h * frac
        color = accent if i % 2 == 0 else dim
        draw.rounded_rectangle([bx1, y2 - bh, bx2, y2], radius=bar_w / 2, fill=color)


def _draw_speaker_widget(img, power_text: str | None, accent: tuple[int, int, int]) -> None:
    draw = ImageDraw.Draw(img)
    cx = (WIDGET_X1 + WIDGET_X2) // 2
    box_w = WIDGET_W

    f_cap = fit_font(draw, "МОЩНОСТЬ", box_w - 24, "label", 18)
    draw.text((cx, WIDGET_Y1 + 14), "МОЩНОСТЬ", font=f_cap, fill=accent, anchor="mt")

    big_text = power_text or "—"
    f_big = fit_font(draw, big_text, box_w - 40, "title", 76)
    draw.text((cx, WIDGET_Y1 + 48), big_text, font=f_big, fill=(245, 245, 255), anchor="mt")
    if power_text:
        f_unit = fit_font(draw, "ВТ", box_w - 40, "label", 22)
        draw.text((cx, WIDGET_Y1 + 48 + f_big.size + 4), "ВТ", font=f_unit, fill=accent, anchor="mt")

    # эквалайзер — нижняя треть виджета
    eq_y1 = WIDGET_Y1 + 150
    eq_y2 = WIDGET_Y2 - 16
    dim = (60, 63, 78)
    _draw_eq_bars(draw, WIDGET_X1 + 16, eq_y1, WIDGET_X2 - 16, eq_y2, accent, dim)


async def build_speaker_widget(
    product: str,
    features: list[tuple[str, str]],
    product_img_bytes: bytes | None,
    brand: str = "",
    category: str = "",
    accent: tuple[int, int, int] = DEFAULT_ACCENT,
    raw_specs: str = "",
) -> bytes:
    """Build speaker/audio infographic (power + equalizer widget) and return JPEG bytes."""

    img = make_widget_bg(accent)
    product_rgba, product_dark = await prepare_product(product_img_bytes)
    draw_widget_boxes(img, accent, product_dark=product_dark)
    if product_rgba is not None:
        img = paste_product(img, product_rgba, accent)

    draw_widget_header(img, "КОЛОНКА", product, brand, accent)

    idx = _find_power_index(features)
    power_text = _parse_power(features[idx][1]) if idx >= 0 else None
    if not power_text:
        power_text = _parse_power(product) or _parse_power(raw_specs)

    _draw_speaker_widget(img, power_text, accent)

    chars = pick_chars(features, _SPEAKER_CHAR_PRIORITY, exclude_idx=idx)
    draw_chars_3(img, chars, accent)

    return finalize(img)
