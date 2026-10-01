import io
from PIL import Image, ImageDraw, ImageFilter

from .fonts import fit_font, wrap_text, get_font


def _truncate_with_ellipsis(draw: ImageDraw.ImageDraw, text: str,
                             font, max_w: int) -> str:
    """Возвращает строку + «…» (всегда), при необходимости укоротив
    конец так, чтобы «text + …» уложилось в max_w. Вызывается только когда
    есть остаток текста дальше — поэтому многоточие обязательно."""
    candidate = text + "…"
    try:
        w = draw.textlength(candidate, font=font)
    except Exception:
        w = len(candidate) * font.size * 0.6
    while w > max_w and len(text) > 1:
        text = text[:-1]
        candidate = text + "…"
        try:
            w = draw.textlength(candidate, font=font)
        except Exception:
            w = len(candidate) * font.size * 0.6
    return candidate


def _fit_lines_in_box(draw: ImageDraw.ImageDraw, text: str, max_w: int,
                      max_h: int, role: str, max_size: int,
                      hard_max_lines: int = 5) -> tuple[list[str], object]:
    """
    Подбирает строки + шрифт так, чтобы суммарная высота уложилась в max_h.
    Если текст всё равно не влезает — обрезает последнюю строку с "…".
    """
    lines, font = wrap_text(draw, text, max_w, role, max_size, max_lines=hard_max_lines)
    lh = font.size + 3
    fit_count = max(1, max_h // lh)
    if len(lines) > fit_count:
        lines = lines[:fit_count]
        lines[-1] = _truncate_with_ellipsis(draw, lines[-1], font, max_w)
    return lines, font


def _wrap_fixed_size(draw: ImageDraw.ImageDraw, text: str, max_w: int,
                      role: str, size: int) -> list[str]:
    """Разбивает текст на строки с ФИКСИРОВАННЫМ размером шрифта.
    Используется чтобы у всех блоков преимуществ был одинаковый кегль."""
    from .fonts import get_font
    font = get_font(size, role)
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        test = (current + " " + word).strip()
        try:
            w = draw.textlength(test, font=font)
        except Exception:
            w = len(test) * size * 0.6
        if w <= max_w:
            current = test
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _fit_fixed_size(draw: ImageDraw.ImageDraw, text: str, max_w: int,
                     max_h: int, role: str, size: int) -> tuple[list[str], object]:
    """То же что _fit_lines_in_box, но без подбора шрифта — фиксированный
    размер. Если строки не помещаются в max_h, обрезаем с «…»."""
    from .fonts import get_font
    font = get_font(size, role)
    lines = _wrap_fixed_size(draw, text, max_w, role, size)
    lh = font.size + 3
    fit_count = max(1, max_h // lh)
    if len(lines) > fit_count:
        lines = lines[:fit_count]
        lines[-1] = _truncate_with_ellipsis(draw, lines[-1], font, max_w)
    return lines, font


def overlay(img: Image.Image, product: str,
            features: list[tuple[str, str]],
            tips: list[tuple[str, str]],
            accent: tuple[int, int, int] | None = None) -> Image.Image:
    W, H = img.size

    NAV   = (20,  40,  80)
    GRAY  = (110, 120, 140)

    # Зоны (тюнинг)
    col_top  = int(H * 0.05)
    body_y   = int(H * 0.25)        # подняли с 0.32 — больше места под блоки
    body_h   = int(H * 0.72)        # +10%H к высоте блоков, чтобы слоган уложился

    mid_x    = int(W * 0.33)
    mid_w    = int(W * 0.285)
    col_gap  = int(W * 0.025)
    rt_x     = mid_x + mid_w + col_gap
    rt_w     = W - rt_x - int(W * 0.02)

    draw_m = ImageDraw.Draw(img)

    # Сплошная матовая подложка УБРАНА (2026-06-02): она высветляла весь фон под
    # колонками и цветной mesh был не виден («рич словно без фона»). Блоки и так
    # на собственных белых плашках — между ними и за заголовками теперь виден
    # цветной фон, как на инфографике-референсе (белые карточки на градиенте).
    draw_m = ImageDraw.Draw(img)

    # Цвета по яркости frosted glass (или gaming-override через accent)
    if accent is not None:
        dark_bg = True
        ar, ag, ab = accent
        text_col = (230, 238, 255)
        val_col  = (245, 245, 255)
        gray_col = (min(255, ar // 2 + 90), min(255, ag // 2 + 90), min(255, ab // 2 + 90))
        blk_fill = (18, 18, 32, 210)
        hdr_col  = accent
    else:
        sample2 = img.crop((mid_x, body_y, mid_x + mid_w, body_y + 50)).convert("L")
        bg_bright = sum(sample2.getdata()) / (mid_w * 50)
        dark_bg = bg_bright < 110
        text_col = (230, 238, 255) if dark_bg else NAV
        val_col  = (210, 220, 240) if dark_bg else (30,  40,  60)
        gray_col = (160, 175, 210) if dark_bg else GRAY
        blk_fill = (30, 40, 80, 40) if dark_bg else (255, 255, 255, 220)
        hdr_col  = text_col

    # ── Название товара — крупно, прижато к заголовкам колонок (исходный
    #     вариант до правок шапки). anchor=mb, baseline за 7%H до body_y.
    if product:
        title_x      = mid_x
        title_w      = (rt_x + rt_w) - mid_x
        title_y_top  = col_top + int(H * 0.015)
        # −20px: поднимаем низ зоны названия → больше отступ до блоков,
        # чтобы название не сливалось с характеристиками/преимуществами.
        title_y_bot  = body_y - int(H * 0.07) - 20
        title_h      = title_y_bot - title_y_top
        title_cy     = (title_y_top + title_y_bot) // 2
        f_title = fit_font(draw_m, product, title_w - 24, "title",
                            int(title_h * 0.6))
        # Центрируем название по вертикали И горизонтали (anchor mm).
        draw_m.text((title_x + title_w // 2, title_cy), product,
                    font=f_title, fill=text_col, anchor="mm")

    # Заголовки колонок ХАРАКТЕРИСТИКИ / ПРЕИМУЩЕСТВА — сразу над блоками
    hdr_max = int(H * 0.038)
    hdr_y   = body_y - int(H * 0.025) - 30   # подняты на 30px
    f_hdr_m = fit_font(draw_m, "ХАРАКТЕРИСТИКИ", mid_w - 8, "label", hdr_max)
    f_hdr_r = fit_font(draw_m, "ПРЕИМУЩЕСТВА",   rt_w  - 8, "label", hdr_max)
    draw_m.text((mid_x + mid_w // 2, hdr_y), "ХАРАКТЕРИСТИКИ",
                font=f_hdr_m, fill=hdr_col, anchor="mm")
    draw_m.text((rt_x + rt_w // 2,   hdr_y), "ПРЕИМУЩЕСТВА",
                font=f_hdr_r, fill=hdr_col, anchor="mm")

    # ── Блоки ──
    n_specs  = min(len(features), 4)
    n_tips   = min(len(tips), 3)
    n_rows   = max(n_specs, n_tips)
    # Левая колонка (характеристики) — сетка как была.
    row_h    = int(body_h / n_rows * 0.86)
    row_gap  = int(body_h / n_rows * 0.14)

    # Правая колонка (преимущества): n_tips блоков растягиваются на всю
    # высоту левой колонки — низ последнего tip совпадает с низом последнего
    # блока характеристик. Промежутки равные (= row_gap).
    tip_gap = row_gap
    if n_specs > 0:
        specs_bottom = body_y + (n_specs - 1) * (row_h + row_gap) + row_h
        fill_h       = specs_bottom - body_y
        tip_row_h    = ((fill_h - tip_gap * (n_tips - 1)) // n_tips) if n_tips > 0 else row_h
    else:
        # Нет ни одной характеристики (n_specs=0) — формула выше опиралась на
        # n_specs>=1 и уходила в отрицательные значения (specs_bottom < body_y),
        # что валило rounded_rectangle с "y1 must be >= y0". Без левой колонки
        # тюльпаны просто используют обычную равномерную сетку (как и специи
        # получили бы при том же n_rows).
        tip_row_h = row_h

    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    dov = ImageDraw.Draw(ov)
    _outline = accent if accent else None
    _border  = 3 if accent else 0
    for i in range(n_specs):
        by = body_y + i * (row_h + row_gap)
        dov.rounded_rectangle([mid_x, by, mid_x + mid_w, by + row_h],
                               radius=10, fill=blk_fill,
                               outline=_outline, width=_border)
    for i in range(n_tips):
        by = body_y + i * (tip_row_h + tip_gap)
        dov.rounded_rectangle([rt_x, by, rt_x + rt_w, by + tip_row_h],
                               radius=10, fill=blk_fill,
                               outline=_outline, width=_border)
    img = Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB")
    draw = ImageDraw.Draw(img)

    # ── Характеристики (короткие значения, до 3 строк) ──
    # Единый размер шрифта лейбла И значения на ВСЮ колонку (не подбирается
    # отдельно под каждый блок) — иначе короткие лейблы вроде "СОСТАВ"
    # рисуются заметно крупнее длинных вроде "КОЛИЧЕСТВО ПРЕДМЕТОВ В
    # УПАКОВКЕ (ШТ.)" в соседней плашке, и колонка выглядит неровной
    # (баг замечен вживую 02.09.2026). Правая колонка (см. tips_desc_size
    # ниже) уже так делает — здесь то же самое.
    specs_slice = features[:n_specs]
    tw_specs = mid_w - int(mid_w * 0.10)
    lbl_cap  = max(9, int(row_h * 0.18))
    val_cap  = max(12, int(row_h * 0.22))
    if specs_slice:
        lbl_size = min(
            fit_font(draw, title.upper(), tw_specs, "label", lbl_cap).size
            for title, _ in specs_slice
        )
        val_size = min(
            _fit_lines_in_box(
                draw, value, tw_specs, int(row_h * 0.55), "value", val_cap, hard_max_lines=3,
            )[1].size
            for _, value in specs_slice
        )
        f_lbl_shared = get_font(lbl_size, "label")
    for i, (title, value) in enumerate(specs_slice):
        by    = body_y + i * (row_h + row_gap)
        cx    = mid_x + mid_w // 2
        tw    = tw_specs
        pad   = int(row_h * 0.12)

        lbl_y = by + int(row_h * 0.20)
        draw.text((cx, lbl_y), title.upper(), font=f_lbl_shared,
                  fill=accent if accent else gray_col, anchor="mm")

        text_start = lbl_y + f_lbl_shared.size // 2 + int(row_h * 0.10)
        text_avail = (by + row_h - pad) - text_start

        lines, f_val = _fit_fixed_size(draw, value, tw, text_avail, "value", val_size)
        lh = f_val.size + 4
        block_h = lh * len(lines)
        # Центрируем блок строк вертикально в доступной зоне
        start_y = text_start + (text_avail - block_h) // 2 + f_val.size // 2
        for j, line in enumerate(lines):
            draw.text((cx, start_y + j * lh), line, font=f_val, fill=val_col, anchor="mm")

    # ── Преимущества — фиксированный размер шрифта для ВСЕХ блоков ──
    # Чтобы шрифты у всех 3 блоков были одинаковые (а не подбирались под
    # длину каждого текста), считаем размер один раз для всей колонки.
    # desc-текст у 1-2 блоков — оставляем как было по кеглю (блоки 1/2 ок).
    tips_desc_size = max(13, int(tip_row_h * 0.10))
    # Слоган — базовый размер. hard_max_lines рассчитывается динамически из высоты
    # блока, а не фиксируется числом — иначе wrap_text обрезает текст раньше времени.
    tips_slogan_size = max(16, int(tip_row_h * 0.11))

    for i, (tip_title, tip_desc) in enumerate(tips[:n_tips]):
        by    = body_y + i * (tip_row_h + tip_gap)
        cx    = rt_x + rt_w // 2
        tw    = rt_w - int(rt_w * 0.06)
        pad   = int(tip_row_h * 0.06)

        if not tip_desc:
            # Слоган — wrap_text сам уменьшает шрифт пока текст не влезет.
            # НЕ используем _fit_lines_in_box: она добавляет «…» при переполнении.
            slogan_avail_h = tip_row_h - pad * 2
            # max_lines рассчитан по минимальному шрифту (9px) — гарантирует
            # что wrap_text найдёт размер, при котором весь текст помещается.
            _slogan_max_lines = max(2, slogan_avail_h // (9 + 4))
            lines, f_slogan = wrap_text(
                draw, tip_title, tw, "title", tips_slogan_size,
                max_lines=_slogan_max_lines,
            )
            lh = f_slogan.size + 4
            # Если строк всё равно больше — режем без «…»
            _fit = max(1, slogan_avail_h // lh)
            if len(lines) > _fit:
                lines = lines[:_fit]
            block_h = lh * len(lines)
            start_y = by + (tip_row_h - block_h) // 2 + f_slogan.size // 2
            for j, line in enumerate(lines):
                draw.text((cx, start_y + j * lh), line,
                          font=f_slogan, fill=text_col, anchor="mm")
            continue

        # Заголовок — вверху блока, отступ 15px от верха.
        f_ttl = fit_font(draw, tip_title.upper(), tw, "label", max(9, int(tip_row_h * 0.13)))
        ttl_y = by + 15 + f_ttl.size // 2
        draw.text((cx, ttl_y), tip_title.upper(), font=f_ttl, fill=gray_col, anchor="mm")

        # Описание — по центру оставшейся (нижней) области блока: симметрично,
        # с равными отступами сверху/снизу под заголовком.
        region_top = ttl_y + f_ttl.size // 2 + int(tip_row_h * 0.05)
        region_bot = by + tip_row_h - pad
        region_h   = region_bot - region_top

        lines, f_desc = _fit_fixed_size(
            draw, tip_desc, tw, region_h, "label", tips_desc_size,
        )
        lh = f_desc.size + 3
        block_text_h = lh * len(lines)
        start_y = region_top + (region_h - block_text_h) // 2 + f_desc.size // 2
        for j, line in enumerate(lines):
            draw.text((cx, start_y + j * lh),
                      line, font=f_desc, fill=val_col, anchor="mm")

    return img


def build(img_bytes: bytes, product: str,
          features: list[tuple[str, str]],
          tips: list[tuple[str, str]],
          accent: tuple[int, int, int] | None = None) -> bytes:
    img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    img = overlay(img, product, features, tips, accent=accent)
    from .logo import paste_brand_watermark
    bg_dark = accent is not None
    img = paste_brand_watermark(img, corner="top-left", bg_dark=bg_dark)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    return buf.getvalue()
