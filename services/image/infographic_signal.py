"""
Инфографика для радиомостов/усилителей сигнала/Wi-Fi-адаптеров и похожих
устройств связи — layout как у мышей/ОЗУ/видеокарт (шапка | виджет
слева-сверху | характеристики слева-снизу | товар справа), виджет —
signal-бары (как индикатор силы сигнала на телефоне): 5 столбиков
возрастающей высоты, закрашены по уровню значения.

Фон и карточки — НЕ тёмная gaming-эстетика (как у мышей/ОЗУ), а светлая
пастельная палитра из local_bg.py, та же что у обычного default-стиля
(infographic.py): градиент + декор + полупрозрачные белые карточки текста,
тёмный текст. Для этой категории (радиомосты/усилители) тёмный "техно"-вид
оказался лишним — спокойнее смотрится как обычная карточка товара.

Метрика для баров подбирается автоматически по ключевым словам в
characteristics — дальность (км) / усиление (дБ) / скорость (Мбит/с),
смотря что реально есть у товара (см. _METRICS).
"""
import io
import logging
import re

from PIL import Image, ImageDraw, ImageFilter

from .fonts import fit_font, best_lines, get_font
from .background import remove_background, remove_solid_background, place_on_transparent
from utils.gpu_safety import run_gpu
from .logo import paste_brand_watermark
from .infographic_simple import _label_color
from . import local_bg as _local_bg

log = logging.getLogger(__name__)

W, H = 768, 1024
RADIUS = 16

HDR_X1, HDR_Y1 = 185, 26
HDR_X2, HDR_Y2 = 730, 198
HDR_H = HDR_Y2 - HDR_Y1

DIAL_X1, DIAL_Y1 = 16, 212
DIAL_X2, DIAL_Y2 = 280, 474
DIAL_W = DIAL_X2 - DIAL_X1
DIAL_H = DIAL_Y2 - DIAL_Y1

CHR_X1, CHR_Y1 = 16, 486
CHR_X2, CHR_Y2 = 280, 856
CHR_W = CHR_X2 - CHR_X1
CHR_H = CHR_Y2 - CHR_Y1

PRD_X1, PRD_Y1 = 285, 212
PRD_X2, PRD_Y2 = 748, 1004
PRD_W = PRD_X2 - PRD_X1
PRD_H = PRD_Y2 - PRD_Y1

DEFAULT_ACCENT = (49, 189, 224)   # циан — связь/радио

_NUM_RE = re.compile(r'\d[\d\s.,]*\d|\d')

_BRIDGE_RE   = re.compile(r'радиомост|bridge', re.IGNORECASE)
_AMP_RE      = re.compile(r'усилит|amplifier', re.IGNORECASE)
_REPEATER_RE = re.compile(r'репитер|повторитель|repeater|extender', re.IGNORECASE)
_ADAPTER_RE  = re.compile(r'адаптер|adapter', re.IGNORECASE)


def detect_type_label(raw_specs: str) -> str:
    """Заголовок инфографики по исходной строке товара — конкретный тип
    устройства связи, а не общая "Сетевое оборудование"."""
    if _BRIDGE_RE.search(raw_specs):
        return "РАДИОМОСТ"
    if _AMP_RE.search(raw_specs):
        return "УСИЛИТЕЛЬ СИГНАЛА"
    if _REPEATER_RE.search(raw_specs):
        return "ПОВТОРИТЕЛЬ WI-FI"
    if _ADAPTER_RE.search(raw_specs):
        return "АДАПТЕР"
    return "СЕТЕВОЕ ОБОРУДОВАНИЕ"

NAV = (20, 40, 80)
GRAY_UNFILLED = (205, 210, 222)

# Метрика → (ключевые слова для поиска нужной характеристики, единица
# измерения, разумный максимум шкалы для % закрашенных баров).
_METRICS = [
    ("дальность", ("дальность", "расстояние", "радиус действия", "км"), "км", 30),
    ("усиление",  ("усилен", "коэффициент усиления", "dbi", "дби", " дб"), "дБ", 20),
    ("скорость",  ("скорость", "мбит", "пропускная способност"), "Мбит/с", 1000),
]


def _parse_leading_number(text: str) -> float | None:
    m = _NUM_RE.search(text)
    if not m:
        return None
    digits = m.group().replace(" ", "").replace(",", ".")
    try:
        return float(digits)
    except ValueError:
        return None


def _find_metric(features: list[tuple[str, str]]) -> tuple[int, str, str, float, str]:
    """Ищет среди features характеристику, подходящую под одну из метрик.
    Возвращает (индекс, unit, заголовок_виджета, max_scale, сырое значение).
    Не нашли — характеристики нет вообще, бары просто не покажут число."""
    for i, (label, value) in enumerate(features):
        low = (label + " " + value).lower()
        for title, keywords, unit, max_scale in _METRICS:
            if any(kw in low for kw in keywords):
                return i, unit, title.upper(), max_scale, value
    return (0, "", "", 0, features[0][1]) if features else (-1, "", "", 0, "")


def _draw_cards(img: Image.Image, dark_bg: bool) -> None:
    """Полупрозрачные белые карточки под шапкой/виджетом/характеристиками —
    как блоки в default-стиле (infographic.py), НЕ тёмные bordered-боксы
    gaming-стиля. Зона товара — без карточки, как в default (товар просто
    на градиенте + тень)."""
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    shadow_color = (0, 0, 0, 60)
    block_fill = (255, 255, 255, 50) if dark_bg else (255, 255, 255, 220)
    for x1, y1, x2, y2 in [(HDR_X1, HDR_Y1, HDR_X2, HDR_Y2),
                            (DIAL_X1, DIAL_Y1, DIAL_X2, DIAL_Y2),
                            (CHR_X1, CHR_Y1, CHR_X2, CHR_Y2)]:
        od.rounded_rectangle([x1 + 4, y1 + 4, x2 + 4, y2 + 4], radius=RADIUS, fill=shadow_color)
        od.rounded_rectangle([x1, y1, x2, y2], radius=RADIUS, fill=block_fill)
    img.paste(Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB"), (0, 0))


def _draw_header(img: Image.Image, product: str, brand: str, type_label: str,
                  accent: tuple[int, int, int], text_col, val_col) -> None:
    draw = ImageDraw.Draw(img)
    cx = (HDR_X1 + HDR_X2) // 2
    avail_w = HDR_X2 - HDR_X1 - 32
    cat_text = (type_label or "АДАПТЕР").upper()
    if brand:
        model_str = product
        if model_str.lower().startswith(brand.lower()):
            model_str = model_str[len(brand):].strip()
        bm = f"{brand.upper()}  {model_str}" if model_str else brand.upper()
    else:
        bm = product
    f_cat = fit_font(draw, cat_text, avail_w, "title", int(HDR_H * 0.50))
    draw.text((cx, HDR_Y1 + 30), cat_text, font=f_cat, fill=_label_color(accent), anchor="mt")
    f_bm = fit_font(draw, bm, avail_w, "label", int(HDR_H * 0.27))
    draw.text((cx, HDR_Y2 - 32), bm, font=f_bm, fill=text_col, anchor="mb")


def _draw_signal_bars(img: Image.Image, title: str, unit: str, max_scale: float,
                       raw_value: str, accent: tuple[int, int, int], val_col) -> None:
    """Бары силы сигнала (как индикатор на телефоне) — 5 столбиков
    возрастающей высоты, закрашены по уровню значения."""
    draw = ImageDraw.Draw(img, "RGBA")
    cx = (DIAL_X1 + DIAL_X2) // 2
    label_h = 40
    lbl_col = _label_color(accent)

    lbl_sz = max(15, int(label_h * 0.42))
    f_lbl = fit_font(draw, (title or "ПОКАЗАТЕЛЬ"), DIAL_W - 32, "label", lbl_sz)
    draw.text((cx, DIAL_Y1 + 14), title or "ПОКАЗАТЕЛЬ", font=f_lbl, fill=lbl_col, anchor="mt")

    n_bars = 5
    bar_w = 26
    gap = 12
    max_h = 110
    min_h = 32
    total_w = n_bars * bar_w + (n_bars - 1) * gap
    base_x = cx - total_w // 2
    base_y = DIAL_Y1 + label_h + max_h + 14

    num = _parse_leading_number(raw_value)
    frac = max(0.0, min(1.0, num / max_scale)) if (num is not None and max_scale > 0) else 0.0
    filled = max(1, round(frac * n_bars)) if num is not None else 0

    for i in range(n_bars):
        bh = min_h + (max_h - min_h) * i / (n_bars - 1)
        x0 = base_x + i * (bar_w + gap)
        y1 = base_y
        y0 = base_y - bh
        fill = accent if i < filled else GRAY_UNFILLED
        draw.rounded_rectangle([x0, y0, x0 + bar_w, y1], radius=6, fill=fill)

    val_y = base_y + 18
    if num is not None:
        num_str = f"{num:g}".replace(".", ",")
        f_val = fit_font(draw, f"{num_str} {unit}", DIAL_W - 24, "title", 30)
        draw.text((cx, val_y), f"{num_str} {unit}", font=f_val, fill=val_col, anchor="mt")
    else:
        lines, sz = best_lines(draw, raw_value, DIAL_W - 32, "value", 24)
        f_val = get_font(sz, "value")
        draw.text((cx, val_y), lines[0], font=f_val, fill=val_col, anchor="mt")


def _draw_chars(img: Image.Image, features: list[tuple[str, str]],
                 accent: tuple[int, int, int], val_col) -> None:
    draw = ImageDraw.Draw(img)
    n = min(3, len(features))
    if n == 0:
        return
    lbl_col = _label_color(accent)
    pad_x = CHR_X1 + 20
    avail_w = CHR_W - 40
    pad_y = CHR_Y1 + 18
    avail_h = CHR_H - 36
    slot_h = avail_h // n
    div_col = (210, 215, 230)
    for i, (label, value) in enumerate(features[:n]):
        y0 = pad_y + i * slot_h
        if i > 0:
            draw.line([(pad_x + 6, y0), (pad_x + avail_w - 6, y0)], fill=div_col, width=1)
        cx = pad_x + avail_w // 2
        lbl_sz = max(13, int(slot_h * 0.22))
        f_lbl = fit_font(draw, label.upper(), avail_w, "label", lbl_sz)
        draw.text((cx, y0 + int(slot_h * 0.32)), label.upper(), font=f_lbl, fill=lbl_col, anchor="mm")
        val_sz = max(17, int(slot_h * 0.38))
        f_val = fit_font(draw, value, avail_w, "value", val_sz)
        draw.text((cx, y0 + int(slot_h * 0.70)), value, font=f_val, fill=val_col, anchor="mm")


async def build_signal(
    product: str,
    features: list[tuple[str, str]],
    product_img_bytes: bytes | None,
    brand: str = "",
    category: str = "",
    accent: tuple[int, int, int] = DEFAULT_ACCENT,
    raw_specs: str = "",
) -> bytes:
    """Build signal-bars infographic (радиомост/усилитель/адаптер) and return JPEG bytes."""
    type_label = detect_type_label(raw_specs or product)
    bg_bytes = _local_bg.generate_background(W, H, None)
    img = Image.open(io.BytesIO(bg_bytes)).convert("RGB")

    sample = img.crop((CHR_X1, CHR_Y1, CHR_X2, CHR_Y1 + 100)).convert("L")
    avg = sum(sample.getdata()) / (sample.width * sample.height)
    dark_bg = avg < 110
    text_col = (230, 238, 255) if dark_bg else NAV
    val_col = (235, 240, 255) if dark_bg else (15, 15, 15)

    _draw_cards(img, dark_bg)

    product_rgba = None
    if product_img_bytes:
        product_rgba = await run_gpu(remove_background, product_img_bytes, timeout=110, label="remove_background")
        if product_rgba is None:
            product_rgba = await run_gpu(remove_solid_background, product_img_bytes,
                                         label="remove_solid_background")
        if product_rgba is None:
            product_rgba = Image.open(io.BytesIO(product_img_bytes)).convert("RGBA")

    if product_rgba is not None:
        margin = 30
        zone_w = PRD_W - margin * 2
        zone_h = PRD_H - margin * 2
        zone = place_on_transparent(product_rgba, zone_w, zone_h, padding=14)
        shadow_alpha = zone.split()[3].filter(ImageFilter.GaussianBlur(18))
        shadow = Image.new("RGBA", (zone_w, zone_h), (0, 0, 0, 0))
        shadow.putalpha(shadow_alpha.point(lambda x: int(x * 0.28)))
        paste_x = PRD_X1 + (PRD_W - zone_w) // 2
        paste_y = PRD_Y1 + (PRD_H - zone_h) // 2
        img_rgba = img.convert("RGBA")
        img_rgba.paste(shadow, (paste_x + 10, paste_y + 10), shadow)
        img_rgba.paste(zone, (paste_x, paste_y), zone)
        img = img_rgba.convert("RGB")

    _draw_header(img, product, brand, type_label, accent, text_col, val_col)

    idx, unit, widget_title, max_scale, raw_value = _find_metric(features)
    rest = [f for j, f in enumerate(features) if j != idx][:3] if idx >= 0 else []
    _draw_signal_bars(img, widget_title, unit, max_scale, raw_value, accent, val_col)
    _draw_chars(img, rest, accent, val_col)

    result = paste_brand_watermark(img, corner="bottom-left", scale=0.32, bg_dark=dark_bg)
    buf = io.BytesIO()
    result.save(buf, format="JPEG", quality=92)
    return buf.getvalue()
