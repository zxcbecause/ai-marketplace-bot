# -*- coding: utf-8 -*-
"""14.09.2026 — автозагрузка сертификата качества в Ozon seller одной
командой, без ручных кликов по веб-форме (раньше на один бренд уходило
15+ ручных шагов в кабинете).

У Ozon НЕТ публичного API для раздела «Сертификаты качества» — раздел
только веб-формой, поэтому здесь используется Playwright поверх реального
Chrome с отдельным постоянным профилем (логинимся один раз через `login`,
дальше сессия просто переиспользуется).

PDF-сертификаты в архиве Сертификаты.zip — СКАНЫ БЕЗ ТЕКСТОВОГО СЛОЯ
(pymupdf возвращает 0 символов) — надёжно распарсить тип документа/номер/
даты скриптом без OCR нельзя, риск ошибки в юридическом рег.номере слишком
дорогой. Поэтому номер и даты документа сверяются вручную и передаются
сюда явными флагами. Экономится не
чтение PDF, а весь дорогой цикл скриншот→клик→скриншot по форме Ozon
(было ~15+ шагов на один бренд).

ВАЖНО про --reg-number: копировать номер вручную с ВЕРНОЙ кириллицей
(ЕАЭС, Д, В, С — все кириллические), не как есть из автоматической
экстракции PDF-текста (там штампуется смесь похожих латинских букв —
именно это 14.09 давало ошибку валидации «Введите корректный номер»).

КРИТИЧНО про --doc-type certificate (найдено 14.09, кейс Philips пылесосы):
поле «Регистрационный номер» под типом «Сертификат соответствия» НЕ
ПРИНИМАЕТ ни один проверенный вариант написания валидного ЕАЭС-номера
сертификата (перепробованы кириллица/латиница/с «ЕАЭС N»/без — всё
отклоняется клиентской валидацией «Введите корректный номер»). Это похоже
на баг/недоделку самого Ozon, не опечатку. Под типом «Декларация о
соответствии» тот же номер (с заменой Д на С в письме) ПРОХОДИТ клиентскую
валидацию, но реальный документ ОТКЛОНЯЕТСЯ на проверке реестра («Не
найден» — потому что он и правда сертификат, а не декларация). Короче:
на сегодня рабочего пути загрузить настоящий «Сертификат соответствия»
через эту форму НЕ НАЙДЕНО. Скрипт при --doc-type certificate требует явный
--force-certificate-type и всё равно предупреждает — не трать на это
прогон вслепую, скорее всего он не пройдёт.

Использование:
    # один раз (откроется окно браузера — залогиниться руками в кабинет продавца):
    python tools/ozon_cert_upload.py login

    # проверить, нет ли уже такого номера в аккаунте (без изменений):
    python tools/ozon_cert_upload.py status --reg-number "ЕАЭС N RU Д-XX.XXXX.X.00000/25"

    # дальше на каждый бренд/категорию:
    python tools/ozon_cert_upload.py submit ^
        --brand Soundcore --ozon-category "Наушники и гарнитуры" ^
        --exclude-category "Акустика и колонки" ^
        --doc-type declaration --standard EAEU --country Россия ^
        --reg-number "ЕАЭС N RU Д-XX.XXXX.X.00000/25" ^
        --issued 01.01.2025 --expires 31.12.2029 ^
        --name "Декларация о соответствии (Soundcore, Наушники и гарнитуры)" ^
        --submit

Без --submit скрипт доходит до превью списка товаров, делает скриншот
(data/temp/ozon_cert_preview_<бренд>.png) и ОСТАНАВЛИВАЕТСЯ, ничего не
отправляя (черновик у Ozon не сохраняется — это просто проверка перед
реальным прогоном, при следующем запуске с --submit форму придётся
заполнить заново).

С --submit скрипт ПОСЛЕ отправки сам открывает список сертификатов, находит
только что созданный документ по рег.номеру и читает его реальный статус
(«Одобрен»/«Отклонён»/«Ожидает проверки») — раньше (см. кейс Philips 14.09,
когда это делалось руками) скрипт бы просто напечатал «Отправлено» и на
этом остановился, а по факту документ был отклонён реестром. Если статус
«Отклонён» — печатает текст причины и НЕ удаляет черновик сам (если не
передан --auto-delete-rejected), чтобы решение оставалось за оператором.

Перед отправкой скрипт также:
  - проверяет, нет ли уже документа с таким же --reg-number в аккаунте
    (см. живые дубли в самом аккаунте 14.09 — не паниковать, но
    подтверждать явно через --allow-duplicate, если это осознанная доливка
    товаров в существующий документ, а не случайное повторение);
  - для деклараций/сертификатов (не refusal) проверяет --expires — если
    до истечения меньше 90 дней, требует --force-near-expiry (см.
    отбракованный кейс Thermaltake 11.09 — рискованно привязывать товары
    к почти истёкшему документу).

Тип документа "отказное письмо" (--doc-type refusal, нужен --pdf <путь>)
реализован по памяти о флоу 11.09 (файл вместо реестровой проверки,
чекбокс «Бессрочно»), но НЕ протестирован в этой сессии — считать
экспериментальным, проверить на одном кейсе перед массовым использованием.

Ничего в этом файле не запускалось против живого аккаунта после правок
14.09 (сессии) — перед массовым использованием прогнать --submit БЕЗ
финального флага на одном реальном бренде и сверить превью глазами.
"""
import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

from tools.ozon_cert_lib import days_until, get_brand_skus, write_binding_template

ROOT = Path(__file__).resolve().parent.parent
PROFILE_DIR = ROOT / "data" / ".ozon_playwright_profile"
TEMP_DIR = ROOT / "data" / "temp"
CERT_LIST_URL = "https://seller.ozon.ru/app/products/certificates"
CERT_URL = f"{CERT_LIST_URL}/add"

DOC_TYPE_LABELS = {
    "declaration": "Декларация о соответствии",
    "certificate": "Сертификат соответствия",
    "refusal": "Отказное письмо",
}
STANDARD_LABELS = {"EAEU": "ЕАЭС", "NATIONAL": "Национальный"}
NEAR_EXPIRY_DAYS = 90


def cmd_login(args):
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    timeout_s = args.timeout
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE_DIR), channel="chrome", headless=False,
            viewport={"width": 1440, "height": 900},
        )
        page = ctx.new_page()
        page.goto("https://seller.ozon.ru/app/products/certificates")
        print(f"Открылось окно Chrome — залогинься в кабинет продавца там. Жду до {timeout_s}с...")
        deadline = time.time() + timeout_s
        logged_in = False
        while time.time() < deadline:
            try:
                if "id.ozon.ru" not in page.url and page.get_by_text(os.getenv("OZON_SHOP_NAME", "Товары"), exact=False).count() > 0:
                    logged_in = True
                    break
            except Exception:
                pass
            page.wait_for_timeout(3000)
        ctx.close()
    if logged_in:
        print("Залогинен, сессия сохранена в", PROFILE_DIR)
    else:
        print(f"Не дождался логина за {timeout_s}с — профиль в {PROFILE_DIR} мог остаться"
              " незалогиненным, перезапусти login при необходимости.")


def _select_combobox(page, index: int, option_text: str, type_first: bool = False):
    inputs = page.locator("input:visible")
    box = inputs.nth(index)
    box.click()
    if type_first:
        box.type(option_text[:4])
    page.get_by_text(option_text, exact=True).first.click()


def _open_context(headless: bool):
    if not PROFILE_DIR.exists():
        sys.exit("Профиль браузера не найден — сначала: python tools/ozon_cert_upload.py login")
    p = sync_playwright().start()
    ctx = p.chromium.launch_persistent_context(
        str(PROFILE_DIR), channel="chrome", headless=headless,
        viewport={"width": 1440, "height": 900}, accept_downloads=True,
    )
    return p, ctx


def find_certificate_row(page, reg_number: str) -> dict | None:
    """Ищет строку в списке сертификатов по точному рег.номеру (первая
    колонка таблицы). Возвращает {status, approved_count, url} или None,
    если такого номера ещё нет в аккаунте. Использовать И перед отправкой
    (проверка дублей), И после (проверка реального статуса)."""
    page.goto(CERT_LIST_URL, wait_until="networkidle")
    search = page.locator('input[placeholder="Поиск"]')
    if search.count() > 0:
        search.first.fill(reg_number)
        page.wait_for_timeout(600)
    row = page.locator("tbody tr", has_text=reg_number).first
    if row.count() == 0:
        return None
    link = row.locator("a").first
    href = link.get_attribute("href") if link.count() > 0 else None
    status_text = row.inner_text()
    return {
        "status_row_text": status_text,
        "url": f"https://seller.ozon.ru{href}" if href else None,
    }


def read_certificate_status(page, url: str) -> dict:
    """Открывает детальную страницу документа, возвращает {status, reason}.
    reason заполняется только если статус «Отклонён» (текст из панели
    справа, например «Не найден. Постановление Правительства РФ...» — см.
    кейс Philips 14.09)."""
    page.goto(url, wait_until="networkidle")
    status = ""
    for candidate in ("Одобрен", "Отклонён", "Ожидает проверки", "На проверке"):
        if page.get_by_text(candidate, exact=True).count() > 0:
            status = candidate
            break
    reason = ""
    if status == "Отклонён":
        reason_block = page.locator("text=Не найден").locator("..")
        if reason_block.count() > 0:
            reason = reason_block.first.inner_text()
    return {"status": status, "reason": reason}


def cmd_status(args):
    p, ctx = _open_context(headless=args.headless)
    try:
        page = ctx.new_page()
        row = find_certificate_row(page, args.reg_number)
        if row is None:
            print(f"Номер {args.reg_number!r} в аккаунте не найден.")
            return
        print("Найдена строка:", row["status_row_text"].replace("\n", " | "))
        if row["url"]:
            detail = read_certificate_status(page, row["url"])
            print("Статус:", detail["status"])
            if detail["reason"]:
                print("Причина отклонения:", detail["reason"])
    finally:
        ctx.close()
        p.stop()


def cmd_submit(args):
    if args.doc_type == "refusal" and not args.pdf:
        sys.exit("--pdf обязателен для --doc-type refusal")

    if args.doc_type == "certificate" and not args.force_certificate_type:
        sys.exit(
            "--doc-type certificate: на 14.09 НИ ОДИН вариант рег.номера не "
            "прошёл клиентскую валидацию Ozon под этим типом (см. докстринг "
            "файла, кейс Philips). Если всё же хочешь попробовать — передай "
            "--force-certificate-type осознанно, но ожидай отказа."
        )

    if args.doc_type != "refusal" and args.expires:
        remaining = days_until(args.expires)
        if remaining < NEAR_EXPIRY_DAYS and not args.force_near_expiry:
            sys.exit(
                f"--expires {args.expires} истекает через {remaining} дн. "
                f"(< {NEAR_EXPIRY_DAYS}) — см. отбракованный кейс Thermaltake "
                "11.09. Если осознанно продолжаешь — добавь --force-near-expiry."
            )

    print(f"Сверяю SKU: бренд={args.brand!r} категория={args.ozon_category!r} "
          f"исключить={args.exclude_category!r}")
    skus = get_brand_skus(
        args.brand, ozon_category=args.ozon_category,
        exclude_categories=args.exclude_category or [],
    )
    articles = [r["article"] for r in skus]
    if not articles:
        sys.exit("Не нашёл ни одного SKU под этот бренд/категорию — проверь названия")
    print(f"Найдено {len(articles)} SKU")

    p, ctx = _open_context(headless=args.headless)
    try:
        page = ctx.new_page()

        existing = find_certificate_row(page, args.reg_number)
        if existing is not None and not args.allow_duplicate:
            sys.exit(
                f"Номер {args.reg_number!r} уже есть в аккаунте "
                f"({existing['status_row_text'].splitlines()[0]!r}). Если это "
                "осознанная доливка товаров в существующий документ (а не "
                "случайный повтор) — добавь --allow-duplicate и привяжи товары "
                "вручную через 'Привязанные товары' на его странице, этот "
                "скрипт умеет только создавать НОВЫЙ документ."
            )

        page.goto(CERT_URL, wait_until="networkidle")

        if "id.ozon.ru" in page.url or "login" in page.url.lower():
            sys.exit("Сессия истекла — перелогинься: python tools/ozon_cert_upload.py login")

        # --- Шаг 1: информация о документе ---
        _select_combobox(page, 0, DOC_TYPE_LABELS[args.doc_type])
        page.wait_for_timeout(400)

        if args.doc_type != "refusal":
            _select_combobox(page, 1, args.country, type_first=True)
            page.wait_for_timeout(300)
            _select_combobox(page, 2, STANDARD_LABELS[args.standard])
            page.wait_for_timeout(300)
            name_idx, reg_idx, issued_idx, expires_idx = 3, 4, 5, 6
        else:
            name_idx, reg_idx, issued_idx, expires_idx = 1, 2, 3, None

        inputs = page.locator("input:visible")
        inputs.nth(name_idx).fill(args.name)
        inputs.nth(reg_idx).fill(args.reg_number)
        inputs.nth(issued_idx).click()
        inputs.nth(issued_idx).type(args.issued)
        page.keyboard.press("Escape")

        if expires_idx is not None:
            inputs.nth(expires_idx).click()
            inputs.nth(expires_idx).type(args.expires)
            page.keyboard.press("Escape")
        elif args.perpetual:
            page.get_by_text("Бессрочно", exact=False).first.click()

        if args.doc_type == "refusal":
            page.get_by_text("Загрузить файлы", exact=False).first.click()
            page.wait_for_timeout(300)
            file_input = page.locator('input[type="file"]').first
            file_input.set_input_files(str(Path(args.pdf).resolve()))
            page.wait_for_timeout(1000)

        reg_error = page.get_by_text("Введите корректный номер", exact=False)
        if reg_error.count() > 0:
            sys.exit("Ozon отклонил регистрационный номер — проверь кириллицу в --reg-number "
                     "(или это тип 'certificate', см. докстринг файла)")

        page.get_by_role("button", name="Далее").click()
        page.wait_for_timeout(1500)

        # --- Шаг 2: товары ---
        page.get_by_role("button", name="Добавить через шаблон").click()
        page.wait_for_timeout(500)

        with page.expect_download() as dl_info:
            page.get_by_role("button", name="Скачать шаблон").click()
        download = dl_info.value
        template_path = TEMP_DIR / f"_ozon_template_raw_{args.brand}.xlsx"
        download.save_as(str(template_path))

        filled_path = TEMP_DIR / f"_ozon_template_filled_{args.brand}.xlsx"
        write_binding_template(template_path, articles, filled_path)

        modal_file_input = page.locator('input[type="file"]').last
        modal_file_input.set_input_files(str(filled_path))
        page.wait_for_timeout(800)
        page.get_by_role("button", name="Добавить", exact=True).click()
        page.wait_for_timeout(1500)
        # иногда модалка перерисовывается и требует повторного клика
        add_btn = page.get_by_role("button", name="Добавить", exact=True)
        if add_btn.count() > 0 and add_btn.first.is_visible():
            add_btn.first.click()
            page.wait_for_timeout(1000)

        rows = page.locator("tbody tr")
        try:
            page.wait_for_function(
                "document.querySelectorAll('tbody tr').length > 0", timeout=8000)
        except PWTimeout:
            pass
        row_count = rows.count()
        print(f"В превью {row_count} строк (ожидалось {len(articles)})")

        preview_path = TEMP_DIR / f"ozon_cert_preview_{args.brand}.png"
        page.screenshot(path=str(preview_path), full_page=True)
        print("Скриншот превью:", preview_path)

        if row_count != len(articles):
            sys.exit("!!! Количество не совпадает — ПРОВЕРЬ ВРУЧНУЮ, не отправляю на проверку.")

        if not args.submit:
            print("Dry-run (без --submit) — не отправляю. Черновик у Ozon не сохранён,"
                  " при следующем запуске форму нужно будет заполнить заново.")
            return

        page.get_by_role("button", name="Отправить на проверку").click()
        page.wait_for_timeout(2000)
        print("Отправлено, проверяю реальный статус...")

        # --- Пост-проверка: НЕ доверяем факту отправки как успеху (см. кейс
        # Philips 14.09 — форма охотно принимает то, что реестр потом
        # отклоняет). Даём Ozon пару секунд на инстант-проверку по реестру
        # ЕАЭС (декларации/сертификаты обычно решаются мгновенно, не в
        # очереди на ручную модерацию).
        page.wait_for_timeout(2000)
        found = find_certificate_row(page, args.reg_number)
        if found is None or not found.get("url"):
            print("!!! Не нашёл документ в списке после отправки — проверь вручную в Ozon seller.")
            return
        detail = read_certificate_status(page, found["url"])
        print("Статус:", detail["status"])
        if detail["status"] == "Отклонён":
            print("Причина:", detail["reason"] or "(текст причины не найден на странице)")
            if args.auto_delete_rejected:
                del_btn = page.get_by_role("button", name="Удалить")
                if del_btn.count() > 0:
                    del_btn.first.click()
                    page.wait_for_timeout(500)
                    confirm = page.get_by_role("button", name="Да, удалить")
                    if confirm.count() > 0:
                        confirm.first.click()
                        page.wait_for_timeout(1000)
                        print("Отклонённый черновик удалён.")
            else:
                print("Черновик НЕ удалён (передай --auto-delete-rejected, если хочешь"
                      " автоочистку) — реши вручную: другой тип документа/номер/бренд.")
        elif detail["status"] == "Одобрен":
            print(f"Готово: {len(articles)} SKU привязаны и одобрены.")
        else:
            print("Документ ушёл на модерацию (не мгновенное решение) — это необычно"
                  " для ЕАЭС-номеров, проверь вручную через пару минут.")
    finally:
        ctx.close()
        p.stop()


def main():
    parser = argparse.ArgumentParser(description="Автозагрузка сертификата в Ozon seller")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_login = sub.add_parser("login", help="Разовый логин, сохраняет сессию в постоянный профиль")
    p_login.add_argument("--timeout", type=int, default=300, help="Сколько секунд ждать логина")

    p_submit = sub.add_parser("submit", help="Загрузить документ + привязать товары")
    p_submit.add_argument("--brand", required=True, help="Бренд как в экспорте Ozon (колонка Бренд)")
    p_submit.add_argument("--ozon-category", required=True, help="Точное название категории Ozon")
    p_submit.add_argument("--exclude-category", action="append", default=[],
                           help="Категория для исключения (можно указать несколько раз)")
    p_submit.add_argument("--doc-type", choices=list(DOC_TYPE_LABELS), default="declaration")
    p_submit.add_argument("--standard", choices=list(STANDARD_LABELS), default="EAEU")
    p_submit.add_argument("--country", default="Россия")
    p_submit.add_argument("--name", required=True, help='"Тип документа (Бренд, Категория)"')
    p_submit.add_argument("--reg-number", required=True)
    p_submit.add_argument("--issued", required=True, help="дд.мм.гггг")
    p_submit.add_argument("--expires", help="дд.мм.гггг (не нужно для refusal)")
    p_submit.add_argument("--perpetual", action="store_true", help="Бессрочно (только refusal)")
    p_submit.add_argument("--pdf", help="Путь к PDF (обязателен для --doc-type refusal)")
    p_submit.add_argument("--submit", action="store_true",
                           help="Реально нажать «Отправить на проверку» (без флага — dry-run)")
    p_submit.add_argument("--allow-duplicate", action="store_true",
                           help="Разрешить создание, даже если номер уже есть в аккаунте")
    p_submit.add_argument("--force-near-expiry", action="store_true",
                           help=f"Разрешить, даже если до истечения < {NEAR_EXPIRY_DAYS} дн.")
    p_submit.add_argument("--force-certificate-type", action="store_true",
                           help="Разрешить --doc-type certificate (см. предупреждение в докстринге)")
    p_submit.add_argument("--auto-delete-rejected", action="store_true",
                           help="Если после отправки статус «Отклонён» — сразу удалить черновик")
    p_submit.add_argument("--headless", action="store_true")

    p_status = sub.add_parser("status", help="Проверить статус документа по рег.номеру, без изменений")
    p_status.add_argument("--reg-number", required=True)
    p_status.add_argument("--headless", action="store_true")

    args = parser.parse_args()
    if args.cmd == "login":
        cmd_login(args)
    elif args.cmd == "status":
        cmd_status(args)
    else:
        cmd_submit(args)


if __name__ == "__main__":
    main()
