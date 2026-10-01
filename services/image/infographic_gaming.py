"""
Gaming / energetic infographic template — V2.
White background, accent-colored outlined boxes, product image on top.
Layout: title box (top, between WB badges) | chars box (left) | product zone (right).
Chars box overlaps product zone; product image is the topmost element.
"""
import colorsys
import io
import logging
import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from .fonts import fit_font, best_lines, get_font
from .background import remove_background, remove_background_monitor, remove_solid_background, place_on_transparent
from utils.gpu_safety import run_gpu
from .logo import paste_brand_watermark

log = logging.getLogger(__name__)

W, H = 768, 1024

# ── Layout ────────────────────────────────────────────────────────────────────
BORDER = 4    # outline border width (px)
RADIUS = 28   # rounded corner radius

# Title box — between WB badge zones (badges occupy top corners ≈185×185 px)
HDR_X1, HDR_Y1 = 185, 26
HDR_X2, HDR_Y2 = 730, 198
HDR_H = HDR_Y2 - HDR_Y1   # 172

# Characteristics box (left, overlaps product zone on the right)
CHR_X1, CHR_Y1 = 16,  212
CHR_X2, CHR_Y2 = 336, 856
CHR_W = CHR_X2 - CHR_X1   # 320
CHR_H = CHR_Y2 - CHR_Y1   # 644

# Product zone box (right, larger; product image sits on top of it)
PRD_X1, PRD_Y1 = 285, 212
PRD_X2, PRD_Y2 = 748, 1004
PRD_W = PRD_X2 - PRD_X1   # 463
PRD_H = PRD_Y2 - PRD_Y1   # 792

DEFAULT_ACCENT = (200, 20, 20)
_DARK_BASE = (28, 28, 46)   # тёмно-синий, не pitch-black — даёт контраст для тёмных товаров

_PRODUCT_DARK_THRESHOLD = 100  # средняя яркость пикселей товара < 100 → считаем тёмным


def _derive_dark_base(accent: tuple[int, int, int]) -> tuple[int, int, int]:
    """Тёмная подложка, тонированная в тон акцента — разнообразит палитру
    (вместо одного фикс. тёмно-синего фона для всех цветов акцента)."""
    r, g, b = accent
    h, _, _ = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
    r2, g2, b2 = colorsys.hsv_to_rgb(h, 0.18, 0.10)
    return (int(r2 * 255), int(g2 * 255), int(b2 * 255))


def _product_brightness(img_rgba: Image.Image) -> float:
    """Средняя яркость непрозрачных пикселей товара после rembg."""
    try:
        arr = np.array(img_rgba.convert("RGBA"))
        alpha = arr[:, :, 3]
        mask = alpha > 30
        if mask.sum() < 200:
            return 128.0
        px = arr[:, :, :3][mask].astype(float)
        return float(0.299 * px[:, 0].mean() + 0.587 * px[:, 1].mean() + 0.114 * px[:, 2].mean())
    except Exception:
        return 128.0


def make_gaming_bg(w: int, h: int, accent: tuple[int, int, int]) -> Image.Image:
    """Dark base + accent glow atmosphere — работает для любых размеров."""
    r, g, b = accent
    base = Image.new("RGBA", (w, h), (*_derive_dark_base(accent), 255))
    glow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    gd   = ImageDraw.Draw(glow)

    cx, cy = int(w * 0.65), int(h * 0.52)
    for i in range(8, 0, -1):
        alpha = int(110 * (i / 8))
        rx = int(w * 0.70 * (i / 8))
        ry = int(h * 0.60 * (i / 8))
        gd.ellipse((cx - rx, cy - ry, cx + rx, cy + ry), fill=(r, g, b, alpha))

    glow = glow.filter(ImageFilter.GaussianBlur(55))
    return Image.alpha_composite(base, glow).convert("RGB")


def _make_bg(accent: tuple[int, int, int]) -> Image.Image:
    return make_gaming_bg(W, H, accent)


def _draw_boxes(img: Image.Image, accent: tuple[int, int, int],
                product_dark: bool = False) -> None:
    """Draw three outlined boxes in correct z-order: product zone → chars → title."""
    r, g, b = accent
    draw = ImageDraw.Draw(img)

    box_fill  = (18, 18, 32)
    # Тёмный товар → зона продукта чуть светлее: тёмно-серо-синий вместо почти-чёрного
    prd_fill  = (60, 62, 84) if product_dark else box_fill
    tint_fill = (max(8, r // 10 + 12),
                 max(8, g // 10 + 12),
                 max(8, b // 10 + 14))

    # 1. Product zone — bottom layer
    draw.rounded_rectangle(
        [PRD_X1, PRD_Y1, PRD_X2, PRD_Y2],
        radius=RADIUS, fill=prd_fill, outline=(r, g, b), width=BORDER,
    )

    # 2. Chars box — covers product zone border in overlap area (285–336 px)
    draw.rounded_rectangle(
        [CHR_X1, CHR_Y1, CHR_X2, CHR_Y2],
        radius=RADIUS, fill=tint_fill, outline=(r, g, b), width=BORDER,
    )

    # 3. Title box — topmost, above both
    draw.rounded_rectangle(
        [HDR_X1, HDR_Y1, HDR_X2, HDR_Y2],
        radius=RADIUS, fill=box_fill, outline=(r, g, b), width=BORDER,
    )


# Внутренние названия категорий — собирательные/множественные (как в меню
# бота), а в шапке инфографики читается естественнее единственное число.
_CATEGORY_SINGULAR: dict[str, str] = {
    "SSD накопители": "SSD накопитель",
    "Блоки питания": "Блок питания",
    "Видеокарты": "Видеокарта",
    "Внешние жёсткие диски": "Внешний жёсткий диск",
    "Графические планшеты": "Графический планшет",
    "Зарядные устройства и блоки питания": "Зарядное устройство",
    "Клавиатуры": "Клавиатура",
    "Корпуса для ПК": "Корпус",
    "Кронштейны для мониторов": "Кронштейн",
    "Материнские платы": "Материнская плата",
    "Модемы": "Модем",
    "Мониторы": "Монитор",
    "Моноблоки": "Моноблок",
    "Мыши": "Мышь",
    "Ноутбуки": "Ноутбук",
    "Планшеты": "Планшет",
    "Принтеры": "Принтер",
    "Картриджи для принтеров": "Картридж",
    "Коврики для мыши": "Коврик для мыши",
    "Процессоры": "Процессор",
    "Смартфоны": "Смартфон",
}


def _draw_header(img: Image.Image, product: str, brand: str,
                 category: str, accent: tuple[int, int, int]) -> None:
    """Category large (accent) + brand / model below (dark)."""
    r, g, b = accent
    draw = ImageDraw.Draw(img)

    cx      = (HDR_X1 + HDR_X2) // 2
    avail_w = HDR_X2 - HDR_X1 - 32

    category = _CATEGORY_SINGULAR.get(category, category)
    cat_text = (category or (product.split()[0] if product else "ТОВАР")).upper()

    if brand:
        f_cat = fit_font(draw, cat_text, avail_w, "title", int(HDR_H * 0.50))
        draw.text((cx, HDR_Y1 + 30), cat_text,
                  font=f_cat, fill=(r, g, b), anchor="mt")

        model_str = product
        if model_str.lower().startswith(brand.lower()):
            model_str = model_str[len(brand):].strip()
        bm = f"{brand.upper()}  {model_str}" if model_str else brand.upper()
        f_bm = fit_font(draw, bm, avail_w, "label", int(HDR_H * 0.27))
        draw.text((cx, HDR_Y2 - 32), bm,
                  font=f_bm, fill=(210, 215, 230), anchor="mb")
    else:
        # No brand: category large (accent) + product name smaller below
        f_cat = fit_font(draw, cat_text, avail_w, "title", int(HDR_H * 0.50))
        draw.text((cx, HDR_Y1 + 30), cat_text,
                  font=f_cat, fill=(r, g, b), anchor="mt")
        f_prod = fit_font(draw, product, avail_w, "label", int(HDR_H * 0.27))
        draw.text((cx, HDR_Y2 - 32), product,
                  font=f_prod, fill=(210, 215, 230), anchor="mb")


def _draw_chars(img: Image.Image, features: list[tuple[str, str]],
                accent: tuple[int, int, int]) -> None:
    """4 features inside the chars box: label (accent) + value (dark)."""
    r, g, b = accent
    draw = ImageDraw.Draw(img)

    n = min(3, len(features))
    if n == 0:
        return

    # Inner text area inside the chars box
    pad_x   = CHR_X1 + 20
    avail_w = CHR_W - 40
    pad_y   = CHR_Y1 + 24
    avail_h = CHR_H - 48
    slot_h  = avail_h // n

    # Divider color: light accent blend
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

        # Label — small caps, accent color
        lbl_sz = max(13, int(slot_h * 0.22))
        f_lbl  = fit_font(draw, label.upper(), avail_w, "label", lbl_sz)
        draw.text(
            (cx, y0 + int(slot_h * 0.32)),
            label.upper(), font=f_lbl, fill=(r, g, b), anchor="mm",
        )

        # Value — larger, white
        val_sz = max(17, int(slot_h * 0.38))
        f_val  = fit_font(draw, value, avail_w, "value", val_sz)
        draw.text(
            (cx, y0 + int(slot_h * 0.70)),
            value, font=f_val, fill=(245, 245, 255), anchor="mm",
        )


async def build_gaming(
    product: str,
    features: list[tuple[str, str]],
    product_img_bytes: bytes | None,
    brand: str = "",
    category: str = "",
    accent: tuple[int, int, int] = DEFAULT_ACCENT,
) -> bytes:
    """Build gaming-style infographic and return JPEG bytes."""

    # 1. Dark gaming background with accent glow
    img = _make_bg(accent)

    # 2. Detect product brightness for adaptive box fill
    product_rgba = None
    product_dark = False
    if product_img_bytes:
        # Мониторы: безопасное вырезание — заливка замкнутых дыр в экране
        # + детект «дырявого экрана»/«белого квадрата» с фолбэком на
        # flood-fill (см. background.py)
        if category == "Мониторы":
            product_rgba = await run_gpu(remove_background_monitor, product_img_bytes,
                                         timeout=90, label="remove_background_monitor")
        else:
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
        log.info(f"Product brightness={brightness:.0f} → {'DARK (light bg)' if product_dark else 'LIGHT (dark bg)'}")

    # 3. Outlined boxes — зона продукта чуть светлее для тёмных товаров
    _draw_boxes(img, accent, product_dark=product_dark)

    # 4. Product image — on top of all boxes
    if product_rgba is not None:
        zone_w = PRD_W - 16
        zone_h = PRD_H - 16
        zone   = place_on_transparent(product_rgba, zone_w, zone_h, padding=14)
        img_rgba = img.convert("RGBA")

        if product_dark:
            # Тёмный товар на тёмном фоне — даже подсвеченный бокс (prd_fill)
            # не спасает тонкие детали (дуга гарнитуры, провод): чёрная тень
            # тут бесполезна (фон сам тёмный), нужно светлое свечение по
            # силуэту, расширенное на пару px, чтобы тонкие линии "поймали"
            # контраст по всей длине, а не только в массивных местах.
            glow_alpha = zone.split()[3].filter(ImageFilter.MaxFilter(7)).filter(ImageFilter.GaussianBlur(22))
            glow = Image.new("RGBA", (zone_w, zone_h), (255, 255, 255, 0))
            glow.putalpha(glow_alpha.point(lambda x: int(x * 0.55)))
            img_rgba.paste(glow, (PRD_X1 + 13, PRD_Y1 + 8), glow)
        else:
            # Светлый товар на тёмном фоне — обычная мягкая тень для объёма.
            shadow_alpha = zone.split()[3].filter(ImageFilter.GaussianBlur(18))
            shadow = Image.new("RGBA", (zone_w, zone_h), (0, 0, 0, 0))
            shadow.putalpha(shadow_alpha.point(lambda x: int(x * 0.28)))
            img_rgba.paste(shadow, (PRD_X1 + 23, PRD_Y1 + 18), shadow)

        img_rgba.paste(zone, (PRD_X1 + 13, PRD_Y1 + 8), zone)
        img = img_rgba.convert("RGB")

    # 6. Header text (over title box)
    _draw_header(img, product, brand, category, accent)

    # 6. Characteristics (over chars box)
    _draw_chars(img, features, accent)

    # 7. Watermark — белый на тёмном фоне, слева внизу
    result = paste_brand_watermark(img, corner="bottom-left", scale=0.32, bg_dark=True)

    buf = io.BytesIO()
    result.save(buf, format="JPEG", quality=92)
    return buf.getvalue()
