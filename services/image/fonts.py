import contextvars
import os
import random as _random
from PIL import ImageFont, ImageDraw

# ── /rarity: пул случайных шрифтов (17.07.2026) ────────────────────────────
# Активен ТОЛЬКО внутри одного запуска /rarity через contextvars (см.
# set_rarity_family/reset_rarity_family) — на /image, /batch и остальные
# команды не влияет, для них _rarity_family всегда None и get_font()
# работает как раньше.
RARITY_FONT_DIR = "C:/AI-Bot-V2/data/fonts/rarity/"

RARITY_FAMILIES: dict[str, dict[str, str]] = {
    # Полные семьи с разными насыщенностями — годятся для всех трёх ролей.
    "akrobat": {
        "title": RARITY_FONT_DIR + "Akrobat-ExtraBold.otf",
        "label": RARITY_FONT_DIR + "Akrobat-SemiBold.otf",
        "value": RARITY_FONT_DIR + "Akrobat-Regular.otf",
    },
    "bebas": {
        "title": RARITY_FONT_DIR + "BebasNeueBold.otf",
        "label": RARITY_FONT_DIR + "BebasNeueRegular.otf",
        "value": RARITY_FONT_DIR + "BebasNeueLight.otf",
    },
    "involve": {
        "title": RARITY_FONT_DIR + "Involve-Bold.ttf",
        "label": RARITY_FONT_DIR + "Involve-SemiBold.ttf",
        "value": RARITY_FONT_DIR + "Involve-Regular.ttf",
    },
    "kelson": {
        "title": RARITY_FONT_DIR + "KelsonSansBold.otf",
        "label": RARITY_FONT_DIR + "KelsonSansRegular.otf",
        "value": RARITY_FONT_DIR + "KelsonSansLight.otf",
    },
    "findsans": {
        "title": RARITY_FONT_DIR + "FindSansPro-Bold.ttf",
        "label": RARITY_FONT_DIR + "FindSansPro-Medium.ttf",
        "value": RARITY_FONT_DIR + "FindSansPro-Regular.ttf",
    },
    # Однонасыщенные акцентные/декоративные шрифты — мелким кеглем нечитаемы,
    # используются только для заголовка; label/value падают на штатный пул.
    "razluka":   {"title": RARITY_FONT_DIR + "RazlukaSP-Bold.otf"},
    "anarchy":   {"title": RARITY_FONT_DIR + "Anarchy_Normal.ttf"},
    "gothic60":  {"title": RARITY_FONT_DIR + "Gothic60-Regular.otf"},
    "lack":      {"title": RARITY_FONT_DIR + "Lack.otf"},
    "monofonto": {"title": RARITY_FONT_DIR + "MONOFONTO.TTF"},
    "quazi":     {"title": RARITY_FONT_DIR + "quazi_mode.ttf"},
    "tauru":     {"title": RARITY_FONT_DIR + "TAURU.TTF"},
}

_rarity_family: "contextvars.ContextVar[str | None]" = contextvars.ContextVar(
    "rarity_family", default=None
)


# Тематические группы шрифтов — подбор по категории товара (17.07.2026),
# независимо от рандомного gaming/simple визуального стиля.
FONT_MOODS: dict[str, list[str]] = {
    # Агрессивные/техно — явно игровые и «железные» категории.
    "gaming_aggressive": ["anarchy", "gothic60", "monofonto", "razluka", "tauru"],
    # Крупный ударный — универсальный ритейл-акцент (гаджеты, электроника).
    "impact_retail": ["bebas", "akrobat", "quazi"],
    # Спокойные деловые — расходники, комплектующие, офисное/премиум.
    "clean_premium": ["involve", "kelson", "findsans", "lack"],
}

CATEGORY_FONT_MOOD: dict[str, str] = {
    "Видеокарты": "gaming_aggressive",
    "Мыши": "gaming_aggressive",
    "Клавиатуры": "gaming_aggressive",
    "Игровые кресла": "gaming_aggressive",
    "Наушники": "gaming_aggressive",
    "Оперативная память": "gaming_aggressive",
    "Охлаждение": "gaming_aggressive",
    "Охлаждение корпуса": "gaming_aggressive",
    "Материнские платы": "gaming_aggressive",
    "Процессоры": "gaming_aggressive",
    "Корпуса для ПК": "gaming_aggressive",

    "Смартфоны": "impact_retail",
    "Планшеты": "impact_retail",
    "Ноутбуки": "impact_retail",
    "Моноблоки": "impact_retail",
    "Мониторы": "impact_retail",
    "Компьютеры": "impact_retail",
    "Смарт-часы": "impact_retail",
    "Акустика": "impact_retail",
    "Кроссовки": "impact_retail",
    "Камеры видеонаблюдения": "impact_retail",
    "Графические планшеты": "impact_retail",

    "Принтеры": "clean_premium",
    "Картриджи для принтеров": "clean_premium",
    "Внешние жёсткие диски": "clean_premium",
    "SSD накопители": "clean_premium",
    "Зарядные устройства и блоки питания": "clean_premium",
    "Кабели и аксессуары": "clean_premium",
    "Коврики для мыши": "clean_premium",
    "Комплект клавиатура и мышь": "clean_premium",
    "Адаптеры": "clean_premium",
    "Блоки питания": "clean_premium",
    "Кронштейны для мониторов": "clean_premium",
    "Сетевое оборудование": "clean_premium",
    "Модемы": "clean_premium",
}


def pick_random_rarity_family(category: str = "") -> str:
    """Категория известна → тянем из тематической группы (см. CATEGORY_FONT_MOOD),
    иначе (категория не определена) — из всего пула без разбора."""
    mood = CATEGORY_FONT_MOOD.get(category)
    pool = FONT_MOODS.get(mood, list(RARITY_FAMILIES)) if mood else list(RARITY_FAMILIES)
    return _random.choice(pool)


def set_rarity_family(family_key: str | None):
    """Включает шрифтовую семью для всех get_font() вызовов текущей async
    задачи (contextvars не утекают в другие параллельные задачи/пользователей).
    Возвращает token — передать в reset_rarity_family() по завершении."""
    return _rarity_family.set(family_key)


def reset_rarity_family(token) -> None:
    _rarity_family.reset(token)


_FONT_TITLE = [
    "C:/AI-Bot/Montserrat-Bold.ttf",
    "C:/AI-Bot/Montserrat-SemiBold.ttf",
    "C:/Windows/Fonts/bahnschrift.ttf",
    "C:/Windows/Fonts/segoeuib.ttf",
    "C:/Windows/Fonts/calibrib.ttf",
]
_FONT_VALUE = [
    "C:/Windows/Fonts/bahnschrift.ttf",
    "C:/AI-Bot/Montserrat-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
]
_FONT_LABEL = [
    "C:/AI-Bot/Montserrat-SemiBold.ttf",
    "C:/Windows/Fonts/segoeuib.ttf",
    "C:/Windows/Fonts/calibrib.ttf",
]

_ROLE_PATHS = {"title": _FONT_TITLE, "label": _FONT_LABEL}


def get_font(size: int, role: str = "value") -> ImageFont.ImageFont:
    fam_key = _rarity_family.get()
    if fam_key:
        fam = RARITY_FAMILIES.get(fam_key, {})
        # Раньше `fam.get(role) or fam.get("value") or fam.get("title")` — у
        # семей с только "title" (акцентные/декоративные) это давало ТОТ ЖЕ
        # декоративный шрифт заголовка для label/value, хотя комментарий у
        # RARITY_FAMILIES прямо говорит "label/value падают на штатный пул".
        # У gaming_aggressive все шрифты в пуле акцентные — лейблы
        # характеристик гарантированно рендерились нечитаемым декоративным
        # шрифтом на каждом gaming-слайде. Строгий поиск по роли — если нет,
        # идём в штатный пул ниже, а не переиспользуем чужую роль той же семьи.
        fp = fam.get(role)
        if fp and os.path.exists(fp):
            try:
                return ImageFont.truetype(fp, size)
            except Exception:
                pass
    paths = _ROLE_PATHS.get(role, _FONT_VALUE)
    for fp in paths:
        if os.path.exists(fp):
            try:
                return ImageFont.truetype(fp, size)
            except Exception:
                continue
    return ImageFont.load_default()


def fit_font(draw: ImageDraw.ImageDraw, text: str, max_width: int,
             role: str, max_size: int) -> ImageFont.ImageFont:
    """Подбирает максимальный размер шрифта при котором текст влезает в max_width."""
    for size in range(max_size, 10, -2):
        font = get_font(size, role)
        try:
            w = draw.textlength(text, font=font) * 1.05
        except Exception:
            w = len(text) * size * 0.65
        if w <= max_width:
            return font
    return get_font(10, role)


def _ref_line_height(draw: ImageDraw.ImageDraw, size: int, role: str) -> int:
    font = get_font(size, role)
    bbox = draw.textbbox((0, 0), "Ay", font=font)
    return bbox[3] - bbox[1]


def best_lines(draw: ImageDraw.ImageDraw, text: str, max_width: int,
               role: str, max_size: int,
               max_height: int | None = None, max_lines: int = 2) -> tuple[list[str], int]:
    """До max_lines строк — максимальный шрифт, который влезает по ширине
    в каждой строке и (если передан max_height) по суммарной высоте всех
    строк + межстрочные отступы.

    Раньше высота вообще не проверялась — max_size (потолок, заданный
    вызывающим кодом как доля высоты зоны) был единственной, приблизительной
    защитой от переполнения по вертикали. Для длинных характеристик (не
    помещаются в 2 строки без сильного ужатия) добавлена поддержка 3 строк —
    тот же текст на 3 более коротких строках даёт заметно больший шрифт, чем
    на 2 длинных (проверено на реальном тексте характеристики /rarity: 21
    vs 28)."""
    words = text.split()

    def _fits_height(size: int, n_lines: int) -> bool:
        if max_height is None:
            return True
        lh = _ref_line_height(draw, size, role)
        # Интервал между строками пропорционален высоте строки (~30-60%, не
        # фикс. 6px) — согласовано с _draw_centered_block
        # (infographic_rarity.py), иначе проверка "влезает ли" разойдётся
        # с реальной отрисовкой и текст вылезет за пределы плашки.
        gap = max(10, round(lh * 0.6))
        total_h = lh * n_lines + max(0, n_lines - 1) * gap
        return total_h <= max_height

    f_single = fit_font(draw, text, max_width, role, max_size)
    best_sz = f_single.size if _fits_height(f_single.size, 1) else 0
    best = [text] if best_sz else []

    # Алгоритм раньше искал ТОЛЬКО максимальный кегль, без учёта баланса
    # строк — разбивка типа "ARCTIC P12 PRO REVERSE" / "A-RGB" могла
    # выиграть просто потому, что короткая строка позволяет чуть более
    # крупный шрифт, хотя визуально одно короткое слово-«сирота» на
    # отдельной строке смотрится плохо. Считаем отдельно "сбалансированный"
    # рекорд (последняя строка НЕ одно слово, если слов ≥3) — но НАЧИНАЕМ
    # его с уже найденного однострочного варианта, а не с пустоты (пустой
    # старт заставлял алгоритм ВСЕГДА выбирать многострочную разбивку, даже
    # когда однострочный вариант ничем не хуже — «73 CFM»/«600-3000 ОБ/МИН»
    # дробились на 2 строки без всякой причины).
    best_bal, best_bal_sz = best, best_sz

    if len(words) >= 2:
        for i in range(1, len(words)):
            l1 = " ".join(words[:i])
            l2 = " ".join(words[i:])
            sz = min(
                fit_font(draw, l1, max_width, role, max_size).size,
                fit_font(draw, l2, max_width, role, max_size).size,
            )
            if sz > best_sz and _fits_height(sz, 2):
                best_sz = sz
                best = [l1, l2]
            is_orphan = (len(words) - i == 1) and len(words) >= 3
            if not is_orphan and sz > best_bal_sz and _fits_height(sz, 2):
                best_bal_sz = sz
                best_bal = [l1, l2]

    if max_lines >= 3 and len(words) >= 3:
        for i in range(1, len(words) - 1):
            for j in range(i + 1, len(words)):
                l1 = " ".join(words[:i])
                l2 = " ".join(words[i:j])
                l3 = " ".join(words[j:])
                sz = min(
                    fit_font(draw, l1, max_width, role, max_size).size,
                    fit_font(draw, l2, max_width, role, max_size).size,
                    fit_font(draw, l3, max_width, role, max_size).size,
                )
                if sz > best_sz and _fits_height(sz, 3):
                    best_sz = sz
                    best = [l1, l2, l3]
                is_orphan = (len(words) - j == 1) and len(words) >= 4
                if not is_orphan and sz > best_bal_sz and _fits_height(sz, 3):
                    best_bal_sz = sz
                    best_bal = [l1, l2, l3]

    if best_bal:
        best, best_sz = best_bal, best_bal_sz

    if not best:
        # Ничего не влезло даже по высоте (крайний случай) — однострочный
        # вариант без проверки высоты, чтобы не остаться совсем без текста.
        best = [text]
        best_sz = f_single.size

    return best, best_sz


def wrap_text(draw: ImageDraw.ImageDraw, text: str, max_width: int,
              role: str, max_size: int, max_lines: int = 4) -> tuple[list[str], ImageFont.ImageFont]:
    """Разбивает текст на строки фиксированного шрифта — для длинных описаний.

    Возвращает (lines, font), где len(lines) <= max_lines И ВЕСЬ текст
    уложен. Если при текущем размере хотя бы одно слово отсекается —
    пробуем меньший размер. Если уже минимальный — возвращаем что получилось.
    """
    for size in range(max_size, 9, -1):
        font = get_font(size, role)
        words = text.split()
        lines: list[str] = []
        current = ""
        overflow = False
        for word in words:
            test = (current + " " + word).strip()
            try:
                w = draw.textlength(test, font=font)
            except Exception:
                w = len(test) * size * 0.6
            if w <= max_width:
                current = test
            else:
                if current:
                    if len(lines) < max_lines:
                        lines.append(current)
                        current = word
                    else:
                        overflow = True
                        break
                else:
                    # одно слово шире строки — не влезает на этом размере
                    overflow = True
                    break
        if not overflow and current:
            if len(lines) < max_lines:
                lines.append(current)
            else:
                overflow = True
        if not overflow:
            return lines, font
    # Последний фоллбэк — мелким шрифтом. Возвращаем ВСЕ строки, даже если
    # их больше max_lines. _fit_lines_in_box потом обрежет с многоточием —
    # это лучше, чем тихо терять последние слова.
    font = get_font(9, role)
    words = text.split()
    lines, current = [], ""
    for word in words:
        test = (current + " " + word).strip()
        try:
            w = draw.textlength(test, font=font)
        except Exception:
            w = len(test) * 9 * 0.6
        if w <= max_width:
            current = test
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or [text[:40]], font
