"""Определение доминирующего цвета товара после rembg и сравнение с
ожидаемым цветом (color_en). Локально, без LLM."""
import colorsys
import logging
import numpy as np
from PIL import Image

log = logging.getLogger(__name__)


# Диапазоны (H, S, L) в нормализованном виде [0..1].
# Откалиброваны по реальным сбоям логов 2026-06-02:
#   Racing Black RGB(115,126,156) → h≈0.62 s≈0.17 L≈0.53 → blue-grey «unknown»
#   Корпус фотографируется темнее/светлее; диапазоны чуть расширены.
_COLOR_RANGES: dict[str, list[tuple]] = {
    "red":    [((0.94, 1.00), (0.28, 1.00), (0.18, 0.78)),
               ((0.00, 0.05), (0.28, 1.00), (0.18, 0.78))],
    "orange": [((0.05, 0.12), (0.35, 1.00), (0.28, 0.78))],
    "yellow": [((0.11, 0.19), (0.28, 1.00), (0.38, 0.88))],
    "green":  [((0.20, 0.47), (0.18, 1.00), (0.13, 0.78))],
    # blue: захватывает blue-grey и светло-голубой (Star/Mist/Twilight/Sky Blue)
    # Расширили L вверх до 0.88 — очень светлые голубые (169,219,241 L≈0.80) ранее
    # классифицировались как white. s >= 0.08 чтобы не захватывать настоящий белый.
    "blue":   [((0.48, 0.70), (0.08, 1.00), (0.15, 0.88))],
    "navy":   [((0.54, 0.72), (0.20, 1.00), (0.08, 0.42))],
    # purple: расширили влево до 0.62 — Aurora/Violet дают h≈0.65-0.72
    "purple": [((0.62, 0.86), (0.15, 1.00), (0.13, 0.80))],
    "pink":   [((0.83, 0.98), (0.18, 1.00), (0.48, 0.96))],
    # white: расширили — белые телефоны дают L 0.72-1.0 с небольшой насыщенностью
    "white":  [((0.00, 1.00), (0.00, 0.22), (0.72, 1.00))],
    # black: L до 0.18 — выше (0.18-0.30) теперь grey/gray.
    "black":  [((0.00, 1.00), (0.00, 1.00), (0.00, 0.18))],
    # grey: подняли порог s до 0.20 — захватывает «titanium/graphite» с лёгким оттенком
    # grey/gray: L вниз до 0.18 — тёмно-серые (GraphiteGray L≈0.14) ранее
    # классифицировались как black (порог был 0.30).
    "grey":       [((0.00, 1.00), (0.00, 0.20), (0.18, 0.82))],
    "gray":       [((0.00, 1.00), (0.00, 0.20), (0.18, 0.82))],
    # light grey: только светлые оттенки (L > 0.48) — Pearl/Mist/Light Gray.
    # Тёмный Graphite (L≈0.25) сюда НЕ попадает.
    "light grey": [((0.00, 1.00), (0.00, 0.22), (0.48, 0.90))],
    "light gray": [((0.00, 1.00), (0.00, 0.22), (0.48, 0.90))],
    # silver: чистые нейтральные светлые
    "silver": [((0.00, 1.00), (0.00, 0.12), (0.52, 0.92))],
    "gold":   [((0.08, 0.20), (0.28, 1.00), (0.42, 0.82))],
    "beige":  [((0.06, 0.17), (0.08, 0.42), (0.62, 0.94))],
    "brown":  [((0.02, 0.13), (0.18, 1.00), (0.13, 0.52))],
}

# Умные алиасы — что считается совпадением при ожидаемом цвете.
# Добавили агрессивное алиасирование тёмных цветов (dark-family).
_ALIASES: dict[str, set[str]] = {
    "blue":       {"blue", "navy"},
    "navy":       {"blue", "navy"},
    "grey":       {"grey", "gray", "silver"},
    "gray":       {"grey", "gray", "silver"},
    "silver":     {"grey", "gray", "silver", "light grey", "light gray"},
    # light grey принимает только светлые нейтральные — dark grey/graphite отклоняется
    "light grey": {"light grey", "light gray", "silver", "white"},
    "light gray": {"light grey", "light gray", "silver", "white"},
    # тёмные семьи — на фото разные ракурсы/освещение дают соседние классы
    "black":  {"black", "grey", "gray", "navy"},  # очень тёмный grey/navy ≈ black
    "white":  {"white", "silver", "light grey", "light gray"},
}


def _luminance(rgb: tuple[int, int, int]) -> float:
    """Яркость 0..1 (L из HLS)."""
    _, l, _ = colorsys.rgb_to_hls(rgb[0]/255, rgb[1]/255, rgb[2]/255)
    return l


def dominant_rgb(rgba: Image.Image, sample_size: int = 200) -> tuple[int, int, int] | None:
    """Доминирующий цвет КОРПУСА товара (а не экрана).
    Квантование по бинам + мода: задняя крышка = большой однотонный бин,
    пёстрый экран размазан и проигрывает."""
    img = rgba.copy()
    img.thumbnail((sample_size, sample_size), Image.LANCZOS)
    arr = np.asarray(img.convert("RGBA"))
    mask = arr[..., 3] > 200
    visible = arr[mask][:, :3].astype(np.int32)
    if len(visible) < 50:
        return None
    step = 24
    q = visible // step
    keys = q[:, 0] * 100_000 + q[:, 1] * 1_000 + q[:, 2]
    uniq, counts = np.unique(keys, return_counts=True)
    top_key = uniq[int(np.argmax(counts))]
    body = visible[keys == top_key].mean(axis=0)
    return int(body[0]), int(body[1]), int(body[2])


def rgb_to_color_name(rgb: tuple[int, int, int]) -> str | None:
    """Возвращает имя цвета или None если не подходит."""
    r, g, b = rgb
    h, l, s = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
    for name, ranges in _COLOR_RANGES.items():
        for (h_lo, h_hi), (s_lo, s_hi), (l_lo, l_hi) in ranges:
            if h_lo <= h <= h_hi and s_lo <= s <= s_hi and l_lo <= l <= l_hi:
                return name
    return None


def color_matches(rgba: Image.Image, expected_en: str) -> tuple[bool, str | None, tuple | None]:
    """Проверяет доминирующий цвет корпуса товара vs ожидаемый.
    Возвращает (match, detected_name, rgb)."""
    if not expected_en:
        return True, None, None
    expected = expected_en.lower().strip()
    # Нормализуем вариации написания
    if expected == "light gray":
        expected = "light grey"
    if expected not in _COLOR_RANGES:
        return True, None, None
    rgb = dominant_rgb(rgba)
    if rgb is None:
        return True, None, None

    detected = rgb_to_color_name(rgb)

    # --- Умное алиасирование по яркости ---
    # Если цвет не классифицирован, применяем яркостные правила:
    # очень тёмное (L < 0.32) → считаем black/navy; очень светлое (L > 0.70) → white/silver
    if detected is None:
        lum = _luminance(rgb)
        if lum < 0.32:
            detected = "black"
            log.info(f"Color: RGB={rgb} L={lum:.2f} → dark fallback → black")
        elif lum > 0.70:
            detected = "white"
            log.info(f"Color: RGB={rgb} L={lum:.2f} → light fallback → white")
        else:
            log.info(f"Color check: unknown RGB={rgb} for expected={expected} — пропускаем")
            return True, None, rgb

    accept = _ALIASES.get(expected, {expected})
    match = detected in accept

    # ── Дополнительные яркостно-насыщенностные матчи ──────────────────────────
    if not match:
        _, lum, sat = colorsys.rgb_to_hls(rgb[0]/255, rgb[1]/255, rgb[2]/255)

        # black ↔ тёмный blue/grey: Racing Black h≈0.62 s≈0.17 L≈0.53
        if expected == "black" and detected in ("blue", "grey", "gray"):
            if lum < 0.58 and sat < 0.25:
                match = True
                log.info(f"Color: dark {detected} L={lum:.2f} S={sat:.2f} → black-family")

        # grey ↔ black: GraphiteGray (35,35,35) L=0.14 — очень тёмный серый
        # попадает в black-диапазон, но визуально это «тёмно-серый» корпус.
        elif expected in ("grey", "gray") and detected == "black":
            if lum > 0.10:  # исключаем чисто-чёрное (L < 0.10)
                match = True
                log.info(f"Color: dark black L={lum:.2f} → grey-family")

        # grey ↔ blue с низкой насыщенностью: Graphite h≈0.62 s≈0.10
        # слабый синий оттенок — фактически нейтральный серый.
        elif expected in ("grey", "gray") and detected == "blue":
            if sat < 0.15:
                match = True
                log.info(f"Color: low-sat blue S={sat:.2f} → grey-family")

        # light grey — принимаем только если фото действительно светлое (L > 0.45)
        elif expected == "light grey":
            if detected in ("grey", "gray", "silver", "white") and lum > 0.45:
                match = True
                log.info(f"Color: light {detected} L={lum:.2f} → light grey-family")
            elif detected == "blue" and sat < 0.15 and lum > 0.55:
                # Pearl/Ash/Mist — очень светлый нейтральный с холодным оттенком
                # попадает в blue-диапазон из-за низкой но ненулевой насыщенности
                match = True
                log.info(f"Color: cool-tinted light blue S={sat:.2f} L={lum:.2f} → light grey-family")
            else:
                match = False
                log.info(f"Color: {detected} L={lum:.2f} слишком тёмный/насыщенный для light grey — MISMATCH")

    log.info(f"Color check: detected={detected!r} expected={expected!r} → "
             f"{'MATCH' if match else 'MISMATCH'} (RGB={rgb})")
    return match, detected, rgb
