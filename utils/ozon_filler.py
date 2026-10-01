"""
Универсальный загрузчик/заполнитель официальных Ozon-шаблонов Excel.

Шаблон скачивается из личного кабинета Ozon Seller под конкретную категорию.
Структура одинакова для всех категорий:
  - лист "Шаблон"     — данные: заголовки в HEADER_ROW, строки данных с DATA_START,
                         строка REQUIRED_ROW отмечает обязательные/множественные поля.
  - лист "validation" — допустимые значения для выпадающих списков, ПОД ТЕМ ЖЕ
                         номером столбца, что и в "Шаблон" (т.е. колонка R в обоих
                         листах — один и тот же атрибут).

В отличие от ozon_excel_fill.py (где список параметров наушников был
написан руками), здесь список полей и допустимые значения берутся прямо
из самого шаблона — для любой категории, без угадывания.
"""
import io
import logging
import re
from pathlib import Path

from utils.excel_filler import _strip_unit, _parse_chars, _translate_color

log = logging.getLogger(__name__)

SHEET_NAME = "Шаблон"
VALIDATION_SHEET = "validation"
HEADER_ROW = 2
REQUIRED_ROW = 3
DATA_START = 5

# Колонки, которые заполняются напрямую кодом — не спрашиваем LLM.
_SKIP_FOR_LLM = {
    "№", "Артикул*", "Название товара", "Цена, KZT*", "Цена до скидки, KZT",
    "НДС, %*", "SKU", "Штрихкод (Серийный номер / EAN)",
    "Вес в упаковке, г*", "Ширина упаковки, мм*", "Высота упаковки, мм*", "Длина упаковки, мм*",
    "Ссылка на главное фото*", "Ссылки на дополнительные фото", "Артикул фото",
    "Бренд*", "Название модели (для объединения в одну карточку)*", "Тип*",
    "ТН ВЭД коды ЕАЭС*", "Название модели для шаблона наименования",
    "Объединить в похожие товары", "Rich-контент JSON", "#Хештеги",
    "Ошибка", "Предупреждение", "Цвет товара",
    # "Аннотация" заполняется отдельно через generate_description (формула
    # 1500-1900 символов) — если не исключить отсюда, промпт характеристик
    # тоже пытается её заполнить (коротким однострочником) и перезатирает
    # нормальное описание в add_row (порядок: description → потом chars).
    "Аннотация",
    # Административные/учётные поля продавца — LLM не может знать реальные
    # значения, заполняет наугад (14.07.2026: просьба продавца игнорировать).
    "Код производителя", "Код упаковки", "ИКПУ", "Артикул OZON", "NTIN",
    "Код ТРУ 1", "Код ТРУ 2",
}

# Выпадающий список длиннее этого — не вставляем целиком в промпт
# (страны, ТН ВЭД — сотни вариантов, это сожгло бы токены без пользы).
_MAX_ENUM_LEN = 60

# Для полей с длинным списком — короткая подсказка по формату значения
# вместо самого списка (иначе LLM пишет в произвольном формате и
# _normalize_value не находит совпадение).
_FORMAT_HINTS: dict[str, str] = {
    "Объем": (
        'формат "<число> ГБ" или "<число> ТБ" — пробел перед единицей, '
        'дробная часть через запятую (например "512 ГБ", "1 ТБ", "1,5 ТБ")'
    ),
    "Гарантия": 'формат "N год/года/лет" или "N месяц/месяцев" (например "1 год", "3 года")',
    "Страна-изготовитель": (
        "одна страна на русском, например «Тайвань», «Китай», «Вьетнам». "
        'Ищи в источниках фразы "Made in", "Country of origin", "произведено в", '
        "страна сборки бренда — это тоже считается. Пиши только если страна "
        "указана явно хоть в каком-то источнике, не оставляй пустым просто "
        "из общей неуверенности"
    ),
    "Ресурс SSD (TBW)": "ТОЛЬКО число, без единиц измерения (например «200», не «200 TBW» и не «200 ТБ»)",
}

# Габариты/вес упаковки, которые генерирует generate_packaging() —
# ключ из её вывода → возможные имена колонок в Ozon-шаблоне (см мм/см, кг/г).
_PACK_SRC_MAP: dict[str, list[str]] = {
    "Длина упаковки (см)":  ["Длина упаковки, мм*", "Длина упаковки, см*"],
    "Ширина упаковки (см)": ["Ширина упаковки, мм*", "Ширина упаковки, см*"],
    "Высота упаковки (см)": ["Высота упаковки, мм*", "Высота упаковки, см*"],
    "Вес с упаковкой (кг)": ["Вес в упаковке, г*", "Вес в упаковке, кг*"],
}


def _load_workbook_clean(path: Path):
    """Открывает xlsx, вырезая dataValidation из XML — иначе openpyxl иногда
    падает на сложных шаблонах Ozon (вложенные/перекрёстные валидации)."""
    import zipfile
    import openpyxl

    buf = io.BytesIO()
    with zipfile.ZipFile(str(path), "r") as zin:
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                data = zin.read(item.filename)
                if item.filename.startswith("xl/worksheets/"):
                    try:
                        text = data.decode("utf-8")
                        text = re.sub(
                            r"<dataValidations[^>]*>.*?</dataValidations>",
                            "", text, flags=re.DOTALL,
                        )
                        data = text.encode("utf-8")
                    except Exception:
                        pass
                zout.writestr(item, data)
    buf.seek(0)
    return openpyxl.load_workbook(buf)


class OzonExcelFiller:
    """Заполняет официальный Ozon xlsx-шаблон категории данными бота."""

    def __init__(self, template_path: Path, category: str):
        self.template_path = Path(template_path)
        self.category = category
        self.wb = None
        self.ws = None
        self.col_map: dict[str, int] = {}      # имя колонки → индекс
        self.required: set[str] = set()
        self.multiselect: set[str] = set()
        self.enum_options: dict[str, list[str]] = {}
        self._current_row = DATA_START
        self._loaded = False

    def load(self) -> bool:
        try:
            self.wb = _load_workbook_clean(self.template_path)
            self.ws = self.wb[SHEET_NAME]
        except Exception as e:
            log.warning(f"Ozon-шаблон: не удалось открыть {self.template_path}: {e}")
            return False

        for cell in self.ws[HEADER_ROW]:
            if cell.value:
                self.col_map[str(cell.value).strip()] = cell.column

        idx_to_name = {c: n for n, c in self.col_map.items()}
        for cell in self.ws[REQUIRED_ROW]:
            if not cell.value:
                continue
            name = idx_to_name.get(cell.column)
            if not name:
                continue
            marker = str(cell.value)
            if "Обязательное" in marker:
                self.required.add(name)
            if "Множественный" in marker:
                self.multiselect.add(name)

        if VALIDATION_SHEET in self.wb.sheetnames:
            vws = self.wb[VALIDATION_SHEET]
            max_r = vws.max_row
            for col_name, col_idx in self.col_map.items():
                vals = [vws.cell(row=r, column=col_idx).value for r in range(1, max_r + 1)]
                vals = [str(v).strip() for v in vals if v]
                if vals:
                    self.enum_options[col_name] = vals

        log.info(
            f"Ozon-шаблон «{self.category}»: {len(self.col_map)} колонок, "
            f"{len(self.enum_options)} с выпадающими списками"
        )
        self._loaded = True
        return True

    def get_chars_prompt(self) -> str | None:
        """Строит промпт для LLM из реальных колонок шаблона + реальных
        допустимых значений (вместо угаданного руками списка)."""
        if not self._loaded:
            return None
        fields = [c for c in self.col_map if c not in _SKIP_FOR_LLM and c.strip()]
        if not fields:
            return None

        lines = []
        for col in fields:
            req_tag = " [ОБЯЗАТЕЛЬНОЕ]" if col in self.required else ""
            opts = self.enum_options.get(col)
            if opts and len(opts) <= _MAX_ENUM_LEN:
                tag = " (можно несколько через запятую)" if col in self.multiselect else ""
                lines.append(f"{col}{req_tag}{tag}: ТОЛЬКО значение(я) из списка: {' / '.join(opts)}")
            elif col in _FORMAT_HINTS:
                lines.append(f"{col}{req_tag}: {_FORMAT_HINTS[col]}")
            else:
                lines.append(f"{col}{req_tag}")

        return (
            "Ты — эксперт по товарным карточкам Ozon.\n"
            "Отвечай ТОЛЬКО на русском языке.\n\n"
            "ФОРМАТ: строго «Параметр: значение», один параметр на строке, без заголовков.\n\n"
            f"ПОЛЯ ДЛЯ ЗАПОЛНЕНИЯ:\n" + "\n".join(lines) + "\n\n"
            "ПРАВИЛА:\n"
            "— Заполняй только поля из списка выше\n"
            "— Если для поля указан список допустимых значений — используй ТОЛЬКО значение "
            "из списка, буква в букву, ничего не придумывай и не сокращай\n"
            "— Поля с пометкой [ОБЯЗАТЕЛЬНОЕ] заполняй ВСЕГДА: если точного значения нет "
            "в источниках — дай наиболее вероятное на основе знаний о бренде, модели и "
            "категории товара. Пропуск обязательного поля хуже разумной оценки.\n"
            "— БАЗОВЫЕ поля (Страна-изготовитель, Гарантия, Цвет, Тип, назначение) тоже "
            "заполняй уверенно: они почти всегда выводятся из бренда и категории\n"
            "— Точные ЧИСЛОВЫЕ техпараметры (частоты, ёмкости, размеры) НЕ выдумывай: "
            "если числа нет в источниках — не пиши эту строку\n"
            "— Необязательное поле неприменимо к товару — не пиши эту строку\n"
            "— ЗАПРЕЩЕНО: «уточнить», «не указано», «нет данных», «н/д», «-», «—»\n"
            "— Никаких заголовков и пояснений — только «Параметр: значение»"
        )

    def _normalize_value(self, col: str, value: str) -> str:
        value = value.strip()
        opts = self.enum_options.get(col)
        if not opts:
            return _strip_unit(value)
        opt_lower = {o.lower(): o for o in opts}
        if col in self.multiselect:
            parts = re.split(r"[,;]", value)
            matched = []
            for p in parts:
                p = p.strip()
                if not p:
                    continue
                hit = opt_lower.get(p.lower()) or next(
                    (o for o in opts if p.lower() in o.lower() or o.lower() in p.lower()), None
                )
                if hit:
                    matched.append(hit)
            return ";".join(dict.fromkeys(matched))
        hit = opt_lower.get(value.lower()) or next(
            (o for o in opts if value.lower() in o.lower() or o.lower() in value.lower()), None
        )
        return hit or value

    def _canon_col(self, col: str) -> str:
        """Точное имя колонки, иначе нечёткий матч (регистр/пробелы/дефисы) —
        LLM-вариации вида «страна изготовитель» не должны терять значение."""
        if col in self.col_map:
            return col
        from utils.excel_filler import _norm_col_key
        if not hasattr(self, "_col_norm_map"):
            self._col_norm_map = {_norm_col_key(n): n for n in self.col_map}
        return self._col_norm_map.get(_norm_col_key(col), col)

    def _set(self, col: str, value) -> bool:
        col = self._canon_col(col)
        col_idx = self.col_map.get(col)
        if not col_idx or value in (None, ""):
            return False
        value = str(value).strip()
        if not value:
            return False
        normalized = self._normalize_value(col, value)
        cell_value: object = normalized
        # Поля без выпадающего списка и без множественного выбора — пробуем
        # привести к числу (цена, скорости, IOPS и т.п. Ozon ждёт как число,
        # не текст). Категориальные/текстовые поля (enum, мультивыбор,
        # артикул, название) остаются строкой.
        if col not in self.enum_options and col not in self.multiselect:
            num_str = normalized.replace(",", ".").replace(" ", "")
            try:
                num = float(num_str)
                cell_value = int(num) if num == int(num) else num
            except ValueError:
                pass
        self.ws.cell(row=self._current_row, column=col_idx, value=cell_value)
        return True

    def set_packaging(self, pack: dict[str, str]):
        for src_key, dst_candidates in _PACK_SRC_MAP.items():
            raw = pack.get(src_key)
            if not raw:
                continue
            try:
                num = float(str(raw).replace(",", "."))
            except ValueError:
                continue
            for dst in dst_candidates:
                col_idx = self.col_map.get(dst)
                if not col_idx:
                    continue
                if "мм*" in dst or "мм" == dst[-3:]:
                    val = round(num * 10)
                elif ", г*" in dst or dst.endswith("г*"):
                    val = round(num * 1000)
                else:
                    val = num
                self.ws.cell(row=self._current_row, column=col_idx, value=val)
                break

    def add_row(
        self, *, article: str, name: str, brand: str = "", model: str = "",
        fixed_type: str = "", color: str = "", tnved: str = "",
        description: str = "", photo_url: str = "",
        chars_text: str = "", pack: dict[str, str] | None = None,
        vat: str = "16", price: float | str = "",
    ):
        if not self._loaded:
            return

        self._set("Артикул*", article)
        self._set("Название товара", name)
        self._set("НДС, %*", vat)
        if price:
            self._set("Цена, KZT*", price)
        if brand:
            self._set("Бренд*", brand)
        if model:
            self._set("Название модели (для объединения в одну карточку)*", model)
        if fixed_type:
            self._set("Тип*", fixed_type)
        if tnved:
            self._set("ТН ВЭД коды ЕАЭС*", tnved)
        if description:
            self._set("Аннотация", description)
        if photo_url:
            self._set("Ссылка на главное фото*", photo_url)
        if color:
            self._set("Цвет товара", _translate_color(color))

        if chars_text:
            chars = _parse_chars(chars_text)
            filled = 0
            for param, val in chars.items():
                if param in _SKIP_FOR_LLM:
                    continue
                if self._set(param, val):
                    filled += 1
            log.info(f"Ozon-шаблон: заполнено {filled} характеристик для {name}")

        if pack:
            self.set_packaging(pack)

        if "№" in self.col_map:
            self.ws.cell(
                row=self._current_row, column=self.col_map["№"],
                value=self._current_row - DATA_START + 1,
            )

        self._current_row += 1

    def get_bytes(self) -> bytes | None:
        if not self._loaded or self._current_row == DATA_START:
            return None
        try:
            buf = io.BytesIO()
            self.wb.save(buf)
            return buf.getvalue()
        except Exception as e:
            log.warning(f"Ozon-шаблон: ошибка сохранения: {e}")
            return None

    def save(self, path: Path) -> bool:
        data = self.get_bytes()
        if not data:
            return False
        Path(path).write_bytes(data)
        return True

    @property
    def has_template(self) -> bool:
        return self._loaded

    @property
    def rows_filled(self) -> int:
        return self._current_row - DATA_START


# Поля, которые process_ozon_product заполняет сам (api_values в
# services/ozon_pipeline.py) или которые не применимы без Excel-шаблона —
# не спрашиваем LLM повторно ни в OzonExcelFiller, ни в ApiOnlyFiller.
API_ONLY_SKIP_ATTRS = {
    "Бренд", "Тип", "Название модели (для объединения в одну карточку)",
    "Аннотация", "ТН ВЭД коды ЕАЭС", "Нужен код маркировки", "Цвет товара",
    "Rich-контент JSON", "Название файла PDF", "Объединить в похожие товары",
    "Партномер", "Название", "Класс опасности товара", "Документ PDF",
    "#Хештеги", "Код продавца",
    "Вес товара, г", "Размеры, мм", "Вес с упаковкой, г",
    # Административные/учётные поля продавца — LLM не может знать реальные
    # значения, заполняет наугад (14.07.2026: просьба продавца игнорировать).
    "Код производителя", "Код упаковки", "ИКПУ", "Артикул OZON", "NTIN",
    "Код ТРУ 1", "Код ТРУ 2",
}


class ApiOnlyFiller:
    """Замена OzonExcelFiller, когда нет скачанного Excel-шаблона Ozon для
    категории — список полей и признак "обязательное" берём напрямую из
    Ozon API (get_category_attributes уже даёт description на каждое поле,
    даже подробнее, чем лист validation в Excel-шаблоне). Используется как
    filler в process_ozon_product — тот вызывает только get_chars_prompt()
    и add_row(), оба метода здесь совместимы по интерфейсу.
    Введено 13.07.2026 (батч «Пылесосы» — категории без шаблона)."""

    def __init__(self, attrs_meta: list[dict]):
        self._attrs_meta = attrs_meta

    def get_chars_prompt(self) -> str | None:
        fields = [
            a for a in self._attrs_meta
            if a["name"] not in API_ONLY_SKIP_ATTRS and not a["name"].startswith("Озон.")
        ]
        if not fields:
            return None
        lines = []
        for a in fields:
            req_tag = " [ОБЯЗАТЕЛЬНОЕ]" if a.get("is_required") else ""
            if a.get("dictionary_id"):
                hint = " (выбери наиболее подходящее значение из справочника Ozon)"
            elif a.get("description"):
                hint = f" — {a['description'][:180]}"
            else:
                hint = ""
            lines.append(f"{a['name']}{req_tag}{hint}")
        return (
            "Ты — эксперт по товарным карточкам Ozon.\n"
            "Отвечай ТОЛЬКО на русском языке.\n\n"
            "ФОРМАТ: строго «Параметр: значение», один параметр на строке, без заголовков.\n\n"
            f"ПОЛЯ ДЛЯ ЗАПОЛНЕНИЯ:\n" + "\n".join(lines) + "\n\n"
            "ПРАВИЛА:\n"
            "— Заполняй только поля из списка выше\n"
            "— Поля с пометкой [ОБЯЗАТЕЛЬНОЕ] заполняй ВСЕГДА: если точного значения нет "
            "в источниках — дай наиболее вероятное на основе знаний о бренде, модели и "
            "категории товара. Пропуск обязательного поля хуже разумной оценки.\n"
            "— БАЗОВЫЕ поля (страна-изготовитель, гарантия, назначение) тоже заполняй "
            "уверенно: они почти всегда выводятся из бренда и категории\n"
            "— Точные ЧИСЛОВЫЕ техпараметры (мощность, объём, вес) НЕ выдумывай: "
            "если числа нет в источниках — не пиши эту строку\n"
            "— Необязательное поле неприменимо к товару — не пиши эту строку\n"
            "— ЗАПРЕЩЕНО: «уточнить», «не указано», «нет данных», «н/д», «-», «—»\n"
            "— Никаких заголовков и пояснений — только «Параметр: значение»"
        )

    def add_row(self, **kwargs):
        pass  # архивный Excel в этом режиме не ведём
