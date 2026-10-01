"""
Speaker / acoustic infographic template.
Dark background with radial sound-wave glow from center.
Layout: title box (top) | 2×2 characteristics grid (left) | product zone (right).
"""
import io
import logging
import math
import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from .fonts import fit_font, get_font
from .background import remove_background, remove_solid_background, place_on_transparent
from utils.gpu_safety import run_gpu
from .logo import paste_brand_watermark

log = logging.getLogger(__name__)

W, H = 768, 1024

BORDER = 4
RADIUS = 24

# Title box — between WB badge zones
HDR_X1, HDR_Y1 = 185, 26
HDR_X2, HDR_Y2 = 730, 198
HDR_H = HDR_Y2 - HDR_Y1   # 172

# Left zone for 2×2 grid
GRID_X1, GRID_Y1 = 16,  212
GRID_X2, GRID_Y2 = 346, 862
GRID_W = GRID_X2 - GRID_X1   # 330
GRID_H = GRID_Y2 - GRID_Y1   # 650
GRID_GAP = 10

# Product zone (right)
PRD_X1, PRD_Y1 = 316, 212
PRD_X2, PRD_Y2 = 752, 1004
PRD_W = PRD_X2 - PRD_X1   # 436
PRD_H = PRD_Y2 - PRD_Y1   # 792

DEFAULT_ACCENT = (0, 140, 210)   # speaker-синий по умолчанию


def make_speaker_bg(w: int, h: int, accent: tuple[int, int, int]) -> Image.Image:
    """Light base + subtle radial tint from center + concentric sound-wave rings."""
    r, g, b = accent
    base = Image.new("RGBA", (w, h), (250, 251, 255, 255))

    # Лёгкий radial tint — очень нежный цветной градиент от центра
    glow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    cx, cy = w // 2, int(h * 0.48)
    for i in range(10, 0, -1):
        alpha = int(22 * (i / 10))
        rx = int(w * 0.55 * (i / 10))
        ry = int(h * 0.45 * (i / 10))
        gd.ellipse((cx - rx, cy - ry, cx + rx, cy + ry), fill=(r, g, b, alpha))
    glow = glow.filter(ImageFilter.GaussianBlur(70))

    # Концентрические кольца (звуковые волны) — тонкие, светлые
    rings = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    rd = ImageDraw.Draw(rings)
    for i, ring_r in enumerate(range(90, min(w, h) // 2 + 80, 60)):
        alpha = max(8, 38 - i * 6)
        rd.ellipse(
            (cx - ring_r, cy - ring_r, cx + ring_r, cy + ring_r),
            outline=(r, g, b, alpha), width=2,
        )
    rings = rings.filter(ImageFilter.GaussianBlur(1.2))

    img = Image.alpha_composite(base, glow)
    img = Image.alpha_composite(img, rings)
    return img.convert("RGB")


def _draw_cone_rings(img: Image.Image, accent: tuple[int, int, int]) -> Image.Image:
    """Концентрические кольца конуса динамика как текстура в зоне товара."""
    r, g, b = accent
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    cx = (PRD_X1 + PRD_X2) // 2
    cy = (PRD_Y1 + PRD_Y2) // 2
    for i, ring_r in enumerate(range(35, 220, 38)):
        alpha = max(10, 45 - i * 8)
        ld.ellipse(
            (cx - ring_r, cy - ring_r, cx + ring_r, cy + ring_r),
            outline=(r, g, b, alpha), width=2,
        )
    layer = layer.filter(ImageFilter.GaussianBlur(1.0))
    return Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")


def _draw_eq_bars(img: Image.Image, accent: tuple[int, int, int]) -> Image.Image:
    """EQ-визуализатор в пустом пространстве под сеткой характеристик."""
    r, g, b = accent
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)

    area_x1, area_y1 = GRID_X1 + 12, GRID_Y2 + 18
    area_x2, area_y2 = GRID_X2 - 12, H - 28
    area_w = area_x2 - area_x1
    area_h = area_y2 - area_y1

    n_bars = 22
    bar_w = max(4, area_w // n_bars - 3)
    gap   = (area_w - n_bars * bar_w) // (n_bars - 1)

    for i in range(n_bars):
        # Синусоидальная огибающая — плавный EQ-профиль
        t = i / (n_bars - 1)
        height_frac = 0.30 + 0.55 * math.sin(math.pi * t) + 0.15 * math.sin(3 * math.pi * t + 0.4)
        height_frac = max(0.12, min(1.0, height_frac))
        bar_h = int(area_h * height_frac)

        x1 = area_x1 + i * (bar_w + gap)
        y2 = area_y2
        y1 = y2 - bar_h

        # Градиент: снизу ярче, сверху бледнее
        for seg in range(bar_h):
            seg_alpha = int(90 * (seg / bar_h) + 20)
            ld.rectangle([x1, y2 - seg - 1, x1 + bar_w - 1, y2 - seg],
                         fill=(r, g, b, seg_alpha))

    return Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")


def _draw_boxes(img: Image.Image, accent: tuple[int, int, int],
                product_dark: bool = False) -> None:
    r, g, b = accent
    draw = ImageDraw.Draw(img)

    box_fill  = (255, 255, 255)            # белый
    prd_fill  = (240, 242, 248)            # чуть серее для зоны товара
    cell_fill = (
        min(255, 235 + r // 12),
        min(255, 235 + g // 12),
        min(255, 240 + b // 12),
    )                                      # очень лёгкий акцентный оттенок

    # 1. Product zone
    draw.rounded_rectangle(
        [PRD_X1, PRD_Y1, PRD_X2, PRD_Y2],
        radius=RADIUS, fill=prd_fill, outline=(r, g, b), width=BORDER,
    )

    # 2. 2×2 grid — 4 ячейки
    cell_w = (GRID_W - GRID_GAP) // 2
    cell_h = (GRID_H - GRID_GAP) // 2
    for row in range(2):
        for col in range(2):
            x1 = GRID_X1 + col * (cell_w + GRID_GAP)
            y1 = GRID_Y1 + row * (cell_h + GRID_GAP)
            x2 = x1 + cell_w
            y2 = y1 + cell_h
            draw.rounded_rectangle(
                [x1, y1, x2, y2],
                radius=RADIUS, fill=cell_fill, outline=(r, g, b), width=BORDER,
            )

    # 3. Title box — поверх всего
    draw.rounded_rectangle(
        [HDR_X1, HDR_Y1, HDR_X2, HDR_Y2],
        radius=RADIUS, fill=box_fill, outline=(r, g, b), width=BORDER,
    )
    # Тонкая акцентная полоса у нижнего края заголовка
    draw.rounded_rectangle(
        [HDR_X1 + 24, HDR_Y2 - 8, HDR_X2 - 24, HDR_Y2 - 4],
        radius=2, fill=(r, g, b),
    )


def _draw_header(img: Image.Image, product: str, brand: str,
                 category: str, accent: tuple[int, int, int]) -> None:
    r, g, b = accent
    draw = ImageDraw.Draw(img)
    cx = (HDR_X1 + HDR_X2) // 2
    avail_w = HDR_X2 - HDR_X1 - 32

    cat_text = (category or "АКУСТИКА").upper()

    if brand:
        f_cat = fit_font(draw, cat_text, avail_w, "title", int(HDR_H * 0.50))
        draw.text((cx, HDR_Y1 + 30), cat_text, font=f_cat, fill=(r, g, b), anchor="mt")
        model_str = product
        if model_str.lower().startswith(brand.lower()):
            model_str = model_str[len(brand):].strip()
        bm = f"{brand.upper()}  {model_str}" if model_str else brand.upper()
        f_bm = fit_font(draw, bm, avail_w, "label", int(HDR_H * 0.27))
        draw.text((cx, HDR_Y2 - 32), bm, font=f_bm, fill=(40, 40, 60), anchor="mb")
    else:
        f_cat = fit_font(draw, cat_text, avail_w, "title", int(HDR_H * 0.50))
        draw.text((cx, HDR_Y1 + 30), cat_text, font=f_cat, fill=(r, g, b), anchor="mt")
        f_prod = fit_font(draw, product, avail_w, "label", int(HDR_H * 0.27))
        draw.text((cx, HDR_Y2 - 32), product, font=f_prod, fill=(40, 40, 60), anchor="mb")


def _draw_grid(img: Image.Image, features: list[tuple[str, str]],
               accent: tuple[int, int, int]) -> None:
    """4 характеристики в сетке 2×2."""
    r, g, b = accent
    draw = ImageDraw.Draw(img)

    n = min(4, len(features))
    if n == 0:
        return

    cell_w = (GRID_W - GRID_GAP) // 2
    cell_h = (GRID_H - GRID_GAP) // 2

    for i, (label, value) in enumerate(features[:n]):
        row = i // 2
        col = i % 2
        x1 = GRID_X1 + col * (cell_w + GRID_GAP)
        y1 = GRID_Y1 + row * (cell_h + GRID_GAP)
        cx = x1 + cell_w // 2
        cy = y1 + cell_h // 2

        avail_w = cell_w - 24

        # Label — accent, сверху
        lbl_sz = max(12, int(cell_h * 0.18))
        f_lbl = fit_font(draw, label.upper(), avail_w, "label", lbl_sz)
        draw.text((cx, y1 + int(cell_h * 0.28)), label.upper(),
                  font=f_lbl, fill=(r, g, b), anchor="mm")

        # Разделитель
        div_r = min(255, (r * 2 + 255 * 3) // 5)
        div_g = min(255, (g * 2 + 255 * 3) // 5)
        div_b = min(255, (b * 2 + 255 * 3) // 5)
        lw = int(avail_w * 0.6)
        draw.line([(cx - lw // 2, cy - 4), (cx + lw // 2, cy - 4)],
                  fill=(div_r, div_g, div_b), width=1)

        # Value — тёмный, снизу
        val_sz = max(15, int(cell_h * 0.30))
        f_val = fit_font(draw, value, avail_w, "value", val_sz)
        draw.text((cx, y1 + int(cell_h * 0.68)), value,
                  font=f_val, fill=(25, 25, 45), anchor="mm")


async def build_speakers(
    product: str,
    features: list[tuple[str, str]],
    product_img_bytes: bytes | None,
    brand: str = "",
    category: str = "",
    accent: tuple[int, int, int] = DEFAULT_ACCENT,
) -> bytes:
    """Build speaker-style infographic and return JPEG bytes."""

    img = make_speaker_bg(W, H, accent)

    product_rgba = None
    product_dark = False
    if product_img_bytes:
        product_rgba = await run_gpu(remove_background, product_img_bytes, timeout=110, label="remove_background")
        if product_rgba is None:
            log.warning("rembg failed/timeout — fallback: white bg removal")
            product_rgba = await run_gpu(remove_solid_background, product_img_bytes,
                                         label="remove_solid_background")

        if product_rgba is not None:
            arr = np.array(product_rgba.convert("RGBA"))
            alpha = arr[:, :, 3]
            mask = alpha > 30
            if mask.sum() >= 200:
                px = arr[:, :, :3][mask].astype(float)
                brightness = float(0.299*px[:,0].mean() + 0.587*px[:,1].mean() + 0.114*px[:,2].mean())
                product_dark = brightness < 100

    _draw_boxes(img, accent, product_dark=product_dark)
    img = _draw_cone_rings(img, accent)

    if product_rgba is not None:
        zone_w = PRD_W - 16
        zone_h = PRD_H - 16
        zone = place_on_transparent(product_rgba, zone_w, zone_h, padding=16)

        shadow_alpha = zone.split()[3].filter(ImageFilter.GaussianBlur(20))
        shadow = Image.new("RGBA", (zone_w, zone_h), (0, 0, 0, 0))
        shadow.putalpha(shadow_alpha.point(lambda x: int(x * 0.30)))

        img_rgba = img.convert("RGBA")
        img_rgba.paste(shadow, (PRD_X1 + 18, PRD_Y1 + 14), shadow)
        img_rgba.paste(zone,   (PRD_X1 + 8,  PRD_Y1 + 4),  zone)
        img = img_rgba.convert("RGB")

    _draw_header(img, product, brand, category, accent)
    _draw_grid(img, features, accent)
    img = _draw_eq_bars(img, accent)

    result = paste_brand_watermark(img, corner="bottom-left", scale=0.32, bg_dark=False)

    buf = io.BytesIO()
    result.save(buf, format="JPEG", quality=92)
    return buf.getvalue()
