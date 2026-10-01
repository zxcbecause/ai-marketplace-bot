# -*- coding: utf-8 -*-
"""Доп. ступени после создания карточки WB (01.10.2026) — «ступени ракеты»:
каждая необязательна; не получилась — карточка остаётся как есть, батч идёт дальше.

  1. Документ соответствия: если на такой же бренд + предмет у нас уже есть ОДОБРЕННЫЙ документ
     (по последнему снимку data/declarations/_wb_snapshot_*.json), ставим тот же на новую карточку.
  2. Инфографика: бесплатный генератор tools/free_infographic_from_card.py (без LLM, кроме
     редких запасных шагов) → дописываем фото В КОНЕЦ галереи (существующие фото не трогаем).
  3. Rich-контент: у WB нет API для него — картинку готовим и присылаем в Telegram,
     заливается вручную через кабинет (см. гайд, раздел про rich-content).
"""
import asyncio
import json
import logging
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "tools"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

log = logging.getLogger(__name__)
DECL = ROOT / "data" / "declarations"
_index: dict = {}
_index_src: tuple = ()


def _norm(s):
    return re.sub(r"[^0-9a-zа-яё]", "", str(s or "").lower())


def _iso_to_ru(s):
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(s or ""))
    return f"{m.group(3)}.{m.group(2)}.{m.group(1)}" if m else None


def _load_index():
    """(бренд, предмет) -> Counter{(тип, номер, начало, окончание)} по одобренным карточкам снимка."""
    global _index, _index_src
    snaps = [s for s in DECL.glob("_wb_snapshot_*.json") if not s.stem.endswith(("_samples", "_summary"))]
    if not snaps:
        return {}
    snap = max(snaps, key=lambda p: p.stat().st_mtime)
    key = (str(snap), snap.stat().st_mtime)
    if key == _index_src:
        return _index
    from wb_catalog_snapshot import classify
    idx = defaultdict(Counter)
    for r in json.loads(snap.read_text(encoding="utf-8")):
        if classify(r.get("documents"))[1] != "verified":
            continue
        for it in (r.get("documents") or {}).get("items") or []:
            v = it.get("verdict") or {}
            if it.get("type") in (1, 2) and v.get("status") == 1 and re.search(r"\d", it.get("number") or ""):
                end = None if it.get("isEndless") else _iso_to_ru(it.get("endDate"))
                idx[(_norm(r["brand"]), r["subject"])][
                    ("decl" if it["type"] == 2 else "cert", it["number"], _iso_to_ru(it.get("startDate")), end)] += 1
    _index, _index_src = idx, key
    return idx


def known_document(brand: str, subject: str):
    c = _load_index().get((_norm(brand), subject))
    if not c:
        return None
    (kind, number, start, end), n = c.most_common(1)[0]
    if not start:
        return None
    if end and datetime.strptime(end, "%d.%m.%Y") < datetime.now():
        return None
    return {"type": kind, "number": number, "start": start, "end": end, "used_on": n}


def _attach_sync(nm_id: int, doc: dict) -> str:
    import wb_docs_upload as up
    x = {"row": 0, "key": str(nm_id), "type": doc["type"], "number": doc["number"], "start": doc["start"],
         "end": doc["end"], "tnved": ""}
    p = up.plan_row(x, False)
    if p["status"] != "к отправке":
        return p["status"]
    r = up.wb("POST", "/content/v2/cards/update", json=[p["payload"]])
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code not in (200, 204) or body.get("error"):
        return f"WB отклонил: {(body.get('errorText') or r.text)[:150]}"
    return "отправлен на проверку"


async def attach_document(nm_id: int, brand: str, subject: str) -> str:
    doc = known_document(brand, subject)
    if not doc:
        return "нет одобренного документа на этот бренд + предмет — добавить вручную"
    res = await asyncio.to_thread(_attach_sync, nm_id, doc)
    return f"{doc['number']} (уже одобрен на {doc['used_on']} карт.) — {res}"


def _append_photo_sync(nm_id: int, img: bytes) -> str:
    from services.wb_content import _find_card, _headers, BASE
    import requests
    card = _find_card(str(nm_id))
    n = len((card or {}).get("photos") or [])
    h = _headers()
    h.pop("Content-Type", None)
    h["X-Nm-Id"] = str(nm_id)
    h["X-Photo-Number"] = str(n + 1)  # строго в конец — существующие фото не перезаписываем
    r = requests.post(f"{BASE}/content/v3/media/file", headers=h,
                      files={"uploadfile": ("infographic.jpg", img, "image/jpeg")}, timeout=90)
    return "добавлена в конец галереи" if r.status_code == 200 else f"WB HTTP {r.status_code}: {r.text[:120]}"


async def make_and_upload_infographic(vendor_code: str, nm_id: int, upload: bool = True) -> dict:
    from free_infographic_from_card import build_free_infographic
    out = await build_free_infographic(vendor_code)
    res = {"files": out}
    if upload and out.get("infographic"):
        res["upload"] = await asyncio.to_thread(_append_photo_sync, nm_id, Path(out["infographic"]).read_bytes())
    return res


async def post_create(created: list[dict], send_text, send_photo=None, wait_sec: int = 120,
                      infographic: bool = True) -> None:
    """created: [{article, nm_id, subject, brand}] — ступени после батча."""
    if not created:
        return
    await send_text(f"Доп. шаги для {len(created)} новых карточек (документ, инфографика) — жду {wait_sec} с, "
                    f"пока WB проиндексирует карточки…")
    await asyncio.sleep(wait_sec)
    stats = Counter()
    from services.wb_content import _find_card
    for c in created:
        lines = [f"<b>{c['article']}</b> (nmID {c['nm_id']}):"]
        try:
            card = await asyncio.to_thread(_find_card, str(c["nm_id"]))
            if card:
                c["brand"], c["subject"] = card.get("brand") or c.get("brand"), card.get("subjectName") or c.get("subject")
                c["article"] = card.get("vendorCode") or c["article"]
        except Exception as e:
            log.warning(f"post_create find {c['nm_id']}: {e}")
        try:
            r = await attach_document(c["nm_id"], c.get("brand") or "", c.get("subject") or "")
            stats["doc_ok" if "отправлен" in r else "doc_no"] += 1
            lines.append(f"📄 документ: {r}")
        except Exception as e:
            stats["doc_no"] += 1
            lines.append(f"📄 документ: пропущен ({str(e)[:120]})")
        if infographic:
            try:
                r = await make_and_upload_infographic(c["article"], c["nm_id"])
                up = r.get("upload")
                stats["inf_ok" if up and "добавлена" in up else "inf_no"] += 1
                lines.append(f"🖼 инфографика: {up or 'не сгенерирована'}")
                if send_photo and r["files"].get("richcontent"):
                    await send_photo(r["files"]["richcontent"],
                                     f"Rich-контент для {c['article']} — залить вручную в кабинете WB")
            except Exception as e:
                stats["inf_no"] += 1
                lines.append(f"🖼 инфографика: пропущена ({str(e)[:120]})")
        try:
            await send_text("\n".join(lines))
        except Exception as e:
            log.warning(f"post_create send: {e}")
        await asyncio.sleep(1)
    await send_text(f"Доп. шаги готовы: документ поставлен {stats['doc_ok']}, без документа {stats['doc_no']}; "
                    f"инфографика {stats['inf_ok']}, без инфографики {stats['inf_no']}.")
