"""Локальная генерация фонов для карточек (дуотон-градиент + aurora + декор).
Полная замена Gemini: бесплатно, мгновенно, без API.

generate_background(w, h, palette_key) -> JPEG bytes.
"""
import io
import math
import random
from itertools import cycle

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

def _p(c1, c2, angle, acc1, acc2):
    return {"c1": c1, "c2": c2, "angle": angle, "acc1": acc1, "acc2": acc2}

PALETTES: dict[str, dict] = {
    # ── Холодные ───────────────────────────────────────────────────
    "arctic":      _p((188,222,255),(226,214,252), 40,(150,200,255),(205,185,250)),
    "ocean":       _p((176,232,246),(192,210,250), 50,(120,205,235),(150,175,245)),
    "mint_sky":    _p((200,246,226),(202,228,255), 45,(150,235,205),(165,205,252)),
    "aqua_lilac":  _p((184,240,238),(224,212,250), 55,(110,222,224),(200,180,248)),
    "teal_blue":   _p((176,235,230),(186,214,248), 48,(90,205,200),(130,170,240)),
    "ice_mint":    _p((210,248,240),(220,238,255), 42,(140,230,220),(160,195,255)),
    "deep_sky":    _p((170,215,255),(200,200,250), 52,(100,185,255),(170,155,245)),
    # ── Тёплые ──────────────────────────────────────────────────────
    "peach_pink":  _p((255,214,184),(255,204,226), 40,(255,180,150),(255,175,210)),
    "coral_rose":  _p((255,184,168),(255,202,222), 35,(255,150,130),(250,165,200)),
    "sunset":      _p((255,206,150),(255,188,206), 60,(255,185,120),(250,150,175)),
    "apricot":     _p((255,224,182),(255,208,198), 45,(255,195,130),(252,175,165)),
    "lemon_mint":  _p((250,242,192),(204,244,220), 50,(240,230,130),(150,230,190)),
    "warm_gold":   _p((255,220,160),(255,200,185), 55,(255,190,110),(250,165,145)),
    "tangerine":   _p((255,195,155),(255,210,200), 38,(255,160,100),(255,180,165)),
    # ── Сиренево-розовые ────────────────────────────────────────────
    "lavender":    _p((222,206,250),(246,222,246), 55,(190,165,248),(250,190,230)),
    "violet_cyan": _p((212,200,250),(190,236,246), 50,(150,120,245),(90,210,235)),
    "iris":        _p((206,206,250),(232,206,248), 52,(150,150,248),(215,160,240)),
    "magenta":     _p((242,200,242),(250,210,226), 60,(235,140,230),(250,155,195)),
    "blush":       _p((252,210,225),(248,225,242), 45,(248,175,200),(235,185,240)),
    "lilac_rose":  _p((235,205,245),(255,215,235), 58,(210,160,240),(255,175,215)),
    # ── Зелёные / земляные ──────────────────────────────────────────
    "sage":        _p((216,232,200),(242,246,224), 42,(175,210,150),(230,225,175)),
    "emerald":     _p((188,236,206),(226,246,226), 45,(110,210,160),(180,230,180)),
    "olive_cream": _p((228,232,196),(248,244,226), 40,(200,210,140),(240,220,180)),
    "forest_mist": _p((190,228,205),(220,240,218), 44,(130,200,165),(180,225,185)),
    # ── Нейтраль + акцент ───────────────────────────────────────────
    "cloud_blue":  _p((228,233,242),(247,249,253), 45,(140,175,255),(185,205,250)),
    "sand":        _p((242,230,208),(250,244,232), 40,(255,185,140),(235,205,160)),
    "graphite":    _p((210,218,230),(240,244,250), 45,(120,160,250),(170,185,235)),
    "smoke":       _p((220,224,232),(242,244,248), 50,(145,165,210),(190,195,225)),
    # ── Премиум ─────────────────────────────────────────────────────
    "champagne":   _p((246,226,182),(252,244,224), 50,(235,200,135),(245,225,195)),
    "rose_gold":   _p((250,222,210),(252,236,230), 48,(240,175,165),(248,210,190)),
    "pearl":       _p((245,238,225),(252,248,240), 46,(235,215,185),(248,235,215)),
    # ── Игровое / тех-неон ──────────────────────────────────────────
    "cyber":       _p((208,216,252),(226,210,250), 50,(110,140,255),(180,110,250)),
    "neon_aqua":   _p((200,240,246),(212,222,250), 48,(70,218,235),(140,130,252)),
    "electric":    _p((205,215,255),(215,205,250), 52,(80,120,255),(150,90,250)),
}

_rotation = cycle(list(PALETTES.keys()))


def next_palette_key() -> str:
    """Следующий ключ ротации — когда палитру нужно знать ЗАРАНЕЕ
    (одна и та же на несколько слайдов одной карточки)."""
    return next(_rotation)


# ── Базовые примитивы ────────────────────────────────────────────────────────

def _linear_gradient(w: int, h: int, c1, c2, angle_deg: float) -> Image.Image:
    ang = math.radians(angle_deg)
    xx, yy = np.meshgrid(np.linspace(0, 1, w), np.linspace(0, 1, h))
    t = xx * math.cos(ang) + yy * math.sin(ang)
    t = (t - t.min()) / (t.max() - t.min() + 1e-9)
    arr = np.empty((h, w, 3), dtype=np.float32)
    for i in range(3):
        arr[..., i] = c1[i] + (c2[i] - c1[i]) * t
    return Image.fromarray(np.clip(arr, 0, 255).astype("uint8"), "RGB")


def _blob(img: Image.Image, cx: int, cy: int, radius: int, color, alpha: int) -> Image.Image:
    ov = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(ov).ellipse([cx-radius, cy-radius, cx+radius, cy+radius],
                               fill=(*color, alpha))
    ov = ov.filter(ImageFilter.GaussianBlur(radius // 2))
    return Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB")


def _overlay(img: Image.Image, ov: Image.Image, blur: int = 0) -> Image.Image:
    if blur > 0:
        ov = ov.filter(ImageFilter.GaussianBlur(blur))
    return Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB")


# ── Aurora mesh — главный «живой» элемент ────────────────────────────────────

def _aurora(img: Image.Image, acc1, acc2, rnd: random.Random) -> Image.Image:
    """4–6 цветных пятен в разных зонах + белый глянцевый блик.
    Alpha увеличена для заметности; меньше размытие — пятна читаются."""
    w, h = img.size
    blend1 = tuple(int(acc1[i] * 0.6 + acc2[i] * 0.4) for i in range(3))
    blend2 = tuple(int(acc1[i] * 0.3 + acc2[i] * 0.7) for i in range(3))
    colors = [acc1, acc2, blend1, blend2, acc1, acc2]
    rnd.shuffle(colors)
    zones = [
        (rnd.uniform(0.02, 0.28), rnd.uniform(0.02, 0.30)),
        (rnd.uniform(0.72, 0.98), rnd.uniform(0.70, 0.98)),
        (rnd.uniform(0.55, 0.95), rnd.uniform(0.02, 0.28)),
        (rnd.uniform(0.02, 0.32), rnd.uniform(0.60, 0.96)),
        (rnd.uniform(0.30, 0.70), rnd.uniform(0.30, 0.70)),
        (rnd.uniform(0.15, 0.85), rnd.uniform(0.15, 0.85)),
    ]
    rnd.shuffle(zones)
    n = rnd.randint(4, 6)
    for i in range(n):
        fx, fy = zones[i]
        r = int(min(w, h) * rnd.uniform(0.35, 0.60))
        img = _blob(img, int(w * fx), int(h * fy), r, colors[i], rnd.randint(110, 160))
    # глянцевый блик
    img = _blob(img, int(w * rnd.uniform(0.5, 0.92)), int(h * rnd.uniform(0.03, 0.20)),
                int(min(w, h) * rnd.uniform(0.20, 0.35)), (255, 255, 255), rnd.randint(70, 100))
    return img


# ── Декор-функции ─────────────────────────────────────────────────────────────

def _bubbles(img: Image.Image, rnd: random.Random, accents=None) -> Image.Image:
    """Чёткие окружности-контуры разного размера (2× supersample)."""
    w, h = img.size
    ss = 2
    ov = Image.new("RGBA", (w*ss, h*ss), (0,0,0,0))
    d = ImageDraw.Draw(ov)
    lw = max(3, (w*ss) // 280)
    for _ in range(rnd.randint(6, 12)):
        r = rnd.randint(int(min(w,h)*0.05), int(min(w,h)*0.28)) * ss
        x, y = rnd.randint(0, w*ss), rnd.randint(0, h*ss)
        col = rnd.choice(accents) if (accents and rnd.random() < 0.55) else (255,255,255)
        d.ellipse([x-r, y-r, x+r, y+r], outline=(*col, rnd.randint(75, 130)), width=lw)
    return _overlay(img, ov.resize((w, h), Image.LANCZOS))


def _dots(img: Image.Image, rnd: random.Random, accents=None) -> Image.Image:
    """Чёткие круглые точки разного размера (2× supersample)."""
    w, h = img.size
    ss = 2
    ov = Image.new("RGBA", (w*ss, h*ss), (0,0,0,0))
    d = ImageDraw.Draw(ov)
    for _ in range(rnd.randint(10, 18)):
        r = rnd.randint(int(min(w,h)*0.02), int(min(w,h)*0.09)) * ss
        x, y = rnd.randint(0, w*ss), rnd.randint(0, h*ss)
        col = rnd.choice(accents) if (accents and rnd.random() < 0.5) else (255,255,255)
        d.ellipse([x-r, y-r, x+r, y+r], fill=(*col, rnd.randint(45, 85)))
    return _overlay(img, ov.resize((w, h), Image.LANCZOS))


def _rings(img: Image.Image, rnd: random.Random, accents=None) -> Image.Image:
    """Концентрические кольца из угла."""
    w, h = img.size
    ov = Image.new("RGBA", (w,h), (0,0,0,0))
    d = ImageDraw.Draw(ov)
    cx, cy = rnd.choice([(int(w*0.08),int(h*0.10)),(int(w*0.92),int(h*0.88)),(int(w*0.92),int(h*0.10))])
    col = rnd.choice(accents) if (accents and rnd.random() < 0.5) else (255,255,255)
    base = int(min(w,h) * rnd.uniform(0.14, 0.22))
    for i in range(rnd.randint(4, 7)):
        r = base * (i + 1)
        d.ellipse([cx-r, cy-r, cx+r, cy+r], outline=(*col, rnd.randint(40, 70)),
                  width=max(3, w//200))
    return _overlay(img, ov, blur=1)


def _waves(img: Image.Image, rnd: random.Random, accents=None) -> Image.Image:
    """3–4 изогнутые волны через весь холст."""
    w, h = img.size
    ov = Image.new("RGBA", (w,h), (0,0,0,0))
    d = ImageDraw.Draw(ov)
    for k in range(rnd.randint(3, 4)):
        col = rnd.choice(accents) if (accents and rnd.random() < 0.5) else (255,255,255)
        base = int(h * (0.18 + 0.22*k)) + rnd.randint(-40, 40)
        amp = rnd.randint(int(h*0.05), int(h*0.14))
        freq = rnd.uniform(1.2, 2.8)
        pts = [(x, base + int(amp * math.sin(x/w * math.pi * freq + k*1.2)))
               for x in range(0, w+20, 12)]
        d.line(pts, fill=(*col, rnd.randint(45, 70)), width=max(10, w//95), joint="curve")
    return _overlay(img, ov, blur=4)


def _web(img: Image.Image, rnd: random.Random, accents=None) -> Image.Image:
    """Паутинная сеть — узлы + соединяющие линии (constellation)."""
    w, h = img.size
    ov = Image.new("RGBA", (w,h), (0,0,0,0))
    d = ImageDraw.Draw(ov)
    col = rnd.choice(accents) if (accents and rnd.random() < 0.5) else (255,255,255)
    n = rnd.randint(12, 20)
    pts = [(rnd.randint(0,w), rnd.randint(0,h)) for _ in range(n)]
    thr = min(w,h) * rnd.uniform(0.30, 0.48)
    for i in range(n):
        for j in range(i+1, n):
            dist = math.hypot(pts[i][0]-pts[j][0], pts[i][1]-pts[j][1])
            if dist < thr:
                a = int(55 * (1 - dist/thr)) + 15
                d.line([pts[i], pts[j]], fill=(*col, a), width=max(1, w//700))
    nr = max(4, w//220)
    for (x, y) in pts:
        d.ellipse([x-nr, y-nr, x+nr, y+nr], fill=(*col, 100))
    return _overlay(img, ov, blur=1)


def _lines(img: Image.Image, rnd: random.Random, accents=None) -> Image.Image:
    """2–4 случайные линии под произвольным углом."""
    w, h = img.size
    ov = Image.new("RGBA", (w,h), (0,0,0,0))
    d = ImageDraw.Draw(ov)
    for _ in range(rnd.randint(2, 4)):
        col = rnd.choice(accents) if (accents and rnd.random() < 0.4) else (255,255,255)
        x1, y1 = rnd.randint(0,w), rnd.randint(0,h)
        ang = rnd.uniform(0, math.pi)
        length = int(max(w,h) * rnd.uniform(0.5, 1.2))
        x2, y2 = int(x1+math.cos(ang)*length), int(y1+math.sin(ang)*length)
        d.line([(x1,y1),(x2,y2)], fill=(*col, rnd.randint(20,42)), width=max(2, w//210))
    return _overlay(img, ov, blur=3)


def _hexagons(img: Image.Image, rnd: random.Random, accents=None) -> Image.Image:
    """Рассыпанные шестиугольники — геометричный tech-стиль."""
    w, h = img.size
    ss = 2
    ov = Image.new("RGBA", (w*ss, h*ss), (0,0,0,0))
    d = ImageDraw.Draw(ov)
    for _ in range(rnd.randint(4, 8)):
        col = rnd.choice(accents) if (accents and rnd.random() < 0.5) else (255,255,255)
        r = rnd.randint(int(min(w,h)*0.06), int(min(w,h)*0.20)) * ss
        cx, cy = rnd.randint(0, w*ss), rnd.randint(0, h*ss)
        pts = [(int(cx + r*math.cos(math.pi/6 + math.pi/3*i)),
                int(cy + r*math.sin(math.pi/6 + math.pi/3*i))) for i in range(6)]
        d.polygon(pts, outline=(*col, rnd.randint(45, 80)),
                  fill=(*col, rnd.randint(0, 18)))
    return _overlay(img, ov.resize((w, h), Image.LANCZOS))


def _triangles(img: Image.Image, rnd: random.Random, accents=None) -> Image.Image:
    """Несколько треугольников — low-poly / geom aesthetic."""
    w, h = img.size
    ss = 2
    ov = Image.new("RGBA", (w*ss, h*ss), (0,0,0,0))
    d = ImageDraw.Draw(ov)
    for _ in range(rnd.randint(3, 7)):
        col = rnd.choice(accents) if (accents and rnd.random() < 0.45) else (255,255,255)
        cx, cy = rnd.randint(0, w*ss), rnd.randint(0, h*ss)
        r = rnd.randint(int(min(w,h)*0.06), int(min(w,h)*0.22)) * ss
        angle = rnd.uniform(0, math.pi*2)
        pts = [(int(cx + r*math.cos(angle + math.pi*2/3*i)),
                int(cy + r*math.sin(angle + math.pi*2/3*i))) for i in range(3)]
        d.polygon(pts, outline=(*col, rnd.randint(40, 75)),
                  fill=(*col, rnd.randint(0, 22)))
    return _overlay(img, ov.resize((w, h), Image.LANCZOS))


def _cross_hatch(img: Image.Image, rnd: random.Random, accents=None) -> Image.Image:
    """Лёгкий cross-hatch (две диагональные сетки) — текстурный акцент."""
    w, h = img.size
    ov = Image.new("RGBA", (w,h), (0,0,0,0))
    d = ImageDraw.Draw(ov)
    col = rnd.choice(accents) if (accents and rnd.random() < 0.35) else (255,255,255)
    step = int(min(w,h) * rnd.uniform(0.07, 0.11))
    alpha = rnd.randint(12, 22)
    for x in range(-h, w+h, step):
        d.line([(x,0),(x+h,h)], fill=(*col, alpha), width=1)
    for x in range(-h, w+h, step):
        d.line([(x,h),(x+h,0)], fill=(*col, alpha), width=1)
    return _overlay(img, ov, blur=1)


def _neon_glow(img: Image.Image, rnd: random.Random, accents=None) -> Image.Image:
    """Мягкое неоновое свечение — 2–3 ярких размытых пятна акцентного цвета."""
    w, h = img.size
    for _ in range(rnd.randint(2, 3)):
        col = rnd.choice(accents) if accents else (200, 220, 255)
        cx = rnd.randint(int(w*0.1), int(w*0.9))
        cy = rnd.randint(int(h*0.1), int(h*0.9))
        r = int(min(w,h) * rnd.uniform(0.15, 0.32))
        img = _blob(img, cx, cy, r, col, rnd.randint(80, 130))
    return img


def _glitter(img: Image.Image, rnd: random.Random, accents=None) -> Image.Image:
    """Мелкий блеск — много мелких точек врассыпную (как глиттер)."""
    w, h = img.size
    ov = Image.new("RGBA", (w,h), (0,0,0,0))
    d = ImageDraw.Draw(ov)
    for _ in range(rnd.randint(40, 80)):
        x, y = rnd.randint(0,w), rnd.randint(0,h)
        r = rnd.randint(1, max(2, w//280))
        col = rnd.choice(accents) if (accents and rnd.random() < 0.4) else (255,255,255)
        d.ellipse([x-r, y-r, x+r, y+r], fill=(*col, rnd.randint(60, 130)))
    return _overlay(img, ov, blur=0)


def _sparkles(img: Image.Image, rnd: random.Random, accents=None) -> Image.Image:
    """Звёздочки-блики: 4-точечные кресты с лучами (×2 supersample)."""
    w, h = img.size
    ss = 2
    ov = Image.new("RGBA", (w*ss, h*ss), (0,0,0,0))
    d = ImageDraw.Draw(ov)
    for _ in range(rnd.randint(6, 14)):
        col = rnd.choice(accents) if (accents and rnd.random() < 0.5) else (255,255,255)
        cx, cy = rnd.randint(0, w*ss), rnd.randint(0, h*ss)
        arm = rnd.randint(int(min(w,h)*0.04), int(min(w,h)*0.13)) * ss
        a = rnd.randint(80, 140)
        lw = max(2, arm // 6)
        # основные лучи
        d.line([(cx-arm, cy), (cx+arm, cy)], fill=(*col, a), width=lw)
        d.line([(cx, cy-arm), (cx, cy+arm)], fill=(*col, a), width=lw)
        # диагональные лучи (короче)
        s = int(arm * 0.55)
        d.line([(cx-s, cy-s), (cx+s, cy+s)], fill=(*col, a//2), width=max(1, lw//2))
        d.line([(cx+s, cy-s), (cx-s, cy+s)], fill=(*col, a//2), width=max(1, lw//2))
        # центральная точка
        r = max(2, lw)
        d.ellipse([cx-r, cy-r, cx+r, cy+r], fill=(*col, 200))
    return _overlay(img, ov.resize((w, h), Image.LANCZOS))


def _swoosh(img: Image.Image, rnd: random.Random, accents=None) -> Image.Image:
    """Одна-две широкие плавные полосы-мазка через холст — как акварельный штрих."""
    w, h = img.size
    ov = Image.new("RGBA", (w,h), (0,0,0,0))
    d = ImageDraw.Draw(ov)
    for _ in range(rnd.randint(1, 2)):
        col = rnd.choice(accents) if (accents and rnd.random() < 0.6) else (255,255,255)
        # Начало и конец полосы — разные края холста
        side = rnd.randint(0, 3)
        if side == 0:   sx, sy = rnd.randint(0, w//2), 0;         ex, ey = rnd.randint(w//2, w), h
        elif side == 1: sx, sy = w, rnd.randint(0, h//2);         ex, ey = 0, rnd.randint(h//2, h)
        elif side == 2: sx, sy = rnd.randint(0, w), 0;            ex, ey = rnd.randint(0, w), h
        else:           sx, sy = 0, rnd.randint(0, h//2);         ex, ey = w, rnd.randint(h//2, h)
        # Контрольная точка для кривой
        mx = (sx+ex)//2 + rnd.randint(-w//4, w//4)
        my = (sy+ey)//2 + rnd.randint(-h//4, h//4)
        # Сплайн через промежуточные точки
        steps = 40
        pts = []
        for t_i in range(steps+1):
            t = t_i / steps
            bx = int((1-t)**2 * sx + 2*(1-t)*t * mx + t**2 * ex)
            by = int((1-t)**2 * sy + 2*(1-t)*t * my + t**2 * ey)
            pts.append((bx, by))
        width = rnd.randint(max(20, w//20), max(40, w//9))
        a = rnd.randint(30, 55)
        d.line(pts, fill=(*col, a), width=width, joint="curve")
    return _overlay(img, ov, blur=12)


def _dot_grid(img: Image.Image, rnd: random.Random, alpha: int = 35) -> Image.Image:
    """Регулярная точечная сетка (polka)."""
    w, h = img.size
    ov = Image.new("RGBA", (w,h), (0,0,0,0))
    d = ImageDraw.Draw(ov)
    step = int(min(w,h) * rnd.uniform(0.08, 0.13))
    r = max(2, step // 11)
    off = rnd.randint(0, step)
    for y in range(off, h, step):
        for x in range(off, w, step):
            d.ellipse([x-r, y-r, x+r, y+r], fill=(255,255,255,alpha))
    return _overlay(img, ov, blur=1)


def _arcs(img: Image.Image, rnd: random.Random, accents=None) -> Image.Image:
    """Крупные дуги из угла."""
    w, h = img.size
    ov = Image.new("RGBA", (w,h), (0,0,0,0))
    d = ImageDraw.Draw(ov)
    col = rnd.choice(accents) if (accents and rnd.random() < 0.4) else (255,255,255)
    cx, cy = rnd.choice([(0,0),(w,0),(0,h),(w,h)])
    base = int(min(w,h) * rnd.uniform(0.35, 0.50))
    for i in range(rnd.randint(2, 4)):
        r = base + i * int(min(w,h) * rnd.uniform(0.18, 0.25))
        d.arc([cx-r, cy-r, cx+r, cy+r], 0, 360, fill=(*col, rnd.randint(20,38)),
              width=max(3, w//210))
    return _overlay(img, ov, blur=2)


def _confetti(img: Image.Image, rnd: random.Random, accents=None) -> Image.Image:
    """Мелкие разнобразные точки и штрихи врассыпную."""
    w, h = img.size
    ov = Image.new("RGBA", (w,h), (0,0,0,0))
    d = ImageDraw.Draw(ov)
    for _ in range(rnd.randint(20, 35)):
        x, y = rnd.randint(0,w), rnd.randint(0,h)
        s = rnd.randint(int(min(w,h)*0.008), int(min(w,h)*0.025))
        col = rnd.choice(accents) if (accents and rnd.random() < 0.4) else (255,255,255)
        t = rnd.random()
        if t < 0.4:
            d.ellipse([x,y,x+s,y+s], fill=(*col, rnd.randint(40,70)))
        elif t < 0.7:
            d.line([(x,y),(x+s*2,y+s)], fill=(*col, rnd.randint(35,60)),
                   width=max(2, w//320))
        else:
            d.rectangle([x,y,x+s,y+s], outline=(*col, rnd.randint(40,65)))
    return _overlay(img, ov, blur=1)


# ── Реестр и генератор ────────────────────────────────────────────────────────

# Весовая таблица. Формат: (имя, вес, нужны ли accents)
_DECOR_REGISTRY = [
    ("bubbles",    3, True),
    ("rings",      3, True),
    ("waves",      3, True),
    ("swoosh",     2, True),
    ("dots",       2, True),
    ("hexagons",   2, True),
    ("triangles",  2, True),
    ("web",        2, True),
    ("neon_glow",  2, True),
    ("arcs",       2, True),
    ("glitter",    1, True),
    ("confetti",   1, True),
    ("cross_hatch",1, True),
    ("dot_grid",   1, False),
]

_DECOR_FNS = {
    "bubbles":    _bubbles,
    "rings":      _rings,
    "dots":       _dots,
    "hexagons":   _hexagons,
    "triangles":  _triangles,
    "waves":      _waves,
    "web":        _web,
    "neon_glow":  _neon_glow,
    "lines":      _lines,
    "arcs":       _arcs,
    "glitter":    _glitter,
    "cross_hatch":_cross_hatch,
    "confetti":   _confetti,
    "dot_grid":   _dot_grid,
    "sparkles":   _sparkles,
    "swoosh":     _swoosh,
}

_DECOR_POOL = [name for name, w, _ in _DECOR_REGISTRY for _ in range(w)]
_DECOR_NEEDS_ACCENT = {name: needs for name, _, needs in _DECOR_REGISTRY}

# Совместимые пары декоров — хорошо выглядят вместе
_DECOR_PAIRS: list[tuple[str, str]] = [
    ("bubbles",   "glitter"),
    ("bubbles",   "glitter"),
    ("rings",     "web"),
    ("rings",     "dots"),
    ("waves",     "dots"),
    ("waves",     "swoosh"),
    ("hexagons",  "neon_glow"),
    ("hexagons",  "dots"),
    ("triangles", "cross_hatch"),
    ("triangles", "dots"),
    ("swoosh",    "dots"),
    ("arcs",      "bubbles"),
    ("web",       "glitter"),
    ("confetti",  "swoosh"),
]


def _apply_decor(img, name: str, rnd, p) -> Image.Image:
    fn = _DECOR_FNS[name]
    if _DECOR_NEEDS_ACCENT[name]:
        return fn(img, rnd, accents=(p["acc1"], p["acc2"]))
    return fn(img, rnd)


def generate_background(width: int, height: int, palette_key: str | None = None) -> bytes:
    """JPEG-байты фона: дуотон-градиент + aurora-mesh + 1–2 слоя декора."""
    if palette_key not in PALETTES:
        palette_key = next(_rotation)
    p = PALETTES[palette_key]
    rnd = random.Random()

    img = _linear_gradient(width, height, p["c1"], p["c2"], p["angle"])
    img = _aurora(img, p["acc1"], p["acc2"], rnd)

    # Декор: всегда минимум 1 слой; 40% — второй из совместимой пары
    name1 = rnd.choice(_DECOR_POOL)
    img = _apply_decor(img, name1, rnd, p)
    log_names = [name1]

    if rnd.random() < 0.40:
        pairs = [b for a, b in _DECOR_PAIRS if a == name1] + \
                [a for a, b in _DECOR_PAIRS if b == name1]
        name2 = rnd.choice(pairs) if pairs else rnd.choice(
            [n for n in _DECOR_POOL if n != name1])
        img = _apply_decor(img, name2, rnd, p)
        log_names.append(name2)

    import logging
    logging.getLogger(__name__).debug(f"BG decor: {' + '.join(log_names)}")

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    return buf.getvalue()
