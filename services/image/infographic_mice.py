"""
Mice infographic template — "DPI dial".
Графитовый фон с акцентным «прожектором» за циферблатом — своя атмосфера,
отличная от infographic_gaming (там фон тёмно-синий, glow за товаром).
Headline visual — круговой gauge ("циферблат"), показывающий DPI сенсора —
главную характеристику при выборе игровой мыши.
Layout: title box (top) | DPI dial (left, upper) | remaining chars (left, lower)
| product zone (right).
"""
import io
import logging
import math
import re
from PIL import Image, ImageDraw, ImageFilter

from .fonts import fit_font, best_lines, get_font
from .background import remove_background, remove_solid_background, place_on_transparent
from utils.gpu_safety import run_gpu
from .logo import paste_brand_watermark
from .infographic_gaming import _product_brightness, _PRODUCT_DARK_THRESHOLD

log = logging.getLogger(__name__)

W, H = 768, 1024

BORDER = 4
RADIUS = 28

# Title box — between WB badge zones
HDR_X1, HDR_Y1 = 185, 26
HDR_X2, HDR_Y2 = 730, 198
HDR_H = HDR_Y2 - HDR_Y1   # 172

# DPI dial box (left, upper) — компактный, ~30% меньше радиус циферблата,
# уже по ширине, чтобы не наезжать на зону товара
DIAL_X1, DIAL_Y1 = 16, 212
DIAL_X2, DIAL_Y2 = 280, 474
DIAL_W = DIAL_X2 - DIAL_X1   # 264
DIAL_H = DIAL_Y2 - DIAL_Y1   # 262

# Remaining characteristics box (left, lower) — увеличен за счёт циферблата,
# уже по ширине (см. выше)
CHR_X1, CHR_Y1 = 16, 486
CHR_X2, CHR_Y2 = 280, 856
CHR_W = CHR_X2 - CHR_X1   # 264
CHR_H = CHR_Y2 - CHR_Y1   # 370

# Product zone (right)
PRD_X1, PRD_Y1 = 285, 212
PRD_X2, PRD_Y2 = 748, 1004
PRD_W = PRD_X2 - PRD_X1   # 463
PRD_H = PRD_Y2 - PRD_Y1   # 792

DEFAULT_ACCENT = (0, 210, 255)   # точность/лазер — голубой по умолчанию

_BG_BASE = (16, 17, 23)   # графитовый — холоднее и нейтральнее navy-фона gaming

_NUM_RE = re.compile(r'\d[\d\s]*\d|\d')

_GAMING_RE = re.compile(r'gaming|игров', re.IGNORECASE)
_WIRELESS_RE = re.compile(r'wireless|беспровод', re.IGNORECASE)


def detect_type_label(raw_specs: str) -> str:
    """Заголовок инфографики по исходной строке товара: игровая / беспроводная / обычная мышь."""
    if _GAMING_RE.search(raw_specs):
        return "ИГРОВАЯ МЫШЬ"
    if _WIRELESS_RE.search(raw_specs):
        return "БЕСПРОВОДНАЯ\nМЫШЬ"
    return "МЫШЬ"


def _make_mice_bg(w: int, h: int, accent: tuple[int, int, int]) -> Image.Image:
    """Графитовый фон с акцентным «прожектором» за циферблатом (левый верх)
    и приглушённым вторичным glow за товаром — отличается от gaming-фона,
    где база тёмно-синяя и glow всегда сосредоточен за товаром."""
    r, g, b = accent
    base = Image.new("RGBA", (w, h), (*_BG_BASE, 255))
    glow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)

    # Акцентный glow — за циферблатом
    cx1, cy1 = int(w * 0.20), int(h * 0.38)
    for i in range(8, 0, -1):
        alpha = int(75 * (i / 8))
        rx = int(w * 0.42 * (i / 8))
        ry = int(h * 0.36 * (i / 8))
        gd.ellipse((cx1 - rx, cy1 - ry, cx1 + rx, cy1 + ry), fill=(r, g, b, alpha))

    # Приглушённый вторичный glow — за товаром
    cx2, cy2 = int(w * 0.66), int(h * 0.55)
    for i in range(6, 0, -1):
        alpha = int(28 * (i / 6))
        rx = int(w * 0.40 * (i / 6))
        ry = int(h * 0.34 * (i / 6))
        gd.ellipse((cx2 - rx, cy2 - ry, cx2 + rx, cy2 + ry), fill=(r, g, b, alpha))

    glow = glow.filter(ImageFilter.GaussianBlur(60))
    return Image.alpha_composite(base, glow).convert("RGB")


def _parse_leading_number(text: str) -> int | None:
    """Достаёт первое число из строки вида «26000 DPI» / «Сенсор 26 000 DPI»."""
    m = _NUM_RE.search(text)
    if not m:
        return None
    digits = m.group().replace(" ", "")
    try:
        return int(digits)
    except ValueError:
        return None


def _find_dial_index(features: list[tuple[str, str]]) -> int:
    """Индекс характеристики РАЗРЕШЕНИЯ СЕНСОРА (DPI) — для циферблата.

    16.09.2026: раньше матчилось по "ДАТЧИК" в лейбле (ловило "Тип датчика
    мыши" = "лазерный", не имеющий отношения к DPI) и, если не находилось,
    тихо падало на 0-й элемент СПИСКА — какой бы характеристикой он ни был
    (вес, частота опроса и т.п.). _draw_dpi_dial() затем безусловно рисовала
    "DPI" под этим числом — живой баг: "Частота опроса"=125 → "125 DPI" на
    <артикул>, "Вес товара"=140 → "140 DPI" на <артикул>. Теперь матчим
    только по явному DPI/разрешению сенсора, без слепого фолбэка на 0."""
    for i, (label, value) in enumerate(features):
        lab, val = label.upper(), value.upper()
        if "DPI" in lab or "DPI" in val or "РАЗРЕШЕНИЕ" in lab:
            return i
    return -1


def _draw_boxes(img: Image.Image, accent: tuple[int, int, int],
                product_dark: bool = False) -> None:
    """Draw boxes in correct z-order: product zone → dial → chars → title."""
    r, g, b = accent
    draw = ImageDraw.Draw(img)

    box_fill  = (14, 15, 20)
    prd_fill  = (56, 58, 68) if product_dark else box_fill
    tint_fill = (max(9, r // 12 + 13), max(9, g // 12 + 13), max(9, b // 12 + 15))

    # 1. Product zone — bottom layer
    draw.rounded_rectangle(
        [PRD_X1, PRD_Y1, PRD_X2, PRD_Y2],
        radius=RADIUS, fill=prd_fill, outline=(r, g, b), width=BORDER,
    )

    # 2. DPI dial box
    draw.rounded_rectangle(
        [DIAL_X1, DIAL_Y1, DIAL_X2, DIAL_Y2],
        radius=RADIUS, fill=tint_fill, outline=(r, g, b), width=BORDER,
    )

    # 3. Remaining characteristics box
    draw.rounded_rectangle(
        [CHR_X1, CHR_Y1, CHR_X2, CHR_Y2],
        radius=RADIUS, fill=tint_fill, outline=(r, g, b), width=BORDER,
    )

    # 4. Title box — topmost
    draw.rounded_rectangle(
        [HDR_X1, HDR_Y1, HDR_X2, HDR_Y2],
        radius=RADIUS, fill=box_fill, outline=(r, g, b), width=BORDER,
    )


def _draw_header(img: Image.Image, product: str, brand: str,
                 type_label: str, accent: tuple[int, int, int]) -> None:
    """Тип мыши («ИГРОВАЯ МЫШЬ» / «МЫШЬ» / «БЕСПРОВОДНАЯ\\nМЫШЬ», крупно) + бренд/модель ниже (светлым)."""
    r, g, b = accent
    draw = ImageDraw.Draw(img)

    cx      = (HDR_X1 + HDR_X2) // 2
    avail_w = HDR_X2 - HDR_X1 - 32
    cat_text = (type_label or "МЫШЬ").upper()

    if brand:
        model_str = product
        if model_str.lower().startswith(brand.lower()):
            model_str = model_str[len(brand):].strip()
        bm = f"{brand.upper()}  {model_str}" if model_str else brand.upper()
    else:
        bm = product

    if "\n" in cat_text:
        # Двухстрочный заголовок (напр. «БЕСПРОВОДНАЯ / МЫШЬ») — строка
        # бренд/модели ужимается, под title остаётся больше места.
        line1, line2 = cat_text.split("\n")
        f_l1 = fit_font(draw, line1, avail_w, "title", int(HDR_H * 0.27))
        f_l2 = fit_font(draw, line2, avail_w, "title", int(HDR_H * 0.368))
        lh1 = sum(f_l1.getmetrics())
        draw.text((cx, HDR_Y1 + 20), line1, font=f_l1, fill=(r, g, b), anchor="mt")
        draw.text((cx, HDR_Y1 + 20 + lh1 - 6), line2, font=f_l2, fill=(r, g, b), anchor="mt")

        f_bm = fit_font(draw, bm, avail_w, "label", int(HDR_H * 0.18))
        draw.text((cx, HDR_Y2 - 18), bm,
                  font=f_bm, fill=(210, 215, 230), anchor="mb")
    else:
        f_cat = fit_font(draw, cat_text, avail_w, "title", int(HDR_H * 0.50))
        draw.text((cx, HDR_Y1 + 30), cat_text,
                  font=f_cat, fill=(r, g, b), anchor="mt")
        f_bm = fit_font(draw, bm, avail_w, "label", int(HDR_H * 0.27))
        draw.text((cx, HDR_Y2 - 32), bm,
                  font=f_bm, fill=(210, 215, 230), anchor="mb")


def _draw_dpi_dial(img: Image.Image, label: str, value: str,
                   accent: tuple[int, int, int], is_dpi: bool = True) -> None:
    """Круговой gauge: дуга 270° (засечки + трек + заливка) + стрелка + крупное
    число DPI в центре. Если числовое DPI не нашлось — текст значения по центру."""
    r, g, b = accent
    draw = ImageDraw.Draw(img, "RGBA")

    cx = (DIAL_X1 + DIAL_X2) // 2
    label_h = 46
    radius = min(DIAL_W - 40, DIAL_H - label_h - 20) // 2
    cy = DIAL_Y1 + label_h + radius

    start_a, end_a = 135, 405   # 270° sweep, разрыв снизу

    # Безель — тёмный круг под циферблатом с тонким акцентным кольцом по краю,
    # засечки шкалы пересекают его край (вид «приборной панели»)
    bezel_r = radius + 12
    draw.ellipse(
        [cx - bezel_r, cy - bezel_r, cx + bezel_r, cy + bezel_r],
        fill=(10, 11, 16), outline=(r, g, b, 110), width=2,
    )

    # Заголовок над циферблатом
    lbl_sz = max(15, int(label_h * 0.42))
    f_lbl = fit_font(draw, label.upper(), DIAL_W - 32, "label", lbl_sz)
    draw.text((cx, DIAL_Y1 + 14), label.upper(),
              font=f_lbl, fill=(r, g, b), anchor="mt")

    bbox = (cx - radius, cy - radius, cx + radius, cy + radius)

    # Засечки шкалы
    for i in range(9):
        a = math.radians(start_a + (end_a - start_a) * i / 8)
        x1 = cx + (radius + 8)  * math.cos(a)
        y1 = cy + (radius + 8)  * math.sin(a)
        x2 = cx + (radius + 18) * math.cos(a)
        y2 = cy + (radius + 18) * math.sin(a)
        draw.line([(x1, y1), (x2, y2)], fill=(95, 98, 122), width=2)

    # Фоновая дуга-трек
    draw.arc(bbox, start_a, end_a, fill=(52, 54, 76), width=22)

    dpi = _parse_leading_number(value) if is_dpi else None
    if dpi:
        max_dpi = 32000
        frac = max(0.04, min(1.0, dpi / max_dpi))
        fg_end = start_a + (end_a - start_a) * frac
        draw.arc(bbox, start_a, fg_end, fill=(r, g, b), width=22)

        # Маркер-указатель — точка на дуге (не линия из центра, чтобы не
        # перечёркивать число DPI, которое может оказаться по любую сторону)
        na = math.radians(fg_end)
        mx = cx + radius * math.cos(na)
        my = cy + radius * math.sin(na)
        draw.ellipse([mx - 10, my - 10, mx + 10, my + 10],
                      fill=(245, 245, 255), outline=(r, g, b), width=3)

        # Число по центру
        dpi_str = f"{dpi:,}".replace(",", " ")
        f_val = fit_font(draw, dpi_str, int(radius * 1.3), "title", int(radius * 0.46))
        draw.text((cx, cy - int(radius * 0.06)), dpi_str,
                  font=f_val, fill=(245, 245, 255), anchor="mm")
        f_unit = get_font(max(14, int(radius * 0.20)), "label")
        draw.text((cx, cy + int(radius * 0.42)), "DPI",
                  font=f_unit, fill=(r, g, b), anchor="mm")
    else:
        # Нет числового DPI — только трек + значение текстом по центру
        lines, sz = best_lines(draw, value, int(radius * 1.5), "value", int(radius * 0.30))
        f_val = get_font(sz, "value")
        ly = cy - (len(lines) - 1) * sz * 0.6
        for line in lines:
            draw.text((cx, ly), line, font=f_val, fill=(245, 245, 255), anchor="mm")
            ly += sz * 1.2


def _draw_chars(img: Image.Image, features: list[tuple[str, str]],
                accent: tuple[int, int, int]) -> None:
    """До 3 характеристик в нижней левой коробке: метка (accent) + значение (белое)."""
    r, g, b = accent
    draw = ImageDraw.Draw(img)

    n = min(3, len(features))
    if n == 0:
        return

    pad_x   = CHR_X1 + 20
    avail_w = CHR_W - 40
    pad_y   = CHR_Y1 + 18
    avail_h = CHR_H - 36
    slot_h  = avail_h // n

    div_r = min(255, (r * 2 + 255 * 3) // 5)
    div_g = min(255, (g * 2 + 255 * 3) // 5)
    div_b = min(255, (b * 2 + 255 * 3) // 5)

    for i, (label, value) in enumerate(features[:n]):
        y0 = pad_y + i * slot_h

        if i > 0:
            draw.line(
                [(pad_x + 6, y0), (pad_x + avail_w - 6, y0)],
                fill=(div_r, div_g, div_b), width=1,
            )

        cx = pad_x + avail_w // 2

        lbl_sz = max(13, int(slot_h * 0.22))
        f_lbl  = fit_font(draw, label.upper(), avail_w, "label", lbl_sz)
        draw.text(
            (cx, y0 + int(slot_h * 0.32)),
            label.upper(), font=f_lbl, fill=(r, g, b), anchor="mm",
        )

        val_sz = max(17, int(slot_h * 0.38))
        f_val  = fit_font(draw, value, avail_w, "value", val_sz)
        draw.text(
            (cx, y0 + int(slot_h * 0.70)),
            value, font=f_val, fill=(245, 245, 255), anchor="mm",
        )


async def build_mice(
    product: str,
    features: list[tuple[str, str]],
    product_img_bytes: bytes | None,
    brand: str = "",
    category: str = "",
    accent: tuple[int, int, int] = DEFAULT_ACCENT,
    raw_specs: str = "",
) -> bytes:
    """Build mice infographic (DPI dial) and return JPEG bytes."""

    img = _make_mice_bg(W, H, accent)

    product_rgba = None
    product_dark = False
    if product_img_bytes:
        product_rgba = await run_gpu(remove_background, product_img_bytes, timeout=110, label="remove_background")
        if product_rgba is None:
            log.warning("rembg failed/timeout — fallback: white bg removal")
            product_rgba = await run_gpu(remove_solid_background, product_img_bytes,
                                         label="remove_solid_background")
        if product_rgba is None:
            log.warning("white bg removal тоже не удалась — кладём фото как есть")
            product_rgba = Image.open(io.BytesIO(product_img_bytes)).convert("RGBA")
        brightness = _product_brightness(product_rgba)
        product_dark = brightness < _PRODUCT_DARK_THRESHOLD

    _draw_boxes(img, accent, product_dark=product_dark)

    if product_rgba is not None:
        # Небольшой отступ вокруг товара — мышь не должна упираться в рамки зоны.
        # Margin держим маленьким: для фото, где товар и так занимает мало места
        # в кадре (или в кадре лишние элементы вроде зарядного хаба),
        # no_overflow_scale в place_on_transparent и так даёт меньший масштаб —
        # большой margin делал такие фото ещё мельче, а чистые крупные фото — слишком мелкими.
        margin = 30
        zone_w = PRD_W - 16 - margin * 2
        zone_h = PRD_H - 16 - margin * 2
        zone   = place_on_transparent(product_rgba, zone_w, zone_h, padding=14)

        shadow_alpha = zone.split()[3].filter(ImageFilter.GaussianBlur(18))
        shadow = Image.new("RGBA", (zone_w, zone_h), (0, 0, 0, 0))
        shadow.putalpha(shadow_alpha.point(lambda x: int(x * 0.28)))

        paste_x = PRD_X1 + (PRD_W - zone_w) // 2
        paste_y = PRD_Y1 + (PRD_H - zone_h) // 2

        img_rgba = img.convert("RGBA")
        img_rgba.paste(shadow, (paste_x + 10, paste_y + 10), shadow)
        img_rgba.paste(zone,   (paste_x, paste_y),  zone)
        img = img_rgba.convert("RGB")

    _draw_header(img, product, brand, detect_type_label(raw_specs), accent)

    idx = _find_dial_index(features)
    is_dpi = idx >= 0
    if idx < 0:
        idx = 0 if features else -1
    if idx >= 0:
        dial_label, dial_value = features[idx]
        rest = [f for j, f in enumerate(features) if j != idx][:3]
    else:
        dial_label, dial_value = "ДАТЧИК", "—"
        rest = []

    _draw_dpi_dial(img, dial_label, dial_value, accent, is_dpi=is_dpi)
    _draw_chars(img, rest, accent)

    result = paste_brand_watermark(img, corner="bottom-left", scale=0.32, bg_dark=True)

    buf = io.BytesIO()
    result.save(buf, format="JPEG", quality=92)
    return buf.getvalue()
