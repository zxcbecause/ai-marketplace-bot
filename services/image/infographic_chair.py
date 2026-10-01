"""
Инфографика для игровых кресел — сетка иконок возможностей (аналог
infographic_watch.py, тот же общий layout из infographic_widgets.py),
4 иконки: подголовник, поясничная подушка, 4D-подлокотники, наклон/газлифт.
Активна — если в характеристиках/исходном тексте нашлось соответствующее
ключевое слово, иначе серая (не известно, есть или нет).

Иконки — не векторная рисовка PIL, а PNG-силуэты, сгенерированные один раз
через Gemini (см. tools/generate_chair_icons.py) и лежащие в icons/chair/ —
перекрашиваются программно в нужный цвет (accent/dim), как лого брендов
в brand_logos.py: один ассет на все состояния.
"""
import logging
from pathlib import Path

from PIL import Image, ImageDraw

from .fonts import fit_font
from .infographic_widgets import (
    WIDGET_X1, WIDGET_Y1, WIDGET_X2, WIDGET_Y2, WIDGET_W,
    make_widget_bg, draw_widget_boxes, draw_widget_header, draw_chars_3,
    pick_chars, prepare_product, paste_product, finalize,
)

log = logging.getLogger(__name__)

DEFAULT_ACCENT = (224, 84, 154)   # малиновый — кресла/комфорт, отличается от прочих категорий

_CHAIR_CHAR_PRIORITY = ["МАТЕРИАЛ", "НАГРУЗКА", "МЕХАНИЗМ", "КОЛЁСА"]

_ICONS_DIR = Path(__file__).resolve().parent / "icons" / "chair"

_FEATURES = [
    ("headrest", "ПОДГОЛОВНИК", ("подголовник", "headrest")),
    ("lumbar",   "ПОЯСНИЦА",    ("пояснич", "lumbar")),
    ("armrest",  "4D ПОДЛОКОТ", ("4d", "3d", "подлокот", "armrest")),
    ("recline",  "НАКЛОН",      ("наклон", "газлифт", "откид", "recline")),
]

_icon_cache: dict[str, Image.Image | None] = {}


def _load_icon(name: str) -> Image.Image | None:
    if name in _icon_cache:
        return _icon_cache[name]
    path = _ICONS_DIR / f"{name}.png"
    img = None
    if path.exists():
        try:
            img = Image.open(path).convert("RGBA")
        except Exception as e:
            log.warning(f"Chair icon load failed for {name!r}: {e}")
    _icon_cache[name] = img
    return img


def _recolor(icon: Image.Image, color: tuple[int, int, int]) -> Image.Image:
    """Перекрашивает силуэт (RGB заменяется на сплошной цвет, alpha сохраняется)."""
    a = icon.split()[3]
    r, g, b = color
    solid = Image.new("RGBA", icon.size, (r, g, b, 0))
    solid.putalpha(a)
    return solid


def _detect_features(text: str) -> list[bool]:
    text_low = text.lower()
    active = [any(k in text_low for k in keys) for _, _, keys in _FEATURES]
    if not any(active):
        return [True] * len(_FEATURES)
    return active


def _draw_chair_widget(img, full_text: str, accent: tuple[int, int, int]) -> None:
    # Всё рисуем на одном RGBA-буфере (эллипсы + иконки-PNG + текст), иначе
    # порядок конвертаций RGB<->RGBA теряет уже нарисованное.
    img_rgba = img.convert("RGBA")
    draw = ImageDraw.Draw(img_rgba)
    cx = (WIDGET_X1 + WIDGET_X2) // 2

    f_cap = fit_font(draw, "ВОЗМОЖНОСТИ", WIDGET_W - 24, "label", 18)
    draw.text((cx, WIDGET_Y1 + 14), "ВОЗМОЖНОСТИ", font=f_cap, fill=accent, anchor="mt")

    active = _detect_features(full_text)
    dim = (90, 93, 108)

    cols, rows = 2, 2
    grid_x1, grid_y1 = WIDGET_X1 + 8, WIDGET_Y1 + 46
    grid_x2, grid_y2 = WIDGET_X2 - 8, WIDGET_Y2 - 8
    cell_w = (grid_x2 - grid_x1) / cols
    cell_h = (grid_y2 - grid_y1) / rows

    for idx, (icon_key, label, _) in enumerate(_FEATURES):
        col, row = idx % cols, idx // cols
        ccx = grid_x1 + cell_w * (col + 0.5)
        ccy = grid_y1 + cell_h * row + cell_h * 0.36
        badge_r = min(cell_w, cell_h) * 0.30
        color = accent if active[idx] else dim
        draw.ellipse([ccx - badge_r, ccy - badge_r, ccx + badge_r, ccy + badge_r],
                      fill=(10, 11, 16), outline=color, width=2)

        icon = _load_icon(icon_key)
        if icon is not None:
            icon_size = int(badge_r * 1.35)
            scale = min(icon_size / icon.width, icon_size / icon.height)
            iw, ih = max(1, int(icon.width * scale)), max(1, int(icon.height * scale))
            resized = icon.resize((iw, ih), Image.LANCZOS)
            colored = _recolor(resized, color)
            img_rgba.paste(colored, (int(ccx - iw / 2), int(ccy - ih / 2)), colored)

        f_l = fit_font(draw, label, int(cell_w - 6), "label", 14)
        lbl_color = (200, 205, 222) if active[idx] else (120, 123, 138)
        draw.text((ccx, ccy + badge_r + 8), label, font=f_l, fill=lbl_color, anchor="mt")

    img.paste(img_rgba.convert("RGB"), (0, 0))


async def build_chair(
    product: str,
    features: list[tuple[str, str]],
    product_img_bytes: bytes | None,
    brand: str = "",
    category: str = "",
    accent: tuple[int, int, int] = DEFAULT_ACCENT,
    raw_specs: str = "",
) -> bytes:
    """Build gaming chair infographic (feature grid: 4 icons) and return JPEG bytes."""
    img = make_widget_bg(accent)
    product_rgba, product_dark = await prepare_product(product_img_bytes)
    draw_widget_boxes(img, accent, product_dark=product_dark)
    if product_rgba is not None:
        img = paste_product(img, product_rgba, accent)

    draw_widget_header(img, "ИГРОВОЕ КРЕСЛО", product, brand, accent)

    full_text = " ".join([raw_specs, product] + [f"{l} {v}" for l, v in features])
    _draw_chair_widget(img, full_text, accent)

    chars = pick_chars(features, _CHAIR_CHAR_PRIORITY)
    draw_chars_3(img, chars, accent)

    return finalize(img)
