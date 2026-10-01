import io
import logging
from PIL import Image, ImageDraw, ImageFilter

from .fonts import fit_font, best_lines, get_font
from .background import remove_background, remove_solid_background, place_on_transparent
from .infographic_gaming import _product_brightness, _PRODUCT_DARK_THRESHOLD
from utils.gpu_safety import run_gpu

log = logging.getLogger(__name__)

W, H         = 768, 1024
# Слепые зоны WB-плашек (углы), которые НЕЛЬЗЯ закрывать смыслом:
# верх-лево/верх-право ~ y<130, низ-лево/низ-право ~ y>860.
# Полезный контент рисуется в зоне 130..860 по вертикали.
HEADER_H     = 130
BODY_Y       = HEADER_H              # фото и блоки начинаются на y=130
BODY_BOTTOM  = 860                   # ниже — слепые зоны WB
BODY_H       = BODY_BOTTOM - BODY_Y  # 730

TOP_OFFSET   = 35
H_GAP        = 16
RIGHT_PAD    = 16

PHOTO_W      = 421
BLOCKS_X     = PHOTO_W + H_GAP       # 437
BLOCKS_W     = W - BLOCKS_X - RIGHT_PAD  # 315
BLOCKS_TOP   = BODY_Y + TOP_OFFSET   # 165
BLOCKS_H     = BODY_BOTTOM - BLOCKS_TOP  # 695

# ── Шапка ─────────────────────────────────────────────────
# Логотип НЕ рисуем — закрывается WB-плашкой в левом верхнем углу.
# Тип товара (крупно) и модель — в центральной колонке (между плашками).
TITLE_Y_TOP  = 50
TITLE_Y_BOT  = 112
SLOGAN_Y_TOP = 115
SLOGAN_Y_BOT = 145
TITLE_PAD_H  = 160                   # горизонтальный padding — не лезть в боковые плашки
TITLE_PAD_B  = 3
SLOGAN_PAD_B = 2

BLOCK_PAD    = 12
BLOCK_W      = BLOCKS_W - BLOCK_PAD * 2   # 291
TEXT_PAD     = 12
MAX_CHARS    = 20   # заголовок — полное слово (ШУМОПОДАВЛЕНИЕ = 14 символов)
VALUE_MAX    = 20   # значение — fit_font ужмёт по ширине, лимит лишь страховка

# Пересчитаны под BLOCKS_H=824: (block_h, gap)
_BLOCK_PARAMS: dict[int, tuple[int, int]] = {
    2: (200, 50),
    3: (175, 37),
    4: (148, 29),
    5: (120, 23),
    6: (100, 18),
}


def _block_positions(n: int) -> tuple[list[int], int]:
    n = max(2, min(6, n))
    block_h, gap = _BLOCK_PARAMS[n]
    total   = n * block_h + (n - 1) * gap
    margin  = (BLOCKS_H - total) // 2
    y_start = BLOCKS_TOP + margin
    return [y_start + i * (block_h + gap) for i in range(n)], block_h


def _truncate(text: str, limit: int = MAX_CHARS) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _draw_block_backgrounds(overlay: Image.Image,
                             features: list[tuple[str, str]], dark_bg: bool):
    n = max(2, min(6, len(features)))
    positions, block_h = _block_positions(n)
    draw_o = ImageDraw.Draw(overlay)

    shadow_color = (0, 0, 0, 60)
    block_fill   = (255, 255, 255, 50) if dark_bg else (255, 255, 255, 220)

    for by in positions:
        bx = BLOCKS_X + BLOCK_PAD
        draw_o.rounded_rectangle(
            [bx + 4, by + 4, bx + BLOCK_W + 4, by + block_h + 4],
            radius=12, fill=shadow_color,
        )
        draw_o.rounded_rectangle(
            [bx, by, bx + BLOCK_W, by + block_h],
            radius=12, fill=block_fill,
        )


def _draw_block_texts(draw: ImageDraw.ImageDraw,
                      features: list[tuple[str, str]], dark_bg: bool):
    n = max(2, min(6, len(features)))
    positions, block_h = _block_positions(n)

    lbl_color = (160, 175, 215) if dark_bg else (110, 120, 145)
    val_color = (235, 240, 255) if dark_bg else (15,  15,  15)
    text_w    = BLOCK_W - TEXT_PAD * 2
    part_h    = block_h // 3

    for i, (title, value) in enumerate(features[:n]):
        bx = BLOCKS_X + BLOCK_PAD
        by = positions[i]
        cx = bx + BLOCK_W // 2

        label  = _truncate(title.upper())
        lbl_sz = max(9, int(part_h * 0.55))
        f_lbl  = fit_font(draw, label, text_w, "label", lbl_sz)
        lbl_bottom = by + part_h - int(part_h * 0.08)
        draw.text((cx, lbl_bottom), label, font=f_lbl, fill=lbl_color, anchor="mb")

        value_str = _truncate(value, VALUE_MAX)
        val_sz    = max(11, int(part_h * 2 * 0.50))
        f_val     = fit_font(draw, value_str, text_w, "value", val_sz)
        val_cy    = by + part_h + part_h
        draw.text((cx, val_cy), value_str, font=f_val, fill=val_color, anchor="mm")


def _draw_header_background(overlay: Image.Image, title_text: str, subtitle_text: str, dark_bg: bool):
    """Полупрозрачная подложка под шапку — чтобы текст не сливался с фоном."""
    if not title_text and not subtitle_text:
        return
    draw_o = ImageDraw.Draw(overlay)

    shadow_color = (0, 0, 0, 60)
    block_fill   = (255, 255, 255, 50) if dark_bg else (255, 255, 255, 220)

    x0, x1 = TITLE_PAD_H - 10, W - TITLE_PAD_H + 10
    y0 = (TITLE_Y_TOP if title_text else SLOGAN_Y_TOP) - 8
    y1 = (SLOGAN_Y_BOT if subtitle_text else TITLE_Y_BOT) + 8

    draw_o.rounded_rectangle([x0 + 4, y0 + 4, x1 + 4, y1 + 4], radius=14, fill=shadow_color)
    draw_o.rounded_rectangle([x0, y0, x1, y1], radius=14, fill=block_fill)


async def _draw_header(img: Image.Image, draw: ImageDraw.ImageDraw,
                        title_text: str, subtitle_text: str, dark_bg: bool,
                        big_title: bool):
    """
    Шапка (только центральная колонка между верхними WB-плашками):
      Верхняя строка (TITLE_PAD_H..W-TITLE_PAD_H, TITLE_Y_TOP..TITLE_Y_BOT)
      Нижняя строка  (TITLE_PAD_H..W-TITLE_PAD_H, SLOGAN_Y_TOP..SLOGAN_Y_BOT)

    big_title=True  — header_title/header_model: тип товара крупно одной
                       строкой ("Точка доступа", "Монитор", "Мышка") + модель
                       помельче под ним.
    big_title=False — фоллбэк (LLM не вернул тип/модель): полное название
                       в 1-2 строки + слоган, как раньше.
    Логотип НЕ рисуется — закрывается WB-плашкой в углу.
    """
    text_color   = (20, 40, 80) if not dark_bg else (230, 238, 255)
    slogan_color = (45, 58, 100) if not dark_bg else (200, 212, 240)

    title_w = W - TITLE_PAD_H * 2 - 20
    title_h = TITLE_Y_BOT - TITLE_Y_TOP

    if title_text:
        if big_title:
            # Тип товара — короткий, рисуем одной строкой крупным шрифтом.
            max_size = min(int(title_h * 0.85), 44)
            f_title  = fit_font(draw, title_text, title_w, "title", max_size)
            draw.text((W // 2, (TITLE_Y_TOP + TITLE_Y_BOT) // 2),
                      title_text, font=f_title, fill=text_color, anchor="mm")
        else:
            # Полное название — может быть длинным, разбиваем на 1-2 строки.
            max_size = max(12, int(title_h / 2 / 1.15))
            lines, sz = best_lines(draw, title_text, title_w, "title", max_size)
            f_title  = get_font(sz, "title")
            line_h   = int(sz * 1.18)
            total_h  = line_h * len(lines)
            cy = TITLE_Y_TOP + (title_h - total_h) // 2 + line_h // 2
            for line in lines:
                draw.text((W // 2, cy), line, font=f_title, fill=text_color, anchor="mm")
                cy += line_h

    # ── Нижняя строка (модель/слоган, одна строка, по подложке) ──
    if subtitle_text:
        sub_w = W - TITLE_PAD_H * 2 - 20
        sub_h = SLOGAN_Y_BOT - SLOGAN_Y_TOP
        f_sub = fit_font(draw, subtitle_text, sub_w, "label", int(sub_h * 1.1))
        draw.text((W // 2, (SLOGAN_Y_TOP + SLOGAN_Y_BOT) // 2),
                  subtitle_text, font=f_sub, fill=slogan_color, anchor="mm")


async def overlay(bg_img: Image.Image, product: str,
                  features: list[tuple[str, str]],
                  product_img_bytes: bytes | None,
                  brand: str = "", slogan: str = "",
                  header_title: str = "", header_model: str = "") -> Image.Image:

    img = bg_img.resize((W, H), Image.LANCZOS).convert("RGBA")

    sample = img.crop((BLOCKS_X, BODY_Y, W, BODY_Y + 100)).convert("L")
    avg = sum(sample.getdata()) / (sample.width * sample.height)
    dark_bg = avg < 110

    # ── Фото товара ─────────────────────────────────────────
    if product_img_bytes:
        product_rgba = await run_gpu(remove_background, product_img_bytes, timeout=110, label="remove_background")
        if product_rgba is None:
            log.warning("rembg failed/timeout — fallback: white bg removal")
            product_rgba = await run_gpu(remove_solid_background, product_img_bytes,
                                         label="remove_solid_background")
        if product_rgba is None:
            log.warning("white bg removal тоже не удалась — кладём фото как есть")
            product_rgba = Image.open(io.BytesIO(product_img_bytes)).convert("RGBA")
        log.info(f"Product image after rembg: {product_rgba.size}")

        if dark_bg and _product_brightness(product_rgba) < _PRODUCT_DARK_THRESHOLD:
            # Тёмный товар (тонкая гарнитура, провода и т.п.) на тёмной
            # палитре фона иначе почти полностью теряется — gaming/widget-
            # стили решают это подсветкой бокса под товар (см. _draw_boxes /
            # draw_widget_boxes с product_dark), у обычного фона нет
            # фиксированного бокса — подкладываем мягкое светлое свечение.
            glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            gd = ImageDraw.Draw(glow)
            gcx, gcy = PHOTO_W // 2, BODY_Y + BODY_H // 2
            for i in range(10, 0, -1):
                alpha = int(70 * (i / 10))
                rx = int(PHOTO_W * 0.55 * (i / 10))
                ry = int(BODY_H * 0.42 * (i / 10))
                gd.ellipse((gcx - rx, gcy - ry, gcx + rx, gcy + ry), fill=(255, 255, 255, alpha))
            glow = glow.filter(ImageFilter.GaussianBlur(40))
            img = Image.alpha_composite(img, glow)
            log.info("Dark product on dark bg → добавлено светлое свечение под зону товара")

        _asp = product_rgba.width / max(product_rgba.height, 1)
        # Широкий объект (монитор, планшет-дисплей): масштабируем крупнее
        # чтобы занимал ~55% высоты зоны вместо ~25%, без поворота и обрезки
        _pad = 8 if _asp > 1.3 else 12
        # Кабели/переходники (очень вытянутые): уменьшаем масштаб чтобы
        # товар не заполнял зону вплотную к краям
        _xsc = 0.82 if _asp > 2.5 else 1.0
        photo_zone = place_on_transparent(product_rgba, PHOTO_W, BODY_H, padding=_pad, extra_scale=_xsc)
        img.paste(photo_zone, (0, BODY_Y), photo_zone)

    # ── Заголовок шапки: тип товара + модель (фоллбэк — название + слоган) ──
    big_title = bool(header_title)
    title_text    = header_title if big_title else product
    subtitle_text = header_model if big_title else slogan

    # ── Фоны блоков + подложка под шапку ──
    overlay_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    _draw_header_background(overlay_layer, title_text, subtitle_text, dark_bg)
    _draw_block_backgrounds(overlay_layer, features, dark_bg)
    img = Image.alpha_composite(img, overlay_layer)

    # ── Текст блоков ──
    draw = ImageDraw.Draw(img)
    _draw_block_texts(draw, features, dark_bg)

    # ── Шапка ──
    await _draw_header(img, draw, title_text, subtitle_text, dark_bg, big_title)

    return img.convert("RGB")


async def build(bg_bytes: bytes, product: str,
                features: list[tuple[str, str]],
                product_img_bytes: bytes | None,
                brand: str = "", slogan: str = "",
                header_title: str = "", header_model: str = "") -> bytes:
    bg  = Image.open(io.BytesIO(bg_bytes)).convert("RGBA")
    result = await overlay(bg, product, features, product_img_bytes, brand, slogan,
                            header_title=header_title, header_model=header_model)
    from .logo import paste_brand_watermark
    result = paste_brand_watermark(result, corner="bottom-left", scale=0.39)
    buf = io.BytesIO()
    result.save(buf, format="JPEG", quality=92)
    return buf.getvalue()
