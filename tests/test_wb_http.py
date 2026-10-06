import pytest
from wb_http import backoff_seconds


def test_exponential_without_headers():
    assert [backoff_seconds(a) for a in range(4)] == [6, 12, 24, 48]


def test_capped():
    assert backoff_seconds(10) == 120
    assert backoff_seconds(10, cap=30) == 30


@pytest.mark.parametrize("headers, expected", [
    ({"X-Ratelimit-Retry": "3"}, 3),
    ({"X-Ratelimit-Reset": "15"}, 15),
    ({"Retry-After": "7"}, 7),
    # заголовок WB важнее стандартного
    ({"X-Ratelimit-Retry": "2", "Retry-After": "50"}, 2),
])
def test_uses_wb_headers(headers, expected):
    assert backoff_seconds(5, headers) == expected


def test_header_is_capped_and_has_minimum():
    assert backoff_seconds(0, {"Retry-After": "600"}) == 120
    assert backoff_seconds(0, {"X-Ratelimit-Retry": "0"}) == 1


def test_garbage_header_falls_back_to_backoff():
    assert backoff_seconds(1, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}) == 12
