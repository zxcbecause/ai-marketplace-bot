# -*- coding: utf-8 -*-
"""Автоподготовка таблицы к_загрузке.xlsx: какие документы на какие карточки WB. Карточки НЕ меняет.

    .venv\\Scripts\\python.exe tools\\decl_prepare.py [--no-gemini]

Источники связки «артикул → документ» (собраны ранее для Ozon):
  A. Downloads\\_up_<ключ>.xlsx + data\\temp\\tnved_decl\\key_pdfs.json — документы, которые уже
     загружались на Ozon (приоритетный источник);
  B. data\\temp\\final_assignments.json («Бренд||Категория» → артикулы по таблице товаров) +
     data\\temp\\cert_index.json (PDF в папке Сертификаты\\Результат\\<Бренд>\\<Категория>).
     Берём только папки с ОДНИМ документом; где документов несколько — лист «Нужно выбрать».
Номер, тип и даты скрипт читает из PDF сам: текстовый слой — регулярками, сканы и неполные
случаи — через Gemini (ключ из .env). Результаты кэшируются в data\\declarations\\_doc_cache.json,
повторный запуск бесплатный; при лимите Gemini просто запустите завтра — продолжит.
В таблицу попадают только карточки, где на WB документа нет или он отклонён (по последнему снимку).
"""
import argparse
import json
import re
import shutil
import sys
import time
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from config import settings  # noqa: E402
from wb_catalog_snapshot import classify, status_ru  # noqa: E402

DECL = ROOT / "data" / "declarations"
TEMP = ROOT / "data" / "temp"
ARCHIVE = TEMP / "certificates_extract" / "Сертификаты" / "Результат"
DOWNLOADS = Path.home() / "Downloads"
CACHE = DECL / "_doc_cache.json"
OUT = DECL / "к_загрузке.xlsx"
# Ключи, исключённые вручную 30.09 (отклонены на Ozon / не загружены / истекли / BY / 0 товаров)
SKIP_KEYS: set[str] = set()  # ключи групп документов, которые нужно пропускать
NEED = ("none", "none_blocked", "rejected")
D = r"(\d{2}\.\d{2}\.\d{4})"


# ---------- чтение документа ----------
def _pdf_open(path):
    try:
        import pymupdf
    except ImportError:
        import fitz as pymupdf
    return pymupdf.open(path)


def _exists(path: Path) -> bool:
    return path.exists()


def pdf_text(path: Path) -> str:
    if path.suffix.lower() in (".jpg", ".jpeg", ".png"):
        return ""
    try:
        with _pdf_open(path) as doc:
            return "".join(p.get_text() for p in doc)
    except Exception:
        return ""


def regex_parse(text: str) -> dict:
    t = re.sub(r"[ \t\xa0]+", " ", text)
    up = t.upper()
    kind = "decl" if "ДЕКЛАРАЦИЯ О СООТВЕТСТВИИ" in up else ("cert" if "СЕРТИФИКАТ СООТВЕТСТВИЯ" in up else None)
    num = start = end = None
    m = (re.search(r"Регистрационный номер (?:декларации|сертификата)[^:\n]*:\s*([^\n]+)", t)
         or re.search(r"(ЕАЭС\s*(?:N|№)?\s*(?:RU|BY|KG|KZ|AM)\s*[ДСC]-[A-ZА-Я]{2}\.[A-ZА-Я0-9]{2,6}\.[ВB]\.\d{5}/\d{2})", t)
         or re.search(r"(ЕАЭС\s*(?:N|№)?\s*KG\s*417/\d{3}\.[A-ZА-Я]{1,3}\.\d{2}\.\d{5})", t)
         or re.search(r"(ЕАЭС\s*(?:N|№)?\s*KG\s*417/\d{3}\.Д\.\d{7})", t)
         or re.search(r"(ЕАЭС\s*(?:N|№)?\s*KZ\s*\d{7}\.\d{2}\.\d{2}\.\d{5})", t))
    if m:
        num = m.group(1).strip().rstrip(".")
    m = re.search(r"Дата регистрации (?:декларации|сертификата)[^:\n]*:\s*" + D, t) or re.search(r"[Сс]рок действия\s*с\s*" + D, t)
    if m:
        start = m.group(1)
    m = (re.search(r"действительн\w* с даты регистрации по\s*" + D, t)
         or re.search(r"[Сс]рок действия\s*с\s*\d{2}\.\d{2}\.\d{4}\s*(?:г\.?\s*)?по\s*" + D, t))
    if m:
        end = m.group(1)
    b = re.search(r"торгов\w*\s+марк\w*\s*[«\"“]([^»\"”]{2,40})", t)
    return {"type": kind, "number": num, "start": start, "end": end, "how": "текст PDF",
            "brand": b.group(1).strip() if b else None}


MODELS = ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-flash-latest", "gemini-flash-lite-latest", "gemini-2.0-flash"]
_EXHAUSTED: set = set()  # модели, у которых на сегодня кончился лимит


class TransientGemini(Exception):
    pass


PROMPT = """Это документ о соответствии (ЕАЭС). Верни ТОЛЬКО JSON:
{"type": "decl" | "cert" | "other", "number": "...", "start": "ДД.ММ.ГГГГ", "end": "ДД.ММ.ГГГГ" | null,
 "brand": "торговая марка", "product": "вид продукции коротко", "models": ["модели/артикулы, если перечислены"]}
type: "decl" — ДЕКЛАРАЦИЯ О СООТВЕТСТВИИ, "cert" — СЕРТИФИКАТ СООТВЕТСТВИЯ, "other" — отказное/информационное
письмо, протокол и всё прочее. number — регистрационный номер ТОЧНО как напечатан (например
"ЕАЭС N RU Д-XX.XXXX.X.00000/25", "ЕАЭС KG000/000.XX.00.00000", "ЕАЭС BY/000 00.00. ТР000 000.00 00000").
start — дата регистрации/начала действия, end — дата окончания (null, если бессрочно). Ничего не придумывай:
если поле не видно — null."""


def gemini_parse(path: Path, text: str) -> dict | None:
    if not settings.gemini_api_key:
        return None
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=settings.gemini_api_key)
    if len(text) > 300:
        contents = [PROMPT + "\n\nТЕКСТ ДОКУМЕНТА:\n" + text[:15000]]
    else:
        parts = []
        try:
            if path.suffix.lower() in (".jpg", ".jpeg", ".png"):
                mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
                parts.append(types.Part.from_bytes(data=path.read_bytes(), mime_type=mime))
            else:
              with _pdf_open(path) as doc:
                for p in list(doc)[:2]:
                    parts.append(types.Part.from_bytes(data=p.get_pixmap(dpi=170).tobytes("png"),
                                                       mime_type="image/png"))
        except Exception as e:
            return {"type": None, "how": f"PDF не открывается: {str(e)[:80]}"}
        if not parts:
            return None
        contents = parts + [PROMPT]
    last = ""
    for model in MODELS:
        if model in _EXHAUSTED:
            continue
        for attempt in range(3):
            try:
                r = client.models.generate_content(
                    model=model, contents=contents,
                    config=types.GenerateContentConfig(response_mime_type="application/json"))
                d = json.loads(r.text)
                if isinstance(d, list):
                    d = d[0] if d else {}
                d["how"] = "Gemini (скан)" if len(text) <= 300 else "Gemini (текст)"
                time.sleep(4)  # ~15 запросов/мин — в пределах бесплатного лимита
                return d
            except json.JSONDecodeError:
                return {"type": None, "how": "Gemini: непонятный ответ"}
            except Exception as e:
                last = str(e)
                if "PerDay" in last or "per day" in last.lower():
                    _EXHAUSTED.add(model)
                    print(f"    у {model} кончился дневной лимит — переключаюсь на следующую модель", flush=True)
                    if all(m in _EXHAUSTED for m in MODELS):
                        raise RuntimeError("Дневной лимит всех моделей Gemini исчерпан — дальше читаю сканы "
                                           "распознаванием на компьютере (EasyOCR), если оно установлено.")
                    break
                if "404" in last or "NOT_FOUND" in last:
                    break  # такой модели нет — пробуем следующую
                if any(x in last for x in ("429", "RESOURCE_EXHAUSTED", "503", "UNAVAILABLE", "500", "INTERNAL",
                                            "overloaded", "deadline", "timed out")):
                    wait = 15 * (attempt + 1)
                    print(f"    Gemini ({model}) занят, жду {wait} с…", flush=True)
                    time.sleep(wait)
                    continue
                print(f"    Gemini: {last[:150]}", flush=True)
                break
    print("    Gemini не ответил — документ пропущен, при следующем запуске попробую снова.", flush=True)
    raise TransientGemini(last[:200])


_OCR = None


def local_ocr(path: Path) -> str:
    """Распознавание скана на этом компьютере (EasyOCR, без интернета и лимитов). '' — если не установлено."""
    global _OCR
    if _OCR is False:
        return ""
    try:
        if _OCR is None:
            import easyocr
            import torch
            print("    запускаю распознавание на компьютере (EasyOCR)… первый раз скачает модели ~100 МБ", flush=True)
            _OCR = easyocr.Reader(["ru", "en"], gpu=torch.cuda.is_available(), verbose=False)
        lines = []
        if path.suffix.lower() in (".jpg", ".jpeg", ".png"):
            lines += _OCR.readtext(path.read_bytes(), detail=0, paragraph=True)
        else:
            with _pdf_open(path) as doc:
                for p in list(doc)[:2]:
                    lines += _OCR.readtext(p.get_pixmap(dpi=220).tobytes("png"), detail=0, paragraph=True)
        return "\n".join(lines)
    except ImportError:
        _OCR = False
        print("    EasyOCR не установлен — запустите Установить_OCR.bat, чтобы читать сканы без лимитов", flush=True)
        return ""
    except Exception as e:
        print(f"    OCR: {str(e)[:150]}", flush=True)
        return ""


def norm_date(s):
    if not s:
        return None
    m = re.search(D, str(s))
    return m.group(1) if m else None


def read_doc(path: Path, cache: dict, use_gemini: bool, want_meta: bool = False) -> dict:
    key = str(path)
    if want_meta and use_gemini and key in cache and cache[key].get("ok") and not cache[key].get("gemini_done"):
        pass  # для новых документов нужны ещё бренд/модели — спросим Gemini
    elif key in cache and (cache[key].get("ok") or cache[key].get("gemini_done")
                         or (not use_gemini and cache[key].get("ocr_done"))):
        return cache[key]
    text = pdf_text(path) if _exists(path) else ""
    d = regex_parse(text) if len(text) > 300 else {"type": None, "number": None, "start": None, "end": None}
    if not _exists(path):
        d["error"] = "файл не найден"

    def complete():
        return d.get("type") and d.get("number") and d.get("start") and (d.get("end") or d.get("type") == "cert")

    was_complete = complete()
    if (not was_complete or want_meta) and use_gemini and _exists(path):
        try:
            g = gemini_parse(path, text)
            d["gemini_done"] = True
        except TransientGemini:
            g = None
            d["gemini_failed"] = True
        if g:
            for k in ("type", "number", "start", "end", "brand", "product"):
                if g.get(k) and not d.get(k):
                    d[k] = g[k]
            if isinstance(g.get("models"), list):
                d["models"] = [str(m) for m in g["models"]][:300]
            if not was_complete:
                d["how"] = g["how"]
    if not complete() and len(text) <= 300 and _exists(path) and not d.get("gemini_done"):
        ocr_text = local_ocr(path)
        o = regex_parse(ocr_text)
        if ocr_text:
            d["ocr_done"] = True
        if o.get("number") or o.get("start"):
            for k in ("type", "number", "start", "end"):
                if o.get(k) and not d.get(k):
                    d[k] = o[k]
            d["how"] = "OCR на компьютере"
    d["start"], d["end"] = norm_date(d.get("start")), norm_date(d.get("end"))
    d["ok"] = bool(d.get("type") in ("decl", "cert") and d.get("number") and re.search(r"\d{4}", d["number"] or "")
                   and d.get("start"))
    # сверка с именем файла: хвост номера (5+ цифр) должен встречаться в имени — иначе пометка «проверить»
    tail = re.findall(r"\d{5,}", re.sub(r"\D", " ", (d.get("number") or "").split("/")[0]))
    digits = re.sub(r"\D", "", path.name)
    d["name_match"] = bool(tail and tail[-1][-5:] in digits)
    cache[key] = d
    return d


def num_key(s):
    return re.sub(r"[^0-9A-ZА-Я]", "", str(s or "").upper().replace("№", "").replace("N", ""))


# ---------- источники ----------
def source_a():
    kp_file = TEMP / "tnved_decl" / "key_pdfs.json"
    if not kp_file.exists():
        return {}, []
    kp = json.loads(kp_file.read_text(encoding="utf-8"))
    import openpyxl
    out, skipped = {}, []
    for key, pdfs in kp.items():
        f = DOWNLOADS / f"_up_{key}.xlsx"
        if key in SKIP_KEYS or not pdfs or not f.exists():
            skipped.append(key)
            continue
        for r in openpyxl.load_workbook(f, read_only=True).active.iter_rows(min_row=2, values_only=True):
            a = str(r[0]).strip() if r and r[0] is not None else ""
            if a and a != "удалить":
                out.setdefault(a, (Path(pdfs[0]), f"Ozon-файл _up_{key}", "", ""))
    return out, skipped


def source_b():
    fa_file, ci_file = TEMP / "final_assignments.json", TEMP / "cert_index.json"
    if not fa_file.exists() or not ci_file.exists():
        return {}, []
    fa = json.loads(fa_file.read_text(encoding="utf-8"))
    ci = json.loads(ci_file.read_text(encoding="utf-8"))
    out, multi = {}, []
    for group, arts in fa.items():
        brand, cat = group.split("||", 1)
        pdfs = ci.get(brand, {}).get(cat, [])
        if len(pdfs) == 1:
            p = ARCHIVE / brand / cat / pdfs[0]
            for a in arts:
                out.setdefault(str(a).strip(), (p, f"папка {brand}\\{cat}", brand, cat))
        elif pdfs:
            multi.append((brand, cat, pdfs, [str(a).strip() for a in arts]))
    return out, multi


def latest_snapshot():
    snaps = sorted(DECL.glob("_wb_snapshot_*.json"), key=lambda p: p.stat().st_mtime)
    snaps = [s for s in snaps if not s.stem.endswith(("_samples", "_summary"))]
    if not snaps:
        sys.exit("Нет снимка WB — сначала запустите Снимок_WB.bat")
    rows = json.loads(snaps[-1].read_text(encoding="utf-8"))
    for r in rows:
        r["doc"], r["doc_status"], r["verdict"] = classify(r["documents"])
    return snaps[-1], {r["vendorCode"].strip(): r for r in rows}


# ---------- выход ----------
def write_xlsx(main_rows, multi_rows, bad_rows):
    import openpyxl
    from openpyxl.styles import Font, PatternFill
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Документы"
    ws.append(["Артикул или nmID", "Тип (декларация / сертификат)", "Номер документа", "Дата начала", "Дата окончания",
               "ТН ВЭД (необязательно)", "Источник связки", "PDF", "Бренд WB", "Предмет WB", "Статус WB сейчас",
               "Как прочитан", "Проверить"])
    for r in main_rows:
        ws.append(r)
        if r[12]:
            for c in ws[ws.max_row]:
                c.fill = PatternFill("solid", fgColor="FFF2CC")
    widths = (24, 14, 38, 12, 12, 12, 34, 50, 14, 24, 30, 16, 40)
    for i, w in enumerate(widths):
        ws.column_dimensions[openpyxl.utils.get_column_letter(i + 1)].width = w
    for c in ws[1]:
        c.font = Font(bold=True)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions

    w2 = wb.create_sheet("Нужно выбрать")
    w2.append(["Бренд", "Категория", "Документы в папке (несколько — какой к какой модели?)", "Артикулов без документа на WB",
               "Артикулы"])
    for r in multi_rows:
        w2.append(r)
    for c in w2[1]:
        c.font = Font(bold=True)
    for col, w in zip("ABCDE", (16, 26, 80, 14, 80)):
        w2.column_dimensions[col].width = w

    w3 = wb.create_sheet("Не распознано")
    w3.append(["PDF", "Почему", "Тип", "Номер", "Начало", "Окончание", "Артикулов"])
    for r in bad_rows:
        w3.append(r)
    for c in w3[1]:
        c.font = Font(bold=True)
    for col, w in zip("ABCDEFG", (70, 34, 8, 36, 12, 12, 10)):
        w3.column_dimensions[col].width = w
    if OUT.exists():
        shutil.copy(OUT, DECL / f"к_загрузке_старый_{datetime.now():%d_%m_%H%M}.xlsx")
    wb.save(OUT)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-gemini", action="store_true", help="не распознавать сканы (только текстовые PDF)")
    args = ap.parse_args()
    DECL.mkdir(parents=True, exist_ok=True)
    snap, wb_by_vc = latest_snapshot()
    print(f"Снимок WB: {snap.name}, карточек {len(wb_by_vc)}")
    a, a_skip = source_a()
    b, multi = source_b()
    print(f"Связки: Ozon-файлы — {len(a)} артикулов, папки бренд/категория — {len(b)} артикулов, "
          f"папок с несколькими документами — {len(multi)}")
    cand = dict(b)
    cand.update(a)  # Ozon-файлы приоритетнее
    need = {vc: src for vc, src in cand.items()
            if vc in wb_by_vc and wb_by_vc[vc]["doc_status"].split(":")[0] in NEED}
    pdfs = sorted({src[0] for src in need.values()}, key=str)
    print(f"Карточек WB, которым нужен документ и для которых он есть: {len(need)}; документов прочитать: {len(pdfs)}\n")

    cache = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}
    st = {"stop": None, "fails": 0, "gemini": not args.no_gemini, "n": 0}

    def safe_read(p, cache_, _use=None, want_meta=False):
        try:
            d = read_doc(p, cache_, st["gemini"], want_meta)
        except RuntimeError as e:
            print(f"    {e}", flush=True)
            st["stop"], st["gemini"] = str(e), False
            d = read_doc(p, cache_, False, want_meta)
        st["fails"] = st["fails"] + 1 if d.get("gemini_failed") else 0
        if st["gemini"] and st["fails"] >= 3:
            st["gemini"] = False
            st["stop"] = ("Gemini сейчас недоступен (перегружен у Google): сканы не дочитаны, текстовые PDF прочитаны. "
                          "Запустите Подготовить_таблицу.bat позже — дочитает сканы, готовое не потеряется.")
            print("    Gemini недоступен — дальше читаю только текстовые PDF.", flush=True)
        st["n"] += 1
        if st["n"] % 10 == 0:
            print(f"  прочитано документов: {st['n']}", flush=True)
            CACHE.write_text(json.dumps(cache_, ensure_ascii=False, indent=0), encoding="utf-8")
        return d

    for p in pdfs:
        safe_read(p, cache)
    CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=0), encoding="utf-8")

    # ---- новые документы из папки «новые» (Excel со ссылками, PDF, картинки) ----
    from decl_new_sources import collect
    new_folder = DECL / "новые"
    print(f"\nПапка «новые»: читаю документы…", flush=True)
    new_cand, new_report, new_map = collect(
        new_folder, wb_by_vc, lambda p, c, u: safe_read(p, c), cache, st["gemini"], NEED,
        lambda p, c, u: safe_read(p, c, want_meta=True))
    CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=0), encoding="utf-8")
    stop = st["stop"]
    today = date.today()
    main_rows, bad = [], Counter()
    bad_docs = defaultdict(int)
    for vc, (p, src, brand, cat) in sorted(need.items()):
        d = cache.get(str(p))
        w = wb_by_vc[vc]
        if not d or not d.get("ok"):
            why = "не прочитан" if not d else (d.get("error") or ("не декларация/сертификат" if d.get("type") == "other"
                                                                  else "не найден номер/дата"))
            bad_docs[(str(p), why, d and d.get("type"), d and d.get("number"), d and d.get("start"), d and d.get("end"))] += 1
            bad[why] += 1
            continue
        if d.get("end") and datetime.strptime(d["end"], "%d.%m.%Y").date() < today:
            bad_docs[(str(p), "срок истёк", d["type"], d["number"], d["start"], d["end"])] += 1
            bad["срок истёк"] += 1
            continue
        if num_key(w["doc"]) and num_key(d["number"]) in num_key(w["doc"]):
            bad["этот же документ уже отклонён WB"] += 1
            continue
        check = []
        if not d.get("name_match"):
            check.append("номер не совпал с именем файла")
        if d.get("how", "").startswith(("Gemini", "OCR")):
            check.append("прочитан ИИ/OCR — сверить номер")
        if d["type"] == "decl" and not d.get("end"):
            check.append("нет даты окончания")
        main_rows.append([vc, "декларация" if d["type"] == "decl" else "сертификат", d["number"], d["start"],
                          d.get("end") or "", "", src, str(p), w["brand"], w["subject"], status_ru(w["doc_status"]),
                          d.get("how", ""), "; ".join(check)])
    already = {r[0] for r in main_rows}
    n_new = 0
    for vc, (d, src, why) in sorted(new_cand.items()):
        if vc in already:
            continue
        w = wb_by_vc[vc]
        if d.get("end") and datetime.strptime(d["end"], "%d.%m.%Y").date() < today:
            bad["срок истёк"] += 1
            continue
        if num_key(w["doc"]) and num_key(d["number"]) in num_key(w["doc"]):
            bad["этот же документ уже отклонён WB"] += 1
            continue
        check = [why] if "модел" in why else []
        if d.get("how", "").startswith(("Gemini", "OCR")):
            check.append("прочитан ИИ/OCR — сверить номер")
        if d["type"] == "decl" and not d.get("end"):
            check.append("нет даты окончания")
        main_rows.append([vc, "декларация" if d["type"] == "decl" else "сертификат", d["number"], d["start"],
                          d.get("end") or "", "", src, why, w["brand"], w["subject"], status_ru(w["doc_status"]),
                          d.get("how", ""), "; ".join(check)])
        n_new += 1
    multi_rows = []
    for brand, cat, pdf_list, arts in multi:
        n = sum(1 for x in arts if x in wb_by_vc and wb_by_vc[x]["doc_status"].split(":")[0] in NEED and x not in a)
        if n:
            multi_rows.append([brand, cat, "\n".join(pdf_list), n, ", ".join(arts[:60])])
    bad_rows = [[k[0], k[1], k[2], k[3], k[4], k[5], v] for k, v in sorted(bad_docs.items(), key=lambda x: -x[1])]
    bad_rows += new_report
    write_xlsx(main_rows, multi_rows, bad_rows)

    print(f"\nГОТОВО: {OUT}")
    print(f"  к загрузке: {len(main_rows)} карточек "
          f"(из них помечено «проверить»: {sum(1 for r in main_rows if r[12])})")
    for k, v in bad.most_common():
        print(f"  не вошло — {k}: {v}")
    print(f"  папок, где нужно выбрать документ: {len(multi_rows)} (лист «Нужно выбрать»)")
    print(f"  из папки «новые» добавлено: {n_new} карточек")
    if new_map:
        print(f"  ⚠ В новые\\сопоставление_категорий.xlsx {new_map} новых вариантов «документ → предмет WB». "
              f"Проверьте колонку «Брать» (да/нет) и запустите этот .bat ещё раз — выбранное попадёт в таблицу.")
    if stop:
        print(f"\n⚠ {stop}")
    print("\nДальше: просмотрите таблицу (жёлтые строки — проверить), затем Загрузка_документов_WB.bat.")


if __name__ == "__main__":
    main()
