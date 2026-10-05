# -*- coding: utf-8 -*-
"""Нормализация номеров и дат документов соответствия ЕАЭС.

Чистые функции без сети и настроек: их используют wb_docs_upload.py и тесты.

Зачем нужна правка «похожих букв». В российских номерах вида
    ЕАЭС N RU Д-CN.РА08.В.60419/25
код страны (CN) и «RU» пишутся латиницей, а буква типа документа (Д/С), код органа
по сертификации (РА08, НВ26) и буква серии (В/А) — кириллицей. При копировании из PDF
кириллица часто превращается в похожую латиницу (PA08.B), и WB отклоняет запрос,
потому что такого номера в реестре нет.
"""
import re
from datetime import date, datetime

# латинские буквы, которые выглядят как кириллические
_LAT2CYR = str.maketrans("ABCEHKMOPTXY", "АВСЕНКМОРТХУ")
# и наоборот — для кода страны, который должен быть латиницей
_CYR2LAT = str.maketrans("АВСЕНКМОРТХУ", "ABCEHKMOPTXY")

# RU Д-CN.РА08.В.60419/25 · RU С-CN.НВ26.В.04260/24 (буквы — в любом алфавите)
_RU_NUMBER = re.compile(
    r"^(RU|РУ)\s*([ДDСC])\s*-\s*([A-ZА-Я]{2})\.([A-ZА-Я]{2}\d{2})\.([A-ZА-Я])\.(\d+/\d{2})$",
    re.I,
)


def fix_lookalikes(body: str) -> str:
    """Чинит перепутанные латиница/кириллица в российском номере ЕАЭС.

    Номера другого формата (KG, BY, AM, …) возвращаются без изменений.
    """
    m = _RU_NUMBER.match(body.strip())
    if not m:
        return body
    _, kind, country, organ, series, tail = m.groups()
    kind = kind.upper().replace("D", "Д").translate(_LAT2CYR)
    return (
        f"RU {kind}-{country.upper().translate(_CYR2LAT)}."
        f"{organ.upper().translate(_LAT2CYR)}.{series.upper().translate(_LAT2CYR)}.{tail}"
    )


def wb_number(num: str, kind: str) -> str:
    """Номер в виде, в котором WB находит его в реестре.

    BY-декларации — «ЕАЭС № BY/112 …» (со «№» и пробелом), BY-сертификаты — без «№»;
    RU-декларации — «ЕАЭС N RU Д-…»; остальным добавляем «ЕАЭС », если его нет.
    """
    n = re.sub(r"\s+", " ", str(num).strip())
    body = re.sub(r"^(ЕАЭС|EAЭC|EAEC)\s*(№|N)?\s*", "", n, flags=re.I)
    if body.upper().startswith("BY"):
        return f"ЕАЭС № {body}" if kind == "decl" else f"ЕАЭС {body}"
    body = fix_lookalikes(body)
    if kind == "decl" and re.match(r"RU\s*Д", body, re.I):
        return f"ЕАЭС N {body}"
    return f"ЕАЭС {body}"


def key_num(s: str) -> str:
    """Ключ для сравнения номеров: без пробелов, знаков, «N»/«№» и разницы латиница/кириллица."""
    s = str(s).upper().replace("№", "")
    s = re.sub(r"^(ЕАЭС|EAЭC|EAEC)\s*N?", "", s)
    return re.sub(r"[^0-9A-ZА-Я]", "", s.translate(_LAT2CYR))


def norm_type(t) -> str | None:
    """«Декларация»/«сертификат» в любом написании → 'decl' / 'cert'."""
    t = str(t or "").strip().lower()
    if t.startswith("декл") or t in ("д", "decl", "declaration"):
        return "decl"
    if t.startswith("серт") or t in ("с", "c", "cert", "certificate"):
        return "cert"
    return None


def norm_date(v) -> str | None:
    """Дата из Excel-ячейки или строки → «дд.мм.гггг»; None, если не распознана."""
    if v in (None, ""):
        return None
    if isinstance(v, (datetime, date)):
        return v.strftime("%d.%m.%Y")
    s = str(v).strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(s[:10], fmt).strftime("%d.%m.%Y")
        except ValueError:
            pass
    return None
