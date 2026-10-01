# -*- coding: utf-8 -*-
"""Новые документы из папки data\\declarations\\новые\\ для decl_prepare.py.

  * Excel-файлы с листами «Citilink» / «M.Video» (Категория | Бренд | Тип | Номер | Ссылка на PDF | …):
    номер берётся из таблицы, даты — из текста строки или из PDF по ссылке (скачивается в новые\\_скачано\\);
    нотификации ФСБ пропускаются — это не декларации/сертификаты соответствия.
    Лист «DNS-shop находки» пропускается (номеров нет, только описание).
  * PDF / JPG / PNG в папке: номер, тип, даты, бренд, вид продукции и модели читаются из документа
    (Gemini; без Gemini — номер/даты регуляркой или OCR, бренд — из имени файла).
Привязка к карточкам WB:
  * по моделям из документа (модель встречается в названии/артикуле карточки того же бренда) — сразу в таблицу;
  * иначе по «бренд + категория» через файл сопоставление_категорий.xlsx: скрипт сам предлагает предметы WB,
    вы ставите «да/нет» в колонке «Брать» — при следующем запуске выбранное попадёт в таблицу.
"""
import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

D = r"(\d{2}\.\d{2}\.\d{4})"
IMG = (".jpg", ".jpeg", ".png")


def norm(s):
    return re.sub(r"[^0-9a-zа-яё]", "", str(s or "").lower())


def stems(s):
    return {w[:5] for w in re.findall(r"[а-яёa-z]{3,}", str(s or "").lower())
            if w not in ("для", "and", "the", "или", "прочие", "аксессуары")}


def brand_variants(b):
    b = re.sub(r"\(и ещё.*?\)", "", str(b or ""), flags=re.I)
    parts = re.split(r"[/,]|\(|\)", b)
    return {norm(p) for p in parts if len(norm(p)) >= 2}


# ---------- Excel ----------
def excel_rows(folder: Path):
    import openpyxl
    out = []
    for f in sorted(folder.glob("*.xlsx")):
        if f.name.startswith(("~$", "сопоставление")):
            continue
        wb = openpyxl.load_workbook(f, read_only=True, data_only=True)
        for ws in wb.worksheets:
            rows = list(ws.iter_rows(values_only=True))
            if not rows:
                continue
            head = [str(h or "").lower() for h in rows[0]]
            if not any("номер" in h for h in head) or not any("бренд" in h for h in head):
                continue  # лист без номеров (DNS, методология) — пропуск
            ci = lambda *keys: next((i for i, h in enumerate(head) if all(k in h for k in keys)), None)
            i_cat, i_br, i_type, i_num = ci("катег"), ci("бренд"), ci("тип"), ci("номер")
            i_pdf = ci("pdf") if ci("pdf") is not None else ci("ссылка")
            i_note = ci("примеч")
            for n, r in enumerate(rows[1:], start=2):
                r = list(r) + [None] * 10
                typ, num = str(r[i_type] or ""), str(r[i_num] or "")
                if "нотифик" in typ.lower() or "ФСБ" in num:
                    continue
                kind = "decl" if "деклар" in typ.lower() else ("cert" if "сертиф" in typ.lower() else None)
                out.append({"src": f"{f.name} / {ws.title} / строка {n}", "category": r[i_cat], "brand": r[i_br],
                            "kind": kind, "num_text": num, "url": r[i_pdf] if i_pdf is not None else None,
                            "note": str(r[i_note] or "") if i_note is not None else ""})
    return out


def parse_num_text(t):
    t = str(t or "")
    m = re.search(r"(ЕАЭС\s*(?:N|№)?\s*[A-ZА-Я]{2}[^,;(]*?\d{2,}(?:/\d{2})?)(?=[\s,;(]|$)", t)
    num = m.group(1).strip() if m else None
    s = re.search(r"\bс\s*" + D, t) or re.search(r"рег\.?\s*" + D, t)
    e = re.search(r"\bпо\s*" + D, t) or re.search(r"до\s*" + D, t)
    return num, s and s.group(1), e and e.group(1)


def download(url, folder: Path):
    import requests
    folder.mkdir(parents=True, exist_ok=True)
    name = hashlib.md5(url.encode()).hexdigest()[:12] + ".pdf"
    p = folder / name
    if p.exists() and p.stat().st_size > 1000:
        return p
    try:
        r = requests.get(url, timeout=60, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200 and r.content[:4] == b"%PDF":
            p.write_bytes(r.content)
            return p
    except Exception:
        pass
    return None


# ---------- сопоставление категорий ----------
def subject_candidates(brand_set, category, wb_rows):
    subj = Counter(r["subject"] for r in wb_rows if norm(r["brand"]) in brand_set)
    cs = stems(category)
    scored = []
    for s, n in subj.items():
        ss = stems(s)
        if not ss or not cs or not (cs & ss):
            continue
        # «да» по умолчанию только при близком совпадении: все слова предмета есть в категории
        # или пересечение ≈ 2/3 («Принтеры» ≠ «Картриджи для принтеров»)
        strict = ss <= cs or len(cs & ss) / len(cs | ss) >= 0.66
        scored.append((1.0 if strict else 0.3, s, n))
    scored.sort(key=lambda x: (-x[0], -x[2]))
    good = [(sc, s, n) for sc, s, n in scored if sc > 0]
    return good or [(0, "(подходящий предмет не найден — впишите название предмета WB и «да»)", 0)]


def load_mapping(path: Path):
    if not path.exists():
        return {}
    import openpyxl
    m = {}
    for r in openpyxl.load_workbook(path, read_only=True).active.iter_rows(min_row=2, values_only=True):
        if r and r[0]:
            m[(str(r[0]), str(r[3]))] = str(r[5] or "").strip().lower() in ("да", "д", "yes", "+", "1")
    return m


def save_mapping(path: Path, rows):
    import openpyxl
    from openpyxl.styles import Font, PatternFill
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Сопоставление"
    ws.append(["Документ (ключ)", "Бренд", "Категория/продукция в документе", "Предмет WB", "Карточек бренда в предмете",
               "Брать (да/нет)", "Номер документа", "Примечание из Excel (на партию? на другую модель?)"])
    for r in rows:
        ws.append(r)
        if r[5] == "да":
            ws.cell(ws.max_row, 6).fill = PatternFill("solid", fgColor="E2EFDA")
    for c in ws[1]:
        c.font = Font(bold=True)
    for col, w in zip("ABCDEFGH", (40, 16, 40, 34, 12, 12, 36, 70)):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    wb.save(path)


# ---------- главное ----------
def collect(folder: Path, wb_by_vc: dict, read_doc, cache, use_gemini, need_statuses, extra_reader):
    """Возвращает (кандидаты {vendorCode: (doc_dict, источник, пометка)}, отчёт-строки)."""
    if not folder.exists():
        return {}, [], 0
    wb_rows = list(wb_by_vc.values())
    by_brand = defaultdict(list)
    for r in wb_rows:
        by_brand[norm(r["brand"])].append(r)
    map_path = folder / "сопоставление_категорий.xlsx"
    chosen = load_mapping(map_path)
    docs = []  # (key, doc, brand_set, category, models, src)

    for x in excel_rows(folder):
        num, s, e = parse_num_text(x["num_text"])
        d = {"type": x["kind"], "number": num, "start": s, "end": e, "how": "Excel"}
        if x["url"] and (not d["number"] or not d["start"] or not d["type"]):
            p = download(str(x["url"]), folder / "_скачано")
            if p:
                pd = read_doc(p, cache, use_gemini)
                for k in ("type", "number", "start", "end"):
                    d[k] = d.get(k) or pd.get(k)
                d["how"] = "Excel + PDF по ссылке"
        d["ok"] = bool(d.get("type") in ("decl", "cert") and d.get("number") and d.get("start"))
        d["note"] = x["note"]
        docs.append((f"{d.get('number') or x['num_text'][:40]}", d, brand_variants(x["brand"]), x["category"], [],
                     x["src"]))

    seen_numbers = set()
    for p in sorted(folder.iterdir()):
        if p.suffix.lower() not in (".pdf",) + IMG or p.name.startswith("~"):
            continue
        d = extra_reader(p, cache, use_gemini)
        key = norm(d.get("number")) or p.name
        if key in seen_numbers:
            continue
        seen_numbers.add(key)
        brand = d.get("brand") or re.split(r"\s+-\s+|_", p.stem)[0]
        docs.append((d.get("number") or p.name, d, brand_variants(brand), d.get("product") or p.stem,
                     d.get("models") or [], f"новые\\{p.name}"))

    cand, report, map_rows = {}, [], []
    for key, d, bset, category, models, src in docs:
        if not d.get("ok"):
            report.append([src, "не прочитан номер/дата/тип", d.get("type"), d.get("number"), d.get("start"), d.get("end"), 0])
            continue
        pool = [r for b in bset for r in by_brand.get(b, [])]
        if not pool:
            report.append([src, "бренда нет на WB", d.get("type"), d.get("number"), d.get("start"), d.get("end"), 0])
            continue
        hit = []
        toks = {norm(m) for m in models if len(norm(m)) >= 4 and re.search(r"\d", m)}
        if toks:
            for r in pool:
                hay = norm(r["title"]) + " " + norm(r["vendorCode"])
                if any(t in hay for t in toks):
                    hit.append((r, "подобрано по модели из документа"))
        if not hit:
            for sc, subj, n in subject_candidates(bset, category, wb_rows):
                k = (key, subj)
                take = chosen.get(k)
                if take is None:
                    take_default = sc >= 0.5
                    map_rows.append([key, ", ".join(sorted(bset)), str(category)[:120], subj, n,
                                     "да" if take_default else "нет", d.get("number"), d.get("note", "")[:300]])
                    continue
                map_rows.append([key, ", ".join(sorted(bset)), str(category)[:120], subj, n, "да" if take else "нет",
                                 d.get("number"), d.get("note", "")[:300]])
                if take:
                    hit += [(r, f"бренд + предмет «{subj}» (сопоставление)") for r in pool if r["subject"] == subj]
        for r, why in hit:
            if r["doc_status"].split(":")[0] in need_statuses:
                cand.setdefault(r["vendorCode"].strip(), (d, src, why))
    if map_rows:
        uniq = {}
        for r in map_rows:
            uniq.setdefault((r[0], r[3]), r)
        save_mapping(map_path, list(uniq.values()))
    return cand, report, len([1 for r in map_rows if (r[0], r[3]) not in chosen])
