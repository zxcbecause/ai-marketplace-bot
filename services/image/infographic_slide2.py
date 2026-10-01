"""
Второй слайд инфографики — «чистый» слайд (решение сессии 2026-07-02):
фото ВТОРОГО ракурса товара + ОДНА главная характеристика крупно +
слоган снизу (переиспользуется rich_slogan из рич-контента).

Стиль — как у общего шаблона (infographic.py): светлый локальный градиент
(ключ палитры передаётся снаружи, чтобы слайды 1 и 2 были в одной гамме),
белая плашка с тенью, вотермарк в левом нижнем углу.

Пока включён только для категории «Мониторы» (ozon_pipeline + /image).
"""
import io
import logging

from PIL import Image, ImageDraw

from .fonts import fit_font, best_lines, get_font
from .background import remove_background, remove_solid_background, place_on_transparent
from utils.gpu_safety import run_gpu

log = logging.getLogger(__name__)

W, H = 768, 1024
# Слепые зоны маркетплейс-плашек (углы) — как в infographic.py:
# полезный контент держим в вертикальной зоне 130..860.
BODY_Y     = 130
PHOTO_BOT  = 640                 # низ фото-зоны
CARD_W     = 500                 # плашка характеристики
# Плашка+слоган опущены на 20px (просьба 05.07) — больше воздуха под фото
CARD_TOP   = 688
CARD_BOT   = 820
SLOGAN_TOP = 830
SLOGAN_BOT = 882
SIDE_PAD   = 24


def _draw_card(img: Image.Image, dark_bg: bool) -> Image.Image:
    """Полупрозрачная плашка в тон фона (просьба 05.07: не белую).
    Цвет = средний цвет фона под плашкой, слегка осветлённый — плашка
    «подкрашена в тему» палитры и пропускает градиент."""
    x0 = (W - CARD_W) // 2
    x1 = x0 + CARD_W

    region = img.crop((x0, CARD_TOP, x1, CARD_BOT)).convert("RGB")
    px = list(region.resize((8, 4)).getdata())
    avg = tuple(sum(c[i] for c in px) // len(px) for i in range(3))
    if dark_bg:
        tint = tuple(int(c * 0.75) for c in avg)          # чуть темнее фона
    else:
        tint = tuple(int(c * 0.55 + 255 * 0.45) for c in avg)  # чуть светлее

    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    do = ImageDraw.Draw(overlay)
    do.rounded_rectangle([x0 + 4, CARD_TOP + 4, x1 + 4, CARD_BOT + 4],
                          radius=14, fill=(0, 0, 0, 40))
    do.rounded_rectangle([x0, CARD_TOP, x1, CARD_BOT],
                          radius=14, fill=(*tint, 150),
                          outline=(255, 255, 255, 110), width=2)
    return Image.alpha_composite(img, overlay)


async def build_slide2(bg_bytes: bytes,
                       feature: tuple[str, str],
                       slogan: str,
                       product_img_bytes: bytes) -> bytes:
    """
    bg_bytes  — фон (local_bg.generate_background, та же палитра что слайд 1)
    feature   — (заголовок, значение) главной характеристики, напр.
                ("Диагональ", "27\" QHD")
    slogan    — фраза снизу (rich_slogan, 10-16 слов; допустимы 1-2 строки)
    product_img_bytes — фото второго ракурса (photos[1])
    """
    img = Image.open(io.BytesIO(bg_bytes)).convert("RGBA")
    if img.size != (W, H):
        img = img.resize((W, H), Image.LANCZOS)

    sample = img.crop((0, BODY_Y, W, BODY_Y + 100)).convert("L")
    avg = sum(sample.getdata()) / (sample.width * sample.height)
    dark_bg = avg < 110

    # ── Фото второго ракурса — крупно, во всю ширину полезной зоны ──
    product_rgba = await run_gpu(remove_background, product_img_bytes, timeout=110, label="remove_background")
    if product_rgba is None:
        log.warning("slide2: rembg failed/timeout — fallback: white bg removal")
        product_rgba = await run_gpu(remove_solid_background, product_img_bytes,
                                     label="remove_solid_background")
    if product_rgba is None:
        log.warning("slide2: white bg removal тоже не удалась — кладём фото как есть")
        product_rgba = Image.open(io.BytesIO(product_img_bytes)).convert("RGBA")

    _asp = product_rgba.width / max(product_rgba.height, 1)
    _pad = 8 if _asp > 1.3 else 12
    photo_zone = place_on_transparent(
        product_rgba, W - SIDE_PAD * 2, PHOTO_BOT - BODY_Y, padding=_pad,
    )
    img.paste(photo_zone, (SIDE_PAD, BODY_Y), photo_zone)

    # ── Плашка с одной главной характеристикой ──
    img = _draw_card(img, dark_bg)
    draw = ImageDraw.Draw(img)

    lbl_color = (160, 175, 215) if dark_bg else (110, 120, 145)
    val_color = (235, 240, 255) if dark_bg else (15, 15, 15)
    text_w    = CARD_W - 32
    cx        = W // 2
    card_h    = CARD_BOT - CARD_TOP
    part_h    = card_h // 3

    title, value = feature
    label = title.upper()
    f_lbl = fit_font(draw, label, text_w, "label", max(12, int(part_h * 0.55)))
    draw.text((cx, CARD_TOP + part_h - int(part_h * 0.08)), label,
              font=f_lbl, fill=lbl_color, anchor="mb")

    f_val = fit_font(draw, value, text_w, "value", max(14, int(part_h * 2 * 0.52)))
    draw.text((cx, CARD_TOP + part_h * 2), value,
              font=f_val, fill=val_color, anchor="mm")

    # ── Слоган снизу — 1-2 строки, жирный и контрастный (просьба 05.07:
    #    label-шрифт приглушённого тона читался невнятно) ──
    if slogan:
        slg_color = (240, 244, 255) if dark_bg else (22, 36, 78)
        slg_w = W - SIDE_PAD * 2 - 40
        lines, sz = best_lines(draw, slogan, slg_w, "title",
                                max(16, int((SLOGAN_BOT - SLOGAN_TOP) * 0.58)))
        f_slg  = get_font(sz, "title")
        line_h = int(sz * 1.22)
        total  = line_h * len(lines)
        cy = SLOGAN_TOP + ((SLOGAN_BOT - SLOGAN_TOP) - total) // 2 + line_h // 2
        for line in lines:
            draw.text((cx, cy), line, font=f_slg, fill=slg_color, anchor="mm")
            cy += line_h

    result = img.convert("RGB")
    from .logo import paste_brand_watermark
    result = paste_brand_watermark(result, corner="bottom-left", scale=0.39)
    buf = io.BytesIO()
    result.save(buf, format="JPEG", quality=92)
    return buf.getvalue()
