"""
Заполнение WB-шаблонов Excel данными из батч-генерации.
Шаблоны хранятся в data/templates/<категория>.xlsx.
Данные заполняются начиная со строки 5, заголовки в строке 3.
"""
import io
import logging
import re
import shutil
from pathlib import Path

from services.card.category import get_tnved_code

log = logging.getLogger(__name__)

# Единицы измерения, которые убираем из числовых значений перед записью в Excel.
# Заголовок столбца уже содержит единицу, поэтому нужно только число.
_UNIT_RE = re.compile(
    r'^(\d+(?:[.,]\d+)?)\s*(?:'
    r'мм|см|дм|м(?!\w)|дюйм(?:а|ов)?|["″’]|'      # длина
    r'Вт|вт|W(?!\w)|кВт|МВт|'                                  # мощность
    r'В(?!\w)|V(?!\w)|мВ|кВ(?!\w)|'                            # напряжение
    r'Ом|ом|Ω|ω|'                                              # сопротивление
    r'ГГц|МГц|кГц|Гц|GHz|MHz|kHz|Hz(?!\w)|'                   # частота (длинные первыми)
    r'мс|нс|мин(?!\w)|ч(?!\w)|с(?!\w)|'                       # время
    r'мА·ч|мАч|mAh|мА|mA|А(?!\w)|аh|ач|'                     # ток/ёмкость
    r'кг|мг|г(?!\w)|'                                          # масса
    r'мл|л(?!\w)|'                                             # объём жидк.
    r'дБА|дБ|dBA|dB(?!\w)|'                                    # уровень звука
    r'лм|Лм|'                                                  # световой поток
    r'°C|°F|℃|℉|К(?!\w)|'                                     # температура
    r'об/мин|rpm|RPM|'                                         # обороты
    r'LPI|lpi|DPI|dpi|PPI|ppi|'                               # разрешение пера
    r'Мпикс|Мпкс|мпкс|Мп(?!\w)|мп(?!\w)|MP(?!\w)|'           # мегапиксели
    r'px|пкс|'                                                 # пиксели
    r'ТБ|ГБ|МБ|КБ|Тб|Гб|Мб|Кб|TB|GB|MB|KB(?!\w)|'           # память
    r'TBW|tbw|IOPS|iops|'                                      # ресурс записи SSD / случайные операции
    r'ядра|ядер|ядро|'                                         # ядра
    r'%'                                                       # проценты
    r')$',
    re.IGNORECASE,
)


def _strip_unit(value: str) -> str:
    """Если значение — число с единицей измерения, возвращает только число."""
    m = _UNIT_RE.match(value.strip())
    if m:
        return m.group(1).replace(",", ".")
    return value


# Колонки-габариты WB — формат всегда «целое или X.X», единица измерения — сантиметры.
_DIMENSION_FIELDS = {
    "Высота предмета", "Ширина предмета", "Глубина предмета", "Длина предмета",
    "Толщина предмета",
    "Высота упаковки", "Ширина упаковки", "Длина упаковки",
}
# Упаковочные поля — всегда целое число, округление вверх (19.2 → 20)
_PACKAGING_FIELDS = {"Высота упаковки", "Ширина упаковки", "Длина упаковки"}

# Числовое значение с единицей длины (мм/см/дм/м) — для конвертации габаритов в см.
_DIM_UNIT_RE = re.compile(r'^(\d+(?:[.,]\d+)?)\s*(мм|см|дм|м)\b', re.IGNORECASE)


def _to_cm(value: str, ceil: bool = False) -> str:
    """Конвертирует размер в сантиметры — формат WB для колонок-габаритов.
    Если единица не указана — считаем, что значение уже в см (как просит промпт).
    ceil=True — округление вверх до целого (для габаритов упаковки)."""
    import math as _math
    m = _DIM_UNIT_RE.match(value.strip())
    if not m:
        raw = _strip_unit(value)
        if ceil:
            try:
                return str(_math.ceil(float(raw.replace(",", "."))))
            except ValueError:
                return raw
        return raw
    num = float(m.group(1).replace(",", "."))
    unit = m.group(2).lower()
    if unit == "мм":
        num /= 10
    elif unit == "м":
        num *= 100
    elif unit == "дм":
        num *= 10
    if ceil:
        return str(_math.ceil(num))
    if num == int(num):
        return str(int(num))
    return f"{num:.1f}"


# Габариты самой мыши (корпуса) — колонки шаблона требуют мм, но LLM часто
# отдаёт значение в см без указания единицы (типичные размеры мыши — 3-15 см,
# то есть число < 20 без единицы почти наверняка см, а не мм).
_MOUSE_DIM_MM_FIELDS = {"Высота мыши", "Длина мыши", "Ширина мыши"}


def _to_mm(value: str) -> str:
    """Приводит размер мыши к мм."""
    m = _DIM_UNIT_RE.match(value.strip())
    if m:
        num = float(m.group(1).replace(",", "."))
        unit = m.group(2).lower()
        if unit == "см":
            num *= 10
        elif unit == "дм":
            num *= 100
        elif unit == "м":
            num *= 1000
    else:
        stripped = _strip_unit(value)
        try:
            num = float(stripped.replace(",", "."))
        except ValueError:
            return value
        if num < 20:
            num *= 10
    if num == int(num):
        return str(int(num))
    return f"{num:.1f}"


TEMPLATES_DIR = Path(r"C:\AI-Bot-V2\data\templates")

# Перевод цветов EN → RU для Excel-колонок «Цвет» / «Название цвета»
_COLOR_RU: dict[str, str] = {
    "black":        "Чёрный",
    "white":        "Белый",
    "red":          "Красный",
    "blue":         "Синий",
    "dark blue":    "Тёмно-синий",
    "light blue":   "Голубой",
    "navy":         "Тёмно-синий",
    "navy blue":    "Тёмно-синий",
    "green":        "Зелёный",
    "dark green":   "Тёмно-зелёный",
    "light green":  "Светло-зелёный",
    "gray":         "Серый",
    "grey":         "Серый",
    "dark gray":    "Тёмно-серый",
    "dark grey":    "Тёмно-серый",
    "light gray":   "Светло-серый",
    "light grey":   "Светло-серый",
    "silver":       "Серебристый",
    "gold":         "Золотой",
    "rose gold":    "Розовое золото",
    "pink":         "Розовый",
    "light pink":   "Светло-розовый",
    "purple":       "Фиолетовый",
    "violet":       "Фиолетовый",
    "orange":       "Оранжевый",
    "yellow":       "Жёлтый",
    "brown":        "Коричневый",
    "beige":        "Бежевый",
    "cyan":         "Голубой",
    "teal":         "Бирюзовый",
    "turquoise":    "Бирюзовый",
    "burgundy":     "Бордовый",
    "space gray":   "Серый",
    "space grey":   "Серый",
    "champagne":    "Шампанский",
    "cream":        "Кремовый",
    "ivory":        "Слоновая кость",
    "coral":        "Коралловый",
    "magenta":      "Малиновый",
    "indigo":       "Индиго",
    "lime":         "Лаймовый",
    "olive":        "Оливковый",
    "mint":         "Мятный",
    "lavender":     "Лавандовый",
    "bronze":       "Бронзовый",
    "copper":       "Медный",
    "titan":        "Титановый",
    "titanium":     "Титановый",
    "graphite":     "Графитовый",
    "midnight":     "Тёмно-синий",
    "starlight":    "Серебристый",
}


def _translate_color(color: str) -> str:
    """Переводит цвет с английского на русский. Результат всегда в нижнем регистре (формат WB/Ozon), без ё."""
    if not color:
        return color
    # Если в строке есть кириллица — уже русский
    if any("Ѐ" <= c <= "ӿ" for c in color):
        return color.strip().lower().replace("ё", "е")
    # Составные цвета "Grey/Black", "Black, White" — переводим каждую часть отдельно
    parts = re.split(r"\s*[/,]\s*", color.strip())
    if len(parts) > 1:
        return "/".join(_translate_color(p) for p in parts if p)
    key = color.strip().lower()
    if key in _COLOR_RU:
        return _COLOR_RU[key].lower().replace("ё", "е")
    # Пробуем первое слово (например "Black Matt" → "black")
    first = key.split()[0] if key.split() else key
    return _COLOR_RU.get(first, color).lower().replace("ё", "е")

# Маппинг категорий бота → имена шаблонных файлов
CATEGORY_TEMPLATES: dict[str, str] = {
    # "SSD накопители" сюда намеренно НЕ включена (16.07): раньше маппилась
    # на "SSD.xlsx", а файла физически нет в data/templates — /excel
    # гарантированно падал на первом же SSD с невнятной ошибкой. Теперь для
    # неё честно "нет шаблона" — см. _load()/fail_reason, /batch и /excel
    # сообщают об этом явно вместо тихого/непонятного отказа.
    # "Смартфоны" вернули 17.07 — пользователь предоставил реальный файл
    # шаблона (Смартфоны.xlsx).
    "Смартфоны":              "Смартфоны.xlsx",
    # "Сетевое оборудование" добавлено 17.07 — файл "Роутеры.xlsx" от
    # пользователя, категория ранее была без шаблона вообще.
    "Сетевое оборудование":   "Сетевое оборудование.xlsx",
    "Планшеты":               "Планшеты.xlsx",
    "Ноутбуки":               "Ноутбуки.xlsx",
    "Наушники":               "Наушники.xlsx",
    "Мониторы":               "Мониторы.xlsx",
    "Клавиатуры":             "Клавиатуры.xlsx",
    "Мыши":                   "Мыши.xlsx",
    "Видеокарты":             "Видеокарты.xlsx",
    "Процессоры":             "Процессоры.xlsx",
    "Охлаждение":             "Охлаждение.xlsx",
    "Акустика":               "Колонки.xlsx",
    "Графические планшеты":   "Планшеты.xlsx",
    "Зарядные устройства":                    "Зарядные устройства.xlsx",
    "Зарядные устройства и блоки питания":    "Зарядные устройства.xlsx",
    "Кабели и аксессуары":                    "Кабели.xlsx",
    "Кабели":                                 "Кабели.xlsx",
    "Блоки питания":          "Блоки питания.xlsx",
    "Материнские платы":      "Материнские платы.xlsx",
    "Моноблоки":              "Моноблоки.xlsx",
    "Адаптеры":               "Адаптеры.xlsx",
    "Компьютеры":             "Компьютеры.xlsx",
    "Охлаждение корпуса":     "Охлаждение корпуса.xlsx",
}

HEADER_ROW = 3   # строка с названиями столбцов
DATA_START  = 5  # первая строка для данных


_SKIP_VALUES = {
    "", "уточнить", "не применимо", "не указано", "нет данных",
    "неизвестно", "н/д", "нет", "-", "—", "–", "n/a", "unknown",
}

def _parse_chars(chars_text: str) -> dict[str, str]:
    """Парсит текст характеристик «Параметр: значение» в словарь.

    Ключи чистятся от LLM-декораций (маркдаун-жирный **, буллеты -/–/•,
    нумерация «1.», «2)») — раньше строка «**Страна-изготовитель**: Китай»
    молча не совпадала с колонкой шаблона и терялась."""
    result = {}
    for line in chars_text.splitlines():
        line = line.strip()
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        # Срезаем буллеты и нумерацию в начале ключа
        key = re.sub(r'^\s*(?:[-–—•>]+|\d{1,2}[.)])\s*', '', key)
        # Маркдаун-обрамление: **Ключ**, *Ключ*, __Ключ__
        key = key.strip().strip('*_').strip()
        val = val.strip().strip('*').strip()
        if key and val.lower() not in _SKIP_VALUES:
            result[key] = val
    return result


def _norm_col_key(name: str) -> str:
    """Нормализация имени колонки для нечёткого сопоставления:
    нижний регистр, без пробелов/дефисов/звёздочек, ё→е."""
    return re.sub(r"[\s\-–—*]+", "", name).lower().replace("ё", "е")


def _parse_packaging(pack_text: str) -> dict[str, str]:
    """Парсит упаковку в словарь."""
    return _parse_chars(pack_text)


class ExcelBatchFiller:
    """
    Открывает шаблон, построчно заполняет по мере генерации карточек,
    возвращает байты готового файла методом get_bytes().
    """

    def __init__(self, category: str):
        self.category = category
        self.wb = None
        self.ws = None
        self.col_map: dict[str, int] = {}  # заголовок → номер столбца
        self._current_row = DATA_START
        self._loaded = False
        self._template_path: Path | None = None
        self.fail_reason: str = ""

    @staticmethod
    def _load_wb(path: Path):
        """Загружает xlsx, вырезая проблемные dataValidation из XML."""
        import zipfile, re as _re, io as _io
        import openpyxl

        # Читаем xlsx как zip и чистим dataValidation во всех листах
        buf = _io.BytesIO()
        with zipfile.ZipFile(str(path), 'r') as zin:
            with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zout:
                for item in zin.infolist():
                    data = zin.read(item.filename)
                    if item.filename.startswith('xl/worksheets/'):
                        try:
                            text = data.decode('utf-8')
                            # Убираем блоки dataValidation полностью
                            text = _re.sub(
                                r'<dataValidations[^>]*>.*?</dataValidations>',
                                '', text, flags=_re.DOTALL
                            )
                            data = text.encode('utf-8')
                        except Exception:
                            pass
                    zout.writestr(item, data)
        buf.seek(0)
        return openpyxl.load_workbook(buf)

    def _load(self) -> bool:
        """Загружает шаблон если он существует. Возвращает True при успехе."""
        try:
            import openpyxl
        except ImportError:
            log.warning("openpyxl не установлен — Excel-заполнение недоступно")
            return False

        tpl_name = CATEGORY_TEMPLATES.get(self.category)
        if not tpl_name:
            self.fail_reason = f"нет шаблона для категории «{self.category}»"
            log.info(f"Excel: {self.fail_reason}")
            return False

        tpl_path = TEMPLATES_DIR / tpl_name
        if not tpl_path.exists():
            self.fail_reason = f"шаблон «{tpl_name}» для категории «{self.category}» числится в конфигурации, но файла нет на диске"
            log.info(f"Excel: файл шаблона не найден: {tpl_path}")
            return False

        self._template_path = tpl_path
        try:
            self.wb = self._load_wb(tpl_path)
            self.ws = self.wb["Товары"]
        except Exception as e:
            self.fail_reason = f"не удалось открыть шаблон «{tpl_name}»: {e}"
            log.warning(f"Excel: {self.fail_reason}")
            return False

        # Строим маппинг заголовок → индекс столбца
        for cell in self.ws[HEADER_ROW]:
            if cell.value:
                self.col_map[str(cell.value).strip()] = cell.column

        log.info(f"Excel: шаблон {tpl_name} загружен, {len(self.col_map)} столбцов")
        self._loaded = True
        return True

    # Поля которые заполняются напрямую из CardResult — не нужно просить LLM
    _SKIP_FOR_LLM = {
        "Наименование", "Бренд", "Описание", "Фото", "Цвет", "Модель",
        "Ставка НДС", "Артикул продавца", "Артикул WB", "Группа",
        "Видео", "КИЗ", "18+", "Только для ИП и юрлиц",
        "Количество штук в упаковке", "Минимальное количество штук в заказе",
        "Подтверждаю, что товар промаркирован",
        "Обязательное предустановленное российское ПО",
        "Баркоды", "Цена",
        # Административные/торговые поля — продавец заполняет вручную
        "Категория продавца", "Артикул OZON", "NTIN", "ИКПУ",
        # Код ТН ВЭД подбирается по категории (см. TNVED_CODES), LLM не спрашиваем
        "Код ТН ВЭД",
        # Цвет пишем сами если известен; если нет — LLM определяет из контекста,
        # поэтому "Название цвета" намеренно НЕ добавляем в этот список.
    }

    # Подстроки в имени колонки → поле полностью пропускается (не спрашиваем LLM, не пишем)
    _SKIP_KEYWORDS = (
        "дата окончания",
        "дата регистрации",
        "сертификат",
        "декларац",           # Номер декларации соответствия
        "certificate",
        "производител",       # Производитель / Производители / Код производителя
        "part номер",
        "part number",
        "партномер",          # Русский вариант part number
        "код тру",            # Код ТРУ 1/2 (налоговый)
        "код упаковки",
        "количество штук в товаре",  # Код по ЭС
    )

    # Поля с перечислимыми значениями — нормализуем ответ LLM к допустимым вариантам
    _ENUM_FIELDS: dict[str, dict[str, str]] = {
        "хрупкость": {
            # ключ: подстрока из ответа LLM (lowercase) → нормализованное значение
            "хрупк": "Хрупкое",
            "fragil": "Хрупкое",
            "не хрупк": "Не хрупкое",
            "not fragil": "Не хрупкое",
            "нет": "Не хрупкое",
            "no": "Не хрупкое",
            "да": "Хрупкое",
            "yes": "Хрупкое",
        },
        "подсветка": {
            # ARGB/a-rgb проверяем раньше "rgb" — иначе "rgb" совпадёт первым
            "argb": "ARGB",
            "a-rgb": "ARGB",
            "rgb": "RGB",
            "rgb-подсвет": "RGB",
            "цветн": "RGB",
            "многоцвет": "RGB",
            "нет": "Отсутствует",
            "отсутств": "Отсутствует",
            "без подсвет": "Отсутствует",
            "no": "Отсутствует",
            "none": "Отсутствует",
        },
        "хват": {
            "лев": "Для левой руки",
            "left": "Для левой руки",
            "прав": "Для правой руки",
            "right": "Для правой руки",
            "универс": "Универсальный",
            "симметр": "Универсальный",
            "ambidext": "Универсальный",
            "both": "Универсальный",
        },
        "игровая модель": {
            "да": "да",
            "yes": "да",
            "нет": "нет",
            "no": "нет",
        },
    }

    # Подстроки в имени колонки → фиксированное значение (без LLM).
    # Порядок важен: проверка идёт сверху вниз с break — «гарант» ловит
    # «Гарантийный срок» раньше, чем общий «срок» («Срок службы» и т.п.).
    _FIXED_VALUES = {
        "гарант": "12 месяцев",
        "срок": "1 год",
    }


    def _field_is_skipped(self, col: str) -> bool:
        col_low = col.lower()
        return any(kw in col_low for kw in self._SKIP_KEYWORDS)

    def get_excel_chars_prompt(self) -> str | None:
        """Строит промпт для LLM из реальных колонок Excel-шаблона.
        Возвращает None если шаблон не загружен."""
        if not self._loaded:
            return None
        fields = [
            col for col in self.col_map
            if col not in self._SKIP_FOR_LLM
            and not self._field_is_skipped(col)
            and col.lower() not in self._FIXED_VALUES  # фиксированные тоже не спрашиваем
            and col.strip()
        ]
        if not fields:
            return None
        fields_str = "\n".join(fields)
        # Собираем подсказки для enum-полей которые есть в этом шаблоне
        enum_hints = []
        for col in fields:
            for enum_key, enum_map in self._ENUM_FIELDS.items():
                if enum_key in col.lower():
                    allowed = sorted(set(enum_map.values()))
                    enum_hints.append(f"— {col}: ТОЛЬКО одно из: {' / '.join(allowed)}")
        enum_block = ("\n\nОГРАНИЧЕНИЯ НА ЗНАЧЕНИЯ:\n" + "\n".join(enum_hints)) if enum_hints else ""

        # Подсказка по габаритам товара — WB всегда в сантиметрах, а в источниках
        # размеры часто указаны в мм (особенно для небольших устройств).
        dim_fields = [col for col in fields if col in _DIMENSION_FIELDS]
        dim_block = (
            "\n\nГАБАРИТЫ (" + ", ".join(dim_fields) + "): "
            "значение строго В САНТИМЕТРАХ (см). Если в источнике размер указан "
            "в миллиметрах — переведи в см (раздели на 10), не пиши мм-значение как есть."
        ) if dim_fields else ""

        return (
            "Ты — эксперт по товарным карточкам Wildberries.\n"
            "Отвечай ТОЛЬКО на русском языке.\n\n"
            "ФОРМАТ: строго «Параметр: значение», один параметр на строке, без заголовков.\n\n"
            f"ПОЛЯ ДЛЯ ЗАПОЛНЕНИЯ:\n{fields_str}"
            f"{enum_block}{dim_block}\n\n"
            "ПРАВИЛА:\n"
            "— Заполняй только поля из списка выше\n"
            "— ПРИОРИТЕТ: блок «ДАННЫЕ ТОВАРА ОТ ПРОДАВЦА» в контексте — абсолютная истина\n"
            "— Поля про разъём/тип подключения/интерфейс (Разъем подключения, Тип подключения, "
            "Интерфейс и т.п.): если в «ОПИСАНИИ ТОВАРА» уже указан конкретный разъём "
            "(USB Type-C, 3.5 мм jack, Lightning, Bluetooth и т.д.) — используй ИМЕННО его, "
            "даже если в других источниках встречается другой вариант (другая версия товара)\n"
            "— Любое числовое значение (мм, дюйм, Гц, уровни, LPI) берётся ТОЛЬКО из строки продавца\n"
            "— Веб-контекст используй ТОЛЬКО для нетехнических полей (OS, материал, совместимость)\n"
            "— ЗАПРЕЩЕНО: брать числа из обзоров/сайтов если они противоречат строке продавца\n"
            "— ЗАПРЕЩЕНО: домысливать и брать характеристики похожих/родственных моделей\n"
            "— Если значение не найдено нигде ИЛИ поле не применимо к данному товару — НЕ ПИШИ эту строку\n"
            "— ЗАПРЕЩЕНО: «уточнить», «не указано», «нет данных», «н/д», «-», «—»\n"
            "— Никаких заголовков и пояснений — только «Параметр: значение»"
        )

    def _normalize_enum(self, col_name: str, value: str) -> str:
        """Если поле enum — нормализует значение к допустимому варианту."""
        col_low = col_name.lower()
        for enum_key, enum_map in self._ENUM_FIELDS.items():
            if enum_key in col_low:
                val_low = value.lower()
                for pattern, normalized in enum_map.items():
                    if pattern in val_low:
                        return normalized
                # Не совпало ни с чем — возвращаем первый допустимый вариант по умолчанию
                return sorted(set(enum_map.values()))[-1]  # "Не хрупкое"
        return value

    # Точные имена колонок, где несколько значений разделяются «;» (WB multi-tag поля)
    _MULTIVALUE_FIELDS = {
        "Комплектация",
        "Особенности модели",
        "Тип подключения",
        "Интерфейс подключения",
        "Интерфейсы",
        "Назначение",
        "Совместимые устройства",
        "Вид гарнитуры",
        "Системы позиционирования",
        "Материал",
        "Тип защиты",
        "Функции",
        "Поддерживаемые форматы",
        "Совместимые ОС",
        "Технологии",
        # Акустика / Колонки
        "Беспроводные интерфейсы",
        "Доп. опции колонок",
        "Совместимость",
        "Материал корпуса",
        # Ноутбуки
        "Доп. опции ноутбука",
        "Назначение товара",
    }

    def _canon_col(self, col_name: str) -> str:
        """Каноническое имя колонки: точное совпадение, иначе нечёткое
        (без регистра/пробелов/дефисов) — LLM пишет «Страна изготовитель»
        или «страна-изготовитель», а колонка называется «Страна производства»…
        точным матчем такие строки терялись молча."""
        if col_name in self.col_map:
            return col_name
        norm = _norm_col_key(col_name)
        if not hasattr(self, "_col_norm_map"):
            self._col_norm_map = {_norm_col_key(n): n for n in self.col_map}
        return self._col_norm_map.get(norm, col_name)

    def _set(self, col_name: str, value) -> bool:
        """Записывает значение в текущую строку по имени столбца."""
        col_name = self._canon_col(col_name)
        col = self.col_map.get(col_name)
        if col and value:
            if col_name in _PACKAGING_FIELDS:
                normalized = _to_cm(str(value), ceil=True)
            elif col_name in _DIMENSION_FIELDS:
                normalized = _to_cm(str(value))
            elif col_name in _MOUSE_DIM_MM_FIELDS:
                normalized = _to_mm(str(value))
            elif "диапазон частот" in col_name.lower():
                # Только максимальная частота: "60-20000 Гц" → "20000"
                parts = re.split(r'[-–—]', str(value))
                normalized = _strip_unit(parts[-1].strip()) if parts else _strip_unit(str(value))
            elif "разъём" in col_name.lower() or "разъем" in col_name.lower():
                # Только тип разъёма: убираем скобки и размеры в мм
                # "TRS (балансный 6.35мм)" → "TRS", "AUX 3.5мм" → "AUX"
                v = re.sub(r'\s*\([^)]*\)', '', str(value))   # убрать (...)
                v = re.sub(r'\s*\d+[.,]?\d*\s*м?мм\b', '', v, flags=re.IGNORECASE)  # убрать 3.5мм
                v = re.sub(r'\s*\d+[.,]?\d*\s*mm\b', '', v, flags=re.IGNORECASE)    # убрать 3.5mm
                normalized = v.strip().rstrip(',;').strip()
            elif "беспроводн" in col_name.lower() and "интерфейс" in col_name.lower():
                # Только название технологии без версии/спецификации, с заглавной буквы
                # "Bluetooth 5.3, Wi-Fi 802.11ac" → "Bluetooth;Wi-Fi"
                items = re.split(r'[,;]', str(value))
                cleaned = []
                for item in items:
                    item = item.strip()
                    item = re.sub(r'\s+\d[\d.]*\b.*$', '', item)   # обрезаем с первой цифры после пробела
                    item = re.sub(r'\s*\([^)]*\)', '', item)        # убрать (...)
                    item = item.strip()
                    if item:
                        item = item[0].upper() + item[1:]
                        cleaned.append(item)
                normalized = ";".join(cleaned) if cleaned else str(value)
            elif "тип подключения" in col_name.lower():
                # Оставляем только "Беспроводное" / "Проводное"
                v = str(value).lower()
                has_wireless = any(w in v for w in ("беспровод", "wireless", "bluetooth", "wi-fi", "wifi", "radio"))
                has_wired    = any(w in v for w in ("провод", "wired", "usb", "jack", "aux", "3.5", "lightning", "type-c", "кабел"))
                if has_wireless and has_wired:
                    normalized = "Беспроводное;Проводное"
                elif has_wireless:
                    normalized = "Беспроводное"
                elif has_wired:
                    normalized = "Проводное"
                else:
                    normalized = str(value)
            elif "питание" in col_name.lower():
                # "От сети" → "от сети" (с маленькой буквы)
                v = str(value).strip()
                normalized = v[0].lower() + v[1:] if v else v
            else:
                normalized = self._normalize_enum(col_name, str(value))
                normalized = _strip_unit(normalized)
            # Multi-tag поля WB: заменяем «, » на «;» чтобы каждый элемент стал отдельным тегом
            if col_name in self._MULTIVALUE_FIELDS and "," in normalized:
                parts = [p.strip() for p in normalized.split(",") if p.strip()]
                max_vals = 8 if col_name == "Доп. опции колонок" else len(parts)
                normalized = ";".join(parts[:max_vals])
            self.ws.cell(row=self._current_row, column=col, value=normalized)
            return True
        return False

    def add_row(self, result, photo_url: str = "", chars_text: str = "",
                pack_text: str = ""):
        """Добавляет строку с данными карточки."""
        if not self._loaded:
            return

        # ── Основные поля ──────────────────────────────────────────────
        if result.article:
            self._set("Артикул продавца", result.article)
        if result.wb_name:
            self._set("Наименование", result.wb_name)
        self._set("Бренд",            result.brand)
        self._set("Описание",         result.description)
        self._set("Модель",           result.model)
        self._set("Ставка НДС",       "16%")

        # Фото — URL через ";"
        if photo_url:
            self._set("Фото", photo_url)

        # ── Фиксированные значения (гарантия и т.п.) ──────────────────
        for col_name in self.col_map:
            col_low = col_name.lower()
            for kw, fixed_val in self._FIXED_VALUES.items():
                if kw in col_low:
                    self._set(col_name, fixed_val)
                    break

        # ── Характеристики ─────────────────────────────────────────────
        chars: dict[str, str] = {}
        if chars_text:
            chars = _parse_chars(chars_text)
            filled = 0
            for param, val in chars.items():
                # Пропускаем скипаемые поля даже если LLM вдруг их вывел
                if self._field_is_skipped(param):
                    continue
                if self._set(param, val):
                    filled += 1
            log.info(f"Excel: заполнено {filled} характеристик для {result.product}")

        # Цвет — пишем в обе возможные колонки («Цвет» и «Название цвета»),
        # ПОСЛЕ характеристик от LLM — иначе при переиспользованном (другого
        # цвета) chars_text «Название цвета» из LLM перезатёрло бы наш правильный.
        # Если цвет неизвестен — «Название цвета» остаётся тем, что определил LLM.
        if result.color:
            color_ru = _translate_color(result.color)
            self._set("Цвет",           color_ru)
            self._set("Название цвета", color_ru)

        # ── Код ТН ВЭД — подбираем по категории, без участия LLM ───────
        tnved = get_tnved_code(self.category, chars)
        if tnved:
            self._set("Код ТН ВЭД", tnved)   # большинство шаблонов
            self._set("ТНВЭД",      tnved)    # шаблон ноутбуков

        # ── Назначение + Радиус — жёстко для наушников ────────────────
        if self.category == "Наушники":
            # "Назначение товара" — точное имя колонки в шаблоне WB
            self._set("Назначение товара", "Смартфоны и мобильные телефоны;Планшеты")
            self._set("Назначение",        "Смартфоны и мобильные телефоны;Планшеты")
            # Радиус беспроводной связи = 10 м для любых беспроводных наушников
            ct_low = (chars_text or "").lower()
            if any(w in ct_low for w in ("bluetooth", "беспровод", "wireless", "wi-fi", "wifi")):
                self._set("Радиус беспроводной связи", "10")

        # ── Упаковка ───────────────────────────────────────────────────
        if pack_text:
            pack = _parse_packaging(pack_text)
            for param, val in pack.items():
                self._set(param, val)

        # ── Фоллбэк: габариты упаковки = габариты товара × 1.17 ────────
        # Срабатывает только если поле упаковки осталось пустым после всех
        # предыдущих шагов. Вес: граммы → кг × 1.20.
        self._fallback_packaging()

        self._current_row += 1

    def _fallback_packaging(self):
        """Вычисляет пустые поля упаковки из габаритов товара × 1.17 (ceil)."""
        import math as _math
        DIM_FACTOR    = 1.17
        WEIGHT_FACTOR = 1.20
        row = self._current_row

        # (поле товара, поле упаковки)
        DIM_PAIRS = [
            ("Высота предмета",  "Высота упаковки"),
            ("Ширина предмета",  "Ширина упаковки"),
            ("Глубина предмета", "Длина упаковки"),
            ("Длина предмета",   "Длина упаковки"),
            ("Толщина предмета", "Высота упаковки"),  # ноутбуки
        ]
        for src_name, dst_name in DIM_PAIRS:
            src_col = self.col_map.get(src_name)
            dst_col = self.col_map.get(dst_name)
            if not src_col or not dst_col:
                continue
            dst_cell = self.ws.cell(row=row, column=dst_col)
            if dst_cell.value not in (None, ""):   # уже заполнено
                continue
            src_val = self.ws.cell(row=row, column=src_col).value
            if not src_val:
                continue
            try:
                num = float(str(src_val).replace(",", "."))
                dst_cell.value = _math.ceil(num * DIM_FACTOR)
                log.debug(f"Упаковка fallback: {src_name}={num} → {dst_name}={dst_cell.value}")
            except (ValueError, TypeError):
                pass

        # Вес: (поле товара, поле упаковки, делитель г→кг)
        WEIGHT_PAIRS = [
            ("Вес товара без упаковки (г)", "Вес с упаковкой (кг)", 1000.0),
            ("Вес без упаковки (кг)",       "Вес с упаковкой (кг)", 1.0),
            ("Вес товара с упаковкой (г)",  "Вес с упаковкой (кг)", 1000.0),
        ]
        for src_name, dst_name, divisor in WEIGHT_PAIRS:
            src_col = self.col_map.get(src_name)
            dst_col = self.col_map.get(dst_name)
            if not src_col or not dst_col:
                continue
            dst_cell = self.ws.cell(row=row, column=dst_col)
            if dst_cell.value not in (None, ""):
                continue
            src_val = self.ws.cell(row=row, column=src_col).value
            if not src_val:
                continue
            try:
                num = float(str(src_val).replace(",", "."))
                packed = round(num * WEIGHT_FACTOR / divisor, 3)
                dst_cell.value = packed
                log.debug(f"Вес fallback: {src_name}={num} → {dst_name}={packed}")
                break  # нашли один источник — достаточно
            except (ValueError, TypeError):
                pass

    def get_bytes(self) -> bytes | None:
        """Возвращает байты заполненного xlsx-файла."""
        if not self._loaded or self._current_row == DATA_START:
            return None
        try:
            buf = io.BytesIO()
            self.wb.save(buf)
            return buf.getvalue()
        except Exception as e:
            log.warning(f"Excel: ошибка сохранения: {e}")
            return None

    @property
    def has_template(self) -> bool:
        return self._loaded

    @property
    def rows_filled(self) -> int:
        return self._current_row - DATA_START
