"""
Shared layout for compact "widget" infographics (ОЗУ, Видеокарта, Смарт-часы):
header box (top) | feature widget (left, upper) | характеристики (left, lower)
| зона товара (right). Аналог infographic_mice.py, но с отдельным виджетом
вместо DPI-циферблата — конкретный вид виджета рисует каждый модуль сам.
"""
import io
import logging
from PIL import Image, ImageDraw, ImageFilter

from .fonts import fit_font, best_lines, get_font
from .background import (
    remove_background, remove_background_monitor, remove_solid_background,
    place_on_transparent, MONITOR_LIKE_CATEGORIES,
)
from utils.gpu_safety import run_gpu
from .logo import paste_brand_watermark
from .infographic_gaming import _product_brightness, _PRODUCT_DARK_THRESHOLD

log = logging.getLogger(__name__)

W, H = 768, 1024

BORDER = 4
RADIUS = 28
BG_BASE = (16, 17, 23)
BOX_FILL = (14, 15, 20)

HDR_X1, HDR_Y1, HDR_X2, HDR_Y2 = 185, 26, 730, 180
HDR_H = HDR_Y2 - HDR_Y1

WIDGET_X1, WIDGET_Y1, WIDGET_X2, WIDGET_Y2 = 16, 194, 280, 440
WIDGET_W = WIDGET_X2 - WIDGET_X1
WIDGET_H = WIDGET_Y2 - WIDGET_Y1

# y>860 — слепая зона WB-плашек (см. infographic.py BODY_BOTTOM), оставляем пустой
CHR_X1, CHR_Y1, CHR_X2, CHR_Y2 = 16, 452, 280, 860
CHR_W = CHR_X2 - CHR_X1
CHR_H = CHR_Y2 - CHR_Y1

PRD_X1, PRD_Y1, PRD_X2, PRD_Y2 = 285, 194, 748, 860
PRD_W = PRD_X2 - PRD_X1
PRD_H = PRD_Y2 - PRD_Y1


def make_widget_bg(accent: tuple[int, int, int], glow_pos=(0.18, 0.32)) -> Image.Image:
    """Графитовый фон с акцентным glow за виджетом + слабым вторичным за товаром."""
    r, g, b = accent
    base = Image.new("RGBA", (W, H), (*BG_BASE, 255))
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)

    cx, cy = int(W * glow_pos[0]), int(H * glow_pos[1])
    for i in range(8, 0, -1):
        alpha = int(75 * (i / 8))
        rx = int(W * 0.42 * (i / 8))
        ry = int(H * 0.30 * (i / 8))
        gd.ellipse((cx - rx, cy - ry, cx + rx, cy + ry), fill=(r, g, b, alpha))

    cx2, cy2 = int(W * 0.66), int(H * 0.55)
    for i in range(6, 0, -1):
        alpha = int(26 * (i / 6))
        rx = int(W * 0.40 * (i / 6))
        ry = int(H * 0.34 * (i / 6))
        gd.ellipse((cx2 - rx, cy2 - ry, cx2 + rx, cy2 + ry), fill=(r, g, b, alpha))

    glow = glow.filter(ImageFilter.GaussianBlur(60))
    return Image.alpha_composite(base, glow).convert("RGB")


def tint_fill(accent: tuple[int, int, int]) -> tuple[int, int, int]:
    r, g, b = accent
    return (max(9, r // 12 + 13), max(9, g // 12 + 13), max(9, b // 12 + 15))


def draw_widget_boxes(img: Image.Image, accent: tuple[int, int, int],
                       product_dark: bool = False) -> None:
    """Z-order: зона товара → виджет → характеристики → заголовок."""
    r, g, b = accent
    draw = ImageDraw.Draw(img)

    prd_fill = (56, 58, 68) if product_dark else BOX_FILL

    draw.rounded_rectangle([PRD_X1, PRD_Y1, PRD_X2, PRD_Y2], radius=RADIUS,
                            fill=prd_fill, outline=(r, g, b), width=BORDER)
    draw.rounded_rectangle([WIDGET_X1, WIDGET_Y1, WIDGET_X2, WIDGET_Y2], radius=RADIUS,
                            fill=tint_fill(accent), outline=(r, g, b), width=BORDER)
    draw.rounded_rectangle([CHR_X1, CHR_Y1, CHR_X2, CHR_Y2], radius=RADIUS,
                            fill=tint_fill(accent), outline=(r, g, b), width=BORDER)
    draw.rounded_rectangle([HDR_X1, HDR_Y1, HDR_X2, HDR_Y2], radius=RADIUS,
                            fill=BOX_FILL, outline=(r, g, b), width=BORDER)


def _strip_title_words(title: str, text: str) -> str:
    """Убирает из начала text слова, уже совпадающие с началом title (без учёта
    регистра) — иначе под крупным "ОПЕРАТИВНАЯ ПАМЯТЬ" повторяется
    "Оперативная память для ноутбука..." и название выглядит дублирующимся."""
    title_words = title.lower().split()
    text_words = text.split()
    i = 0
    while i < len(title_words) and i < len(text_words) and text_words[i].lower() == title_words[i]:
        i += 1
    if i == 0:
        return text
    rest = " ".join(text_words[i:])
    return rest or text


def draw_widget_header(img: Image.Image, title: str, product: str, brand: str,
                        accent: tuple[int, int, int]) -> None:
    """Категория (крупно, accent) + бренд/модель (ниже, светлым) — как у мышей."""
    r, g, b = accent
    draw = ImageDraw.Draw(img)
    cx = (HDR_X1 + HDR_X2) // 2
    avail_w = HDR_X2 - HDR_X1 - 32

    if brand:
        model_str = product
        if model_str.lower().startswith(brand.lower()):
            model_str = model_str[len(brand):].strip()
        model_str = _strip_title_words(title, model_str)
        bm = f"{brand.upper()}  {model_str}" if model_str else brand.upper()
    else:
        bm = _strip_title_words(title, product)

    f_title = fit_font(draw, title, avail_w, "title", int(HDR_H * 0.46))
    draw.text((cx, HDR_Y1 + 18), title, font=f_title, fill=(r, g, b), anchor="mt")
    f_bm = fit_font(draw, bm, avail_w, "label", int(HDR_H * 0.22))
    draw.text((cx, HDR_Y2 - 16), bm, font=f_bm, fill=(210, 215, 230), anchor="mb")


def draw_chars_3(img: Image.Image, items: list[tuple[str, str]],
                 accent: tuple[int, int, int]) -> None:
    """До 3 характеристик в CHR-боксе, значения центрированы в своих слотах."""
    draw = ImageDraw.Draw(img)
    n = len(items)
    if n == 0:
        return
    pad_x = CHR_X1 + 20
    avail_w = CHR_W - 40
    pad_y = CHR_Y1 + 18
    avail_h = CHR_H - 36
    slot_h = avail_h // n
    label_h = 30
    label_gap = 10
    for i, (label, value) in enumerate(items):
        y0 = pad_y + i * slot_h
        slot_bottom = pad_y + (i + 1) * slot_h - (14 if i < n - 1 else 0)

        f_lbl = fit_font(draw, label.upper(), avail_w, "label", 19)
        lines, sz = best_lines(draw, value, avail_w, "value", 34)
        f_val = get_font(sz, "value")
        line_h = sz * 1.15

        content_h = label_h + label_gap + len(lines) * line_h
        group_top = y0 + (slot_bottom - y0 - content_h) / 2
        draw.text((pad_x, group_top), label.upper(), font=f_lbl, fill=accent, anchor="lt")

        start_y = group_top + label_h + label_gap + line_h / 2
        for j, line in enumerate(lines):
            draw.text((pad_x, start_y + j * line_h), line, font=f_val, fill=(245, 245, 255), anchor="lm")
        if i < n - 1:
            div_y = pad_y + (i + 1) * slot_h - 14
            draw.line([(pad_x, div_y), (CHR_X2 - 20, div_y)], fill=(50, 52, 70), width=1)


def pick_chars(features: list[tuple[str, str]], priority_keywords: list[str],
               exclude_idx: int | None = None, n: int = 3) -> list[tuple[str, str]]:
    """Выбирает до n характеристик: сначала по приоритетным ключевым словам
    (поиск подстроки в label, без учёта регистра), затем добивает остальными."""
    used: set[int] = set()
    if exclude_idx is not None and exclude_idx >= 0:
        used.add(exclude_idx)

    picked: list[tuple[str, str]] = []
    for kw in priority_keywords:
        for i, (label, value) in enumerate(features):
            if i in used:
                continue
            if kw in label.upper():
                picked.append((label, value))
                used.add(i)
                break

    if len(picked) < n:
        for i, feat in enumerate(features):
            if i not in used and len(picked) < n:
                picked.append(feat)
                used.add(i)

    return picked[:n]


async def prepare_product(product_img_bytes: bytes | None, category: str = "") -> tuple[Image.Image | None, bool]:
    """rembg (с фоллбэком на удаление белого фона) + оценка яркости товара.
    Для мониторов/моноблоков — monitor-safe вырезание (см. MONITOR_LIKE_CATEGORIES
    в background.py), убирает белую кайму по кромке плоского экрана."""
    if not product_img_bytes:
        return None, False
    if category in MONITOR_LIKE_CATEGORIES:
        product_rgba = await run_gpu(remove_background_monitor, product_img_bytes,
                                     timeout=90, label="remove_background_monitor")
    else:
        product_rgba = await run_gpu(remove_background, product_img_bytes, timeout=110, label="remove_background")
    if product_rgba is None:
        log.warning("rembg failed/timeout — fallback: solid bg removal (auto-detect color)")
        product_rgba = await run_gpu(remove_solid_background, product_img_bytes,
                                     label="remove_solid_background")
    if product_rgba is None:
        log.warning("solid bg removal тоже не удалась — кладём фото как есть")
        product_rgba = Image.open(io.BytesIO(product_img_bytes)).convert("RGBA")
    brightness = _product_brightness(product_rgba)
    return product_rgba, brightness < _PRODUCT_DARK_THRESHOLD


def paste_product(img: Image.Image, product_rgba: Image.Image,
                   accent: tuple[int, int, int], scale: float = 1.0) -> Image.Image:
    """Вписывает товар в правую (PRD) зону с мягкой тенью.
    scale < 1.0 — дополнительно уменьшает товар внутри зоны (больше "воздуха")."""
    margin = 30
    zone_w = PRD_W - 16 - margin * 2
    zone_h = PRD_H - 16 - margin * 2
    zone = place_on_transparent(product_rgba, zone_w, zone_h, padding=14, extra_scale=scale)

    shadow_alpha = zone.split()[3].filter(ImageFilter.GaussianBlur(18))
    shadow = Image.new("RGBA", (zone_w, zone_h), (0, 0, 0, 0))
    shadow.putalpha(shadow_alpha.point(lambda x: int(x * 0.28)))

    paste_x = PRD_X1 + (PRD_W - zone_w) // 2
    paste_y = PRD_Y1 + (PRD_H - zone_h) // 2

    img_rgba = img.convert("RGBA")
    img_rgba.paste(shadow, (paste_x + 10, paste_y + 10), shadow)
    img_rgba.paste(zone, (paste_x, paste_y), zone)
    return img_rgba.convert("RGB")


def finalize(img: Image.Image) -> bytes:
    """Водяной знак + кодирование в JPEG."""
    result = paste_brand_watermark(img, corner="bottom-left", scale=0.32, bg_dark=True)
    buf = io.BytesIO()
    result.save(buf, format="JPEG", quality=92)
    return buf.getvalue()
