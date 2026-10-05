from datetime import date, datetime

import pytest
from doc_numbers import fix_lookalikes, key_num, norm_date, norm_type, wb_number


# ---------- латиница / кириллица в номере ----------
@pytest.mark.parametrize("raw, expected", [
    # реальный случай: «PA08.B» набрано латиницей, WB не находил номер в реестре
    ("RU Д-CN.PA08.B.60419/25", "RU Д-CN.РА08.В.60419/25"),
    # сертификат: «С» и код органа латиницей
    ("RU C-CN.HB26.B.04260/24", "RU С-CN.НВ26.В.04260/24"),
    # код страны случайно кириллицей — возвращаем латиницу
    ("RU Д-СN.РА02.В.47382/25", "RU Д-CN.РА02.В.47382/25"),
    # уже правильный номер не меняется
    ("RU Д-CN.РА07.В.24430/25", "RU Д-CN.РА07.В.24430/25"),
    # пробелы вокруг дефиса
    ("RU Д - CN.PA01.B.00436/24", "RU Д-CN.РА01.В.00436/24"),
])
def test_fix_lookalikes_ru(raw, expected):
    assert fix_lookalikes(raw) == expected


@pytest.mark.parametrize("raw", [
    "KG417/052.HK.02.12764",
    "KG 417/052.HK.02.12764",
    "BY/112 11.01. ТР037 118.01 12594",
    "что-то непонятное",
])
def test_fix_lookalikes_other_formats_untouched(raw):
    assert fix_lookalikes(raw) == raw


# ---------- итоговый вид для WB ----------
@pytest.mark.parametrize("num, kind, expected", [
    ("ЕАЭС N RU Д-CN.PA08.B.60419/25", "decl", "ЕАЭС N RU Д-CN.РА08.В.60419/25"),
    ("RU Д-CN.РА05.В.92641/24", "decl", "ЕАЭС N RU Д-CN.РА05.В.92641/24"),
    ("ЕАЭС RU C-CN.HB26.B.04260/24", "cert", "ЕАЭС RU С-CN.НВ26.В.04260/24"),
    ("ЕАЭС KG417/044.TW.02.04764", "cert", "ЕАЭС KG417/044.TW.02.04764"),
    ("BY/112 11.01. ТР037 118.01 12594", "decl", "ЕАЭС № BY/112 11.01. ТР037 118.01 12594"),
    ("BY/112 11.01. ТР037 118.01 12594", "cert", "ЕАЭС BY/112 11.01. ТР037 118.01 12594"),
    ("  ЕАЭС   N  RU Д-CN.РА01.В.17487/26 ", "decl", "ЕАЭС N RU Д-CN.РА01.В.17487/26"),
])
def test_wb_number(num, kind, expected):
    assert wb_number(num, kind) == expected


def test_key_num_ignores_alphabet_and_formatting():
    sent = wb_number("RU Д-CN.PA08.B.60419/25", "decl")
    stored_by_wb = "ЕАЭС N RU Д-CN.РА08.В.60419/25"
    assert key_num(sent) == key_num(stored_by_wb)
    assert key_num("ЕАЭС N RU Д-CN.PA08.B.60419/25") == key_num(stored_by_wb)


def test_key_num_differs_for_different_documents():
    assert key_num("ЕАЭС N RU Д-CN.РА08.В.60419/25") != key_num("ЕАЭС N RU Д-CN.РА08.В.60418/25")


# ---------- тип и даты ----------
@pytest.mark.parametrize("raw, expected", [
    ("Декларация", "decl"), ("декларация о соответствии", "decl"), ("decl", "decl"),
    ("Сертификат", "cert"), ("cert", "cert"), ("С", "cert"),
    ("Отказное письмо", None), ("", None), (None, None),
])
def test_norm_type(raw, expected):
    assert norm_type(raw) == expected


@pytest.mark.parametrize("raw, expected", [
    ("28.06.2024", "28.06.2024"),
    ("2024-06-28", "28.06.2024"),
    ("28/06/2024", "28.06.2024"),
    ("28.06.24", "28.06.2024"),
    ("2024-06-28 00:00:00", "28.06.2024"),
    (date(2029, 6, 27), "27.06.2029"),
    (datetime(2029, 6, 27, 12, 30), "27.06.2029"),
    ("бессрочно", None),
    ("", None),
    (None, None),
])
def test_norm_date(raw, expected):
    assert norm_date(raw) == expected
