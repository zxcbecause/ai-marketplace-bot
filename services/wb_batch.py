"""
Пайплайн батч-создания WB-карточек с динамическим шаблоном.

Поток:
1. Если есть локальный шаблон (data/templates/wb/<Категория>.xlsx) — используем его колонки
2. Иначе — запрашиваем характеристики через WB Content API v2
3. Для каждого товара — Exa-поиск + LLM заполняет карточку + характеристики
4. Возвращаем Excel (заполненный шаблон или сгенерированный файл)

Команда: /wb_batch <Субъект WB>
Следующие строки: АРТИКУЛ  Название товара (таб или пробел)
"""
import io
import logging
import re
from pathlib import Path

from services.wb_template import find_template, read_template, fill_template, template_chars  # патч DataValidation — до import openpyxl
from services.wb_content import get_wb_subject_characteristics, search_wb_subjects

import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from services.card import generate_full_card
from utils.billing import save_cost

log = logging.getLogger(__name__)

# Фиксированные колонки WB, всегда идут первыми
_FIXED_COLS = [
    "Артикул продавца",
    "Наименование",
    "Бренд",
    "Предмет",
    "Страна производства",
    "ТНВЭД",
    "Описание",
    "Высота упаковки",
    "Ширина упаковки",
    "Длина упаковки",
    "Вес с упаковкой (кг)",
]

# Единицы, которые убираем из числовых значений (ВБ хранит только число)
_UNIT_RE = re.compile(
    r'^(\d+(?:[.,]\d+)?)\s*'
    r'(?:мм|см|м\b|Вт|W\b|В\b|V\b|ГГц|МГц|Гц|GHz|MHz|Hz\b|мА·?ч|мАч|mAh|мА|mA|'
    r'А\b|кг|г\b|мл|л\b|дБ|dB\b|ГБ|МБ|КБ|ТБ|GB|MB|KB|TB|%|px|пкс)$',
    re.IGNORECASE,
)


def _strip_unit(v: str) -> str:
    m = _UNIT_RE.match(v.strip())
    return m.group(1).replace(",", ".") if m else v


async def fetch_subject_chars(subject: str) -> tuple[list[dict], str, Path | None]:
    """
    Возвращает (chars, canonical_name, template_path).
    template_path — путь к локальному шаблону (если найден), иначе None.

    Порядок: сначала локальный шаблон → потом WB Content API.
    """
    # 1. Ищем локальный шаблон
    tpl_path = find_template(subject)
    if tpl_path:
        col_map, _ = read_template(tpl_path)
        chars = template_chars(col_map)
        canonical = tpl_path.stem  # имя файла без расширения
        log.info(f"fetch_subject_chars: локальный шаблон «{canonical}», {len(chars)} полей")
        return chars, canonical, tpl_path

    # 2. WB Content API
    chars = await get_wb_subject_characteristics(subject)
    if not chars:
        suggestions = await search_wb_subjects(subject)
        names = [s.get("subjectName", "") for s in suggestions[:5] if s.get("subjectName")]
        hint = ", ".join(names) if names else "не найдено"
        raise ValueError(
            f"Субъект «{subject}» не найден в WB.\n"
            f"Похожие: {hint}\n"
            f"Используй точное название категории WB."
        )
    log.info(f"fetch_subject_chars: WB API «{subject}», {len(chars)} характеристик")
    return chars, subject, None


def _build_chars_prompt(subject: str, chars: list[dict]) -> str:
    """Промпт для LLM: заполнить характеристики WB по субъекту."""
    lines = [
        f"Ты заполняешь характеристики карточки товара для Wildberries.",
        f"Категория: {subject}",
        f"",
        f"Заполни следующие характеристики на основе описания товара.",
        f"Формат ответа — построчно: Название характеристики: значение",
        f"Если характеристика неизвестна или неприменима — пропусти её (не пиши).",
        f"Числа без единиц измерения (только цифры).",
        f"",
        f"Характеристики:",
    ]
    for c in chars:
        req = " *" if c.get("required") else ""
        unit = f" ({c['unitName']})" if c.get("unitName") else ""
        lines.append(f"- {c['name']}{unit}{req}")
    lines += [
        "",
        "* — обязательные поля.",
        "",
        "Описание товара:",
    ]
    return "\n".join(lines)


def _parse_chars_response(text: str) -> dict[str, str]:
    result = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        k, v = k.strip().lstrip("-• "), v.strip()
        if k and v:
            result[k] = v
    return result


def _style_header_cell(cell, required: bool = False):
    cell.font = Font(bold=True, size=10,
                     color="FFFFFF" if required else "000000")
    cell.fill = PatternFill("solid", fgColor="C00000" if required else "4472C4")
    cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    thin = Side(style="thin", color="FFFFFF")
    cell.border = Border(left=thin, right=thin, top=thin, bottom=thin)


def build_excel(
    subject: str,
    chars: list[dict],
    rows: list[dict],          # [{article, name, brand, chars_values, context}]
    tnved: str = "",
) -> bytes:
    """Создаёт Excel-файл в формате WB-шаблона и возвращает байты."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = subject[:30]

    # Имена характеристик из API
    char_names = [c["name"] for c in chars]
    required_set = {c["name"] for c in chars if c.get("required")}

    all_cols = _FIXED_COLS + [n for n in char_names if n not in _FIXED_COLS]

    # Строка 1 — заголовок
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(all_cols))
    title_cell = ws.cell(row=1, column=1, value=f"Шаблон WB: {subject}")
    title_cell.font = Font(bold=True, size=12)
    title_cell.fill = PatternFill("solid", fgColor="1F3864")
    title_cell.font = Font(bold=True, size=12, color="FFFFFF")
    title_cell.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 22

    # Строка 2 — заголовки столбцов
    ws.row_dimensions[2].height = 40
    for col_idx, col_name in enumerate(all_cols, 1):
        cell = ws.cell(row=2, column=col_idx, value=col_name)
        _style_header_cell(cell, required=(col_name in required_set))
        # Ширина
        ws.column_dimensions[cell.column_letter].width = max(15, min(40, len(col_name) + 4))

    # Данные — с 3-й строки
    for row_data in rows:
        r_vals = row_data.get("chars_values", {})
        row = []
        for col_name in all_cols:
            if col_name == "Артикул продавца":
                row.append(row_data.get("article", ""))
            elif col_name == "Наименование":
                row.append(row_data.get("name", ""))
            elif col_name == "Бренд":
                row.append(row_data.get("brand", ""))
            elif col_name == "Предмет":
                row.append(subject)
            elif col_name == "ТНВЭД":
                row.append(tnved)
            elif col_name == "Описание":
                row.append(row_data.get("description", ""))
            else:
                val = r_vals.get(col_name, "")
                row.append(_strip_unit(val) if val else "")
        ws.append(row)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


async def process_wb_batch(
    subject: str,
    chars: list[dict],
    lines: list[tuple[str, str]],   # [(article, raw_name), ...]
    llm,
    user_id: int,
    send_text,
    template_path: Path | None = None,
) -> bytes:
    """
    Основной цикл: для каждой строки генерирует карточку и собирает данные.
    Если template_path задан — заполняет локальный шаблон WB.
    """
    chars_prompt_base = _build_chars_prompt(subject, chars)
    total = len(lines)

    if template_path:
        # ── Режим: заполняем готовый WB-шаблон ──
        col_map, _ = read_template(template_path)
        name_set = {v for v in col_map.values()}

        tpl_rows: list[dict[str, str]] = []
        for i, (article, raw) in enumerate(lines, 1):
            await send_text(f"[{i}/{total}] {raw}")
            try:
                # need_characteristics=False (17.07): result.characteristics не
                # используется — ниже свой chars_prompt_base под поля WB-категории.
                result = await generate_full_card(raw, llm, desc_only=False, need_characteristics=False)
                for _ri, resp in enumerate(result.llm_responses):
                    await save_cost(user_id, "wb_batch_card", response=resp,
                                    exa_requests=result.exa_requests if _ri == 0 else 0)

                chars_resp = await llm.chat(result.context, chars_prompt_base)
                await save_cost(user_id, "wb_batch_chars", response=chars_resp)
                chars_values = _parse_chars_response(chars_resp.text)
                pack_values  = _parse_chars_response(result.packaging or "")
                all_values   = {**chars_values, **pack_values}

                row: dict[str, str] = {
                    "Артикул продавца": article,
                    "Наименование":     result.product or raw,
                    "Бренд":            result.brand or "",
                    "Описание":         result.description or "",
                    "Страна производства": all_values.get("Страна производства", "Китай"),
                    "Количество штук в упаковке": "1",
                    "Количество предметов в упаковке": "1",
                }
                # Добавляем все значения LLM, которые есть в шаблоне
                for k, v in all_values.items():
                    if k in name_set and v:
                        row[k] = _strip_unit(v) if v else ""

                tpl_rows.append(row)
            except Exception as e:
                log.error(f"wb_batch [{i}/{total}] {raw}: {e}", exc_info=True)
                await send_text(f"🔴 [{i}/{total}] {raw}: {e}")
                tpl_rows.append({
                    "Артикул продавца": article,
                    "Наименование":     raw,
                })

        return fill_template(template_path, tpl_rows)

    else:
        # ── Режим: генерируем Excel через build_excel (WB API chars) ──
        rows = []
        for i, (article, raw) in enumerate(lines, 1):
            await send_text(f"[{i}/{total}] {raw}")
            try:
                # need_characteristics=False (17.07): result.characteristics не
                # используется — ниже свой chars_prompt_base под поля WB-категории.
                result = await generate_full_card(raw, llm, desc_only=False, need_characteristics=False)
                for _ri, resp in enumerate(result.llm_responses):
                    await save_cost(user_id, "wb_batch_card", response=resp,
                                    exa_requests=result.exa_requests if _ri == 0 else 0)

                chars_resp = await llm.chat(result.context, chars_prompt_base)
                await save_cost(user_id, "wb_batch_chars", response=chars_resp)
                chars_values = _parse_chars_response(chars_resp.text)
                pack_values  = _parse_chars_response(result.packaging or "")

                rows.append({
                    "article":      article,
                    "name":         result.product,
                    "brand":        result.brand or "",
                    "description":  result.description,
                    "chars_values": {**chars_values, **pack_values},
                })
            except Exception as e:
                log.error(f"wb_batch [{i}/{total}] {raw}: {e}", exc_info=True)
                await send_text(f"🔴 [{i}/{total}] {raw}: {e}")
                rows.append({"article": article, "name": raw, "brand": "", "chars_values": {}})

        return build_excel(subject, chars, rows)
