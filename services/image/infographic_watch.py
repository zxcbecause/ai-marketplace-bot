"""
Smart watch infographic template — сетка иконок-датчиков (вариант C).
Layout общий с RAM/GPU (infographic_widgets): header | виджет «ДАТЧИКИ И ФУНКЦИИ»
(left, upper) | характеристики (left, lower) | зона товара (right).
"""
from PIL import ImageDraw

from .fonts import fit_font
from .infographic_widgets import (
    WIDGET_X1, WIDGET_Y1, WIDGET_X2, WIDGET_Y2, WIDGET_W,
    make_widget_bg, draw_widget_boxes, draw_widget_header, draw_chars_3,
    pick_chars, prepare_product, paste_product, finalize,
)

DEFAULT_ACCENT = (0, 210, 255)   # циан — экран/технологии

_WATCH_CHAR_PRIORITY = ["ЭКРАН", "ВОДОЗАЩИТА", "АВТОНОМНОСТЬ", "ПРОЦЕССОР"]
_SENSOR_KEYWORDS = ["ДАТЧИК", "ФУНКЦ", "СЕНСОР"]


# ---------------------------------------------------------------- иконки датчиков
def _icon_heart(draw, cx, cy, s, color):
    r = s * 0.28
    draw.ellipse([cx - r * 2, cy - r, cx, cy + r], fill=color)
    draw.ellipse([cx, cy - r, cx + r * 2, cy + r], fill=color)
    draw.polygon([(cx - r * 2, cy + r * 0.2), (cx + r * 2, cy + r * 0.2), (cx, cy + r * 2.2)], fill=color)


def _icon_drop(draw, cx, cy, s, color):
    r = s * 0.32
    draw.polygon([(cx, cy - r * 1.8), (cx - r, cy + r * 0.2), (cx + r, cy + r * 0.2)], fill=color)
    draw.ellipse([cx - r, cy - r * 0.3, cx + r, cy + r * 1.7], fill=color)


def _icon_pin(draw, cx, cy, s, color):
    r = s * 0.30
    draw.ellipse([cx - r, cy - r * 1.6, cx + r, cy + r * 0.4], fill=color)
    draw.polygon([(cx - r * 0.8, cy - r * 0.1), (cx + r * 0.8, cy - r * 0.1), (cx, cy + r * 1.6)], fill=color)
    draw.ellipse([cx - r * 0.35, cy - r * 1.25, cx + r * 0.35, cy - r * 0.55], fill=(10, 11, 16))


def _icon_nfc(draw, cx, cy, s, color):
    for r in [s * 0.55, s * 0.38, s * 0.20]:
        draw.arc([cx - r, cy - r, cx + r, cy + r], -55, 55, fill=color, width=4)
    draw.ellipse([cx - 3, cy - 3, cx + 3, cy + 3], fill=color)


def _icon_moon(draw, cx, cy, s, color, bg):
    r = s * 0.36
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color)
    draw.ellipse([cx - r * 0.3, cy - r * 1.2, cx + r * 1.4, cy + r * 0.4], fill=bg)


def _icon_steps(draw, cx, cy, s, color):
    r = s * 0.22
    draw.ellipse([cx - r * 1.3, cy - r * 0.6, cx + r * 0.5, cy + r * 1.6], fill=color)
    for dx, dy in [(-r * 1.1, -r * 1.3), (-r * 0.3, -r * 1.5), (r * 0.4, -r * 1.2)]:
        draw.ellipse([cx + dx - r * 0.28, cy + dy - r * 0.28, cx + dx + r * 0.28, cy + dy + r * 0.28], fill=color)


_SENSOR_ICONS = [
    (_icon_heart, "ПУЛЬС", ("пульс", "чсс", "heart")),
    (_icon_drop, "SpO2", ("spo2", "кислород", "sp02")),
    (_icon_pin, "GPS", ("gps", "глонасс", "навигац")),
    (_icon_nfc, "NFC", ("nfc",)),
    (_icon_moon, "СОН", ("сон", "sleep")),
    (_icon_steps, "ШАГИ", ("шаг", "акселерометр", "педометр", "степ")),
]


def _find_sensors_value(features: list[tuple[str, str]]) -> tuple[int, str]:
    """Индекс и текст характеристики с датчиками/функциями. Если не найдена — (-1, "")."""
    for i, (label, value) in enumerate(features):
        if any(kw in label.upper() for kw in _SENSOR_KEYWORDS):
            return i, value
    return -1, ""


def _detect_sensors(text: str) -> list[bool]:
    """Какие из 6 иконок «активны» (товар имеет эту функцию). Если по тексту
    ничего не определилось — считаем все активными (не вводим в заблуждение)."""
    text_low = text.lower()
    active = [any(k in text_low for k in keys) for _, _, keys in _SENSOR_ICONS]
    if not any(active):
        return [True] * len(_SENSOR_ICONS)
    return active


def _draw_watch_widget(img, sensors_text: str, accent: tuple[int, int, int]) -> None:
    draw = ImageDraw.Draw(img)
    cx = (WIDGET_X1 + WIDGET_X2) // 2

    f_cap = fit_font(draw, "ДАТЧИКИ И ФУНКЦИИ", WIDGET_W - 24, "label", 18)
    draw.text((cx, WIDGET_Y1 + 14), "ДАТЧИКИ И ФУНКЦИИ", font=f_cap, fill=accent, anchor="mt")

    active = _detect_sensors(sensors_text)
    dim = (60, 63, 78)

    cols, rows = 3, 2
    grid_x1, grid_y1 = WIDGET_X1 + 8, WIDGET_Y1 + 46
    grid_x2, grid_y2 = WIDGET_X2 - 8, WIDGET_Y2 - 8
    cell_w = (grid_x2 - grid_x1) / cols
    cell_h = (grid_y2 - grid_y1) / rows

    for idx, (icon_fn, label, _) in enumerate(_SENSOR_ICONS):
        col, row = idx % cols, idx // cols
        ccx = grid_x1 + cell_w * (col + 0.5)
        ccy = grid_y1 + cell_h * row + cell_h * 0.34
        badge_r = min(cell_w, cell_h) * 0.30
        color = accent if active[idx] else dim
        outline = accent if active[idx] else dim
        draw.ellipse([ccx - badge_r, ccy - badge_r, ccx + badge_r, ccy + badge_r],
                      fill=(10, 11, 16), outline=outline, width=2)
        if icon_fn is _icon_moon:
            icon_fn(draw, ccx, ccy, badge_r * 1.6, color, (10, 11, 16))
        else:
            icon_fn(draw, ccx, ccy, badge_r * 1.6, color)
        f_l = fit_font(draw, label, int(cell_w - 6), "label", 15)
        lbl_color = (200, 205, 222) if active[idx] else (110, 113, 130)
        draw.text((ccx, ccy + badge_r + 8), label, font=f_l, fill=lbl_color, anchor="mt")


async def build_watch(
    product: str,
    features: list[tuple[str, str]],
    product_img_bytes: bytes | None,
    brand: str = "",
    category: str = "",
    accent: tuple[int, int, int] = DEFAULT_ACCENT,
    raw_specs: str = "",
) -> bytes:
    """Build smart watch infographic (sensor grid) and return JPEG bytes."""

    img = make_widget_bg(accent)
    product_rgba, product_dark = await prepare_product(product_img_bytes)
    draw_widget_boxes(img, accent, product_dark=product_dark)
    if product_rgba is not None:
        img = paste_product(img, product_rgba, accent)

    draw_widget_header(img, "СМАРТ-ЧАСЫ", product, brand, accent)

    idx, sensors_value = _find_sensors_value(features)
    full_text = " ".join([sensors_value, raw_specs] + [f"{l} {v}" for l, v in features])
    _draw_watch_widget(img, full_text, accent)

    chars = pick_chars(features, _WATCH_CHAR_PRIORITY, exclude_idx=idx)
    draw_chars_3(img, chars, accent)

    return finalize(img)
