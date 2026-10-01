"""
Работа с локальными WB-шаблонами (xlsx-файлы, скачанные из кабинета WB).

WB-шаблоны содержат DataValidation с нестандартным sqref — openpyxl не может
их открыть без патча. Патч через from_tree применяется при импорте этого модуля.

Структура шаблона:
  Строка 1 — группировки ("Основная информация", "Размеры и Баркоды", ...)
  Строка 2 — доп. строка слияний
  Строка 3 — заголовки колонок (читаем сюда)
  Строка 4 — подсказки/описания полей
  Строка 5+ — данные товаров
"""
import io
import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).parent.parent / "data" / "templates" / "wb"

# ── Патч openpyxl: DataValidation в WB-шаблонах имеет нестандартный sqref ──
def _patch_openpyxl_dv():
    try:
        from openpyxl.worksheet import datavalidation as _dv
        _orig = _dv.DataValidation.from_tree.__func__

        @classmethod
        def _safe_from_tree(cls, el):
            try:
                return _orig(cls, el)
            except Exception:
                obj = object.__new__(cls)
                obj.__dict__['sqref'] = None
                return obj

        _dv.DataValidation.from_tree = _safe_from_tree
        log.debug("openpyxl DataValidation patched for WB templates")
    except Exception as e:
        log.warning(f"DataValidation patch failed: {e}")


_patch_openpyxl_dv()

import openpyxl  # noqa: E402 — импортируем ПОСЛЕ патча


def find_template(subject: str) -> Optional[Path]:
    """Ищет шаблон WB по имени категории.

    Сначала точное совпадение имени файла — иначе, например, «Охлаждение»
    и «Охлаждение корпуса» (одно имя — подстрока другого) конфликтуют между
    собой при нечётком поиске. Нечёткий поиск — только как фоллбэк.
    """
    if not TEMPLATE_DIR.exists():
        return None
    subject_lower = subject.lower().strip()

    for path in sorted(TEMPLATE_DIR.glob("*.xlsx")):
        if path.stem.lower() == subject_lower:
            log.info(f"Найден шаблон WB (точное совпадение): {path.name} для «{subject}»")
            return path

    for path in sorted(TEMPLATE_DIR.glob("*.xlsx")):
        stem_lower = path.stem.lower()
        if subject_lower in stem_lower or stem_lower in subject_lower:
            log.info(f"Найден шаблон WB (нечёткое совпадение): {path.name} для «{subject}»")
            return path
    return None


def read_template(path: Path) -> tuple[dict[str, str], dict[str, str]]:
    """
    Открывает шаблон WB и читает структуру.
    Возвращает:
      col_map  — {letter: col_name}  из строки 3
      hints    — {letter: hint_text} из строки 4
    """
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.active

    col_map: dict[str, str] = {}
    hints: dict[str, str] = {}

    for cell in ws[3]:
        if cell.value:
            col_map[cell.column_letter] = str(cell.value).strip()

    for cell in ws[4]:
        if cell.value:
            hints[cell.column_letter] = str(cell.value).strip()

    wb.close()
    return col_map, hints


def fill_template(path: Path, rows: list[dict[str, str]], start_row: int = 5) -> bytes:
    """
    Клонирует шаблон и заполняет строки данными (с start_row).
    rows — список dict'ов: {название_колонки → значение}.
    Возвращает байты готового xlsx.
    """
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.active

    # Обратный маппинг: название → буква
    name_to_letter: dict[str, str] = {}
    for cell in ws[3]:
        if cell.value:
            name_to_letter[str(cell.value).strip()] = cell.column_letter

    for row_idx, row_data in enumerate(rows, start=start_row):
        for col_name, value in row_data.items():
            if not value:
                continue
            letter = name_to_letter.get(col_name)
            if letter:
                ws[f"{letter}{row_idx}"] = value

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def template_chars(col_map: dict[str, str]) -> list[dict]:
    """
    Конвертирует col_map шаблона в формат chars (как от WB Content API),
    пропуская служебные колонки (Артикул WB, Фото, Баркоды, сертификаты, ...).
    """
    # Колонки, которые заполняются автоматически или не нужны LLM
    _SKIP = {
        "Группа", "Артикул WB", "Фото", "Видео", "КИЗ", "18+",
        "Только для ИП и юрлиц", "Минимальное количество штук в заказе",
        "Подтверждаю, что товар промаркирован", "Баркоды", "Цена",
        "Ставка НДС", "Дата окончания действия сертификата/декларации",
        "Дата регистрации сертификата/декларации",
        "Номер декларации соответствия", "Номер сертификата соответствия",
        "NTIN", "Артикул OZON", "ИКПУ", "Хрупкость",
    }
    # Колонки, которые берутся из generate_full_card, не нужны LLM
    _FROM_CARD = {
        "Артикул продавца", "Наименование", "Бренд", "Описание",
        "Категория продавца",
    }
    # Колонки с единицами измерения (извлекаем из подсказки "(г)", "(см)", "(м)")
    _UNITS = {
        "Вес с упаковкой (кг)": "кг",
        "Вес товара с упаковкой (г)": "г",
        "Высота предмета": "см", "Высота упаковки": "см",
        "Глубина предмета": "см", "Длина упаковки": "см",
        "Ширина предмета": "см", "Ширина упаковки": "см",
        "Длина кабеля (м)": "м",
        "Максимальный выходной ток": "А",
    }

    chars = []
    for letter, name in col_map.items():
        if name in _SKIP or name in _FROM_CARD:
            continue
        chars.append({
            "name": name,
            "required": False,
            "unitName": _UNITS.get(name, ""),
        })
    return chars
