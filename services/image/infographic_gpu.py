"""
GPU infographic template — компактная панель разъёмов (вариант A).
Layout общий с RAM/Watch (infographic_widgets): header | виджет «РАЗЪЁМЫ»
(left, upper) | характеристики (left, lower) | зона товара (right).
"""
import re

from PIL import ImageDraw

from .fonts import fit_font
from .infographic_widgets import (
    WIDGET_X1, WIDGET_Y1, WIDGET_X2, WIDGET_Y2, WIDGET_W,
    make_widget_bg, draw_widget_boxes, draw_widget_header, draw_chars_3,
    pick_chars, prepare_product, paste_product, finalize,
)

DEFAULT_ACCENT = (70, 230, 140)   # зелёный — производительность

_TDP_RE = re.compile(r'(\d{2,4})\s*w', re.IGNORECASE)

_GPU_CHAR_PRIORITY = ["ВИДЕОПАМЯТ", "ЧАСТОТ", "TDP", "ТИП ПАМЯТИ"]
_TDP_KEYWORDS = ["TDP", "ПОТРЕБЛ", "МОЩНОСТЬ"]


def _find_tdp_value(features: list[tuple[str, str]]) -> str:
    for label, value in features:
        if any(kw in label.upper() for kw in _TDP_KEYWORDS):
            return value
    return ""


def _parse_tdp(value: str) -> int | None:
    m = _TDP_RE.search(value)
    return int(m.group(1)) if m else None


def _power_connector(tdp: int | None) -> str:
    """Эвристика разъёма питания по TDP — точные данные обычно не извлекаются LLM."""
    if tdp is None:
        return "1× 8-PIN"
    if tdp >= 350:
        return "2× 16-PIN (12VHPWR)"
    if tdp >= 250:
        return "1× 16-PIN (12VHPWR)"
    if tdp >= 150:
        return "1× 8-PIN"
    return "PCIe SLOT"


def _draw_gpu_widget(img, tdp_value: str, accent: tuple[int, int, int]) -> None:
    draw = ImageDraw.Draw(img)
    cx = (WIDGET_X1 + WIDGET_X2) // 2
    box_w = WIDGET_W

    f_cap = fit_font(draw, "РАЗЪЁМЫ", box_w - 24, "label", 18)
    draw.text((cx, WIDGET_Y1 + 14), "РАЗЪЁМЫ", font=f_cap, fill=accent, anchor="mt")

    # компактная I/O-панель
    panel_w, panel_h = box_w - 32, 96
    px1 = WIDGET_X1 + 16
    py1 = WIDGET_Y1 + 46
    px2 = px1 + panel_w
    py2 = py1 + panel_h
    draw.rounded_rectangle([px1, py1, px2, py2], radius=10, fill=(30, 32, 40),
                            outline=(80, 83, 100), width=2)

    ports = ["HDMI", "DP", "DP", "USB-C"]
    gap = 10
    n = len(ports)
    pw = (panel_w - 20 - gap * (n - 1)) / n
    ph = 32
    px = px1 + 10
    py = py1 + 14
    for label in ports:
        draw.rounded_rectangle([px, py, px + pw, py + ph], radius=6, fill=accent)
        f_l = fit_font(draw, label, int(pw + gap - 2), "label", 14)
        draw.text((px + pw / 2, py + ph + 8), label, font=f_l,
                  fill=(200, 205, 222), anchor="mt")
        px += pw + gap

    tdp = _parse_tdp(tdp_value)
    power_text = f"ПИТАНИЕ: {_power_connector(tdp)}"
    f_l2 = fit_font(draw, power_text, panel_w, "label", 21)
    draw.text((cx, py2 + 22), power_text, font=f_l2, fill=accent, anchor="mt")
    f_l3 = fit_font(draw, "2.5 SLOT · PCIe 4.0 x16", panel_w, "label", 21)
    draw.text((cx, py2 + 22 + f_l2.size + 6), "2.5 SLOT · PCIe 4.0 x16", font=f_l3,
              fill=(210, 215, 230), anchor="mt")


async def build_gpu(
    product: str,
    features: list[tuple[str, str]],
    product_img_bytes: bytes | None,
    brand: str = "",
    category: str = "",
    accent: tuple[int, int, int] = DEFAULT_ACCENT,
    raw_specs: str = "",
) -> bytes:
    """Build GPU infographic (compact I/O panel) and return JPEG bytes."""

    img = make_widget_bg(accent)
    product_rgba, product_dark = await prepare_product(product_img_bytes)
    draw_widget_boxes(img, accent, product_dark=product_dark)
    if product_rgba is not None:
        img = paste_product(img, product_rgba, accent)

    draw_widget_header(img, "ВИДЕОКАРТА", product, brand, accent)

    tdp_value = _find_tdp_value(features)
    _draw_gpu_widget(img, tdp_value, accent)

    chars = pick_chars(features, _GPU_CHAR_PRIORITY)
    draw_chars_3(img, chars, accent)

    return finalize(img)
