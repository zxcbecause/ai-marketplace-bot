# -*- coding: utf-8 -*-
"""14.09.2026 — общие функции для автозагрузки сертификатов Ozon
(см. ozon_cert_upload.py). Сверка SKU по бренду/категории с последним
экспортом товаров + запись xlsx-шаблона привязки + вспомогательные проверки
(срок действия), вынесенные из ozon_cert_upload.py, чтобы их можно было
дёшево юнит-тестировать без Playwright."""
import glob
import os
from datetime import date, datetime
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parent.parent
DOWNLOADS = Path(os.getenv("DOWNLOADS_DIR", str(Path.home() / "Downloads")))


def parse_ru_date(s: str) -> date:
    """'дд.мм.гггг' -> date. Кидает ValueError на любом другом формате —
    не пытаемся угадывать, если оператор передал что-то не то."""
    return datetime.strptime(s.strip(), "%d.%m.%Y").date()


def days_until(expires: str, today: date | None = None) -> int:
    """Сколько дней осталось до истечения --expires (может быть отрицательным,
    если уже истёк). См. отбракованный кейс Thermaltake 14.09 — сертификат
    был действителен меньше месяца, решили не рисковать привязкой."""
    return (parse_ru_date(expires) - (today or date.today())).days


def latest_products_export() -> Path:
    """Товары_*.xlsx — берём самый свежий по имени/mtime из Downloads."""
    candidates = glob.glob(str(DOWNLOADS / "Товары_*.xlsx"))
    if not candidates:
        raise FileNotFoundError("Не найден экспорт Товары_*.xlsx в Downloads — выгрузи свежий из Ozon")
    return Path(max(candidates, key=os.path.getmtime))


def get_brand_skus(brand: str, ozon_category: str | None = None,
                    exclude_categories: list[str] | None = None,
                    export_path: Path | None = None) -> list[dict]:
    """Артикулы по бренду (регистронезависимо), опционально отфильтрованные
    по точной категории Ozon (как в столбце «Категория» экспорта) или с
    исключением списка категорий."""
    path = export_path or latest_products_export()
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.active
    exclude = {c.lower() for c in (exclude_categories or [])}
    rows = []
    for row in ws.iter_rows(min_row=3, values_only=True):
        if row[0] is None:
            continue
        row_brand = str(row[7] or "")
        if row_brand.lower() != brand.lower():
            continue
        category = str(row[14] or "")
        if ozon_category and category != ozon_category:
            continue
        if category.lower() in exclude:
            continue
        rows.append({"article": str(row[0]), "category": category, "name": row[5]})
    return rows


def write_binding_template(template_path: Path, articles: list[str], out_path: Path) -> Path:
    """Заполняет столбец A свежескачанного шаблона привязки (заголовок в A1
    не трогаем) и сохраняет копию — сам шаблон с диска не переиспользуется
    повторно, каждый прогон качает новый (см. feedback про устаревшие шаблоны)."""
    wb = openpyxl.load_workbook(template_path)
    ws = wb.active
    for i, art in enumerate(articles, start=2):
        ws.cell(row=i, column=1, value=str(art))
    wb.save(out_path)
    return out_path
