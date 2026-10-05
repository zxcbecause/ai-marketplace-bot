"""Статус документа на карточке WB по полю documents (tools/wb_catalog_snapshot.classify)."""
from wb_catalog_snapshot import classify, status_ru

DECL, CERT = 2, 1


def doc(number="ЕАЭС N RU Д-CN.РА08.В.60419/25", type_=DECL, created="2026-10-01T10:00:00", verdict=None):
    return {"id": "111", "type": type_, "number": number, "createdAt": created, "verdict": verdict or {}}


def test_no_documents():
    assert classify(None)[1] == "none"
    assert classify({"items": []})[1] == "none"


def test_no_documents_but_wb_requires_one():
    assert classify({"items": [], "overallVerdict": {"status": 2}})[1] == "none_blocked"


def test_placeholder_numbers_are_ignored():
    fake = [doc(number="НЕ УКАЗАН"), doc(number="-"), {**doc(), "id": "00000000-1"}]
    assert classify({"items": fake})[1] == "none"


def test_pending_when_no_verdict_yet():
    number, st, _ = classify({"items": [doc()]})
    assert st == "pending"
    assert number == "ЕАЭС N RU Д-CN.РА08.В.60419/25"


def test_verified_when_fresh_overall_verdict_ok():
    docs = {"items": [doc(verdict={"status": 1})],
            "overallVerdict": {"status": 1, "createdAt": "2026-10-01T11:00:00"}}
    assert classify(docs)[1] == "verified"


def test_stale_overall_verdict_is_not_trusted():
    # общий вердикт старше нового документа — новый ещё не проверен
    docs = {"items": [doc(created="2026-10-02T09:00:00")],
            "overallVerdict": {"status": 1, "createdAt": "2026-10-01T11:00:00"}}
    assert classify(docs)[1] == "pending"


def test_rejected_with_reason():
    docs = {"items": [doc(verdict={"status": 2, "reason": "document_dates_mismatch"})]}
    st = classify(docs)[1]
    assert st == "rejected: даты не совпадают с реестром"
    assert status_ru(st) == "Отклонён: даты не совпадают с реестром"


def test_need_second_document():
    docs = {"items": [doc(verdict={"status": 1})],
            "overallVerdict": {"status": 2, "reason": "documents_missing", "createdAt": "2026-10-01T11:00:00"}}
    assert classify(docs)[1] == "need_second"


def test_valid_but_waiting_for_final_check():
    docs = {"items": [doc(verdict={"status": 1}), doc(type_=CERT, verdict={"status": 1})]}
    assert classify(docs)[1] == "valid_wait"
