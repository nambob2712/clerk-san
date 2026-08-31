from __future__ import annotations

import pytest

from clerksan.extract.tax_id import (
    is_valid_registration_number,
    normalize_registration_number,
    registration_check_digit,
)


@pytest.mark.parametrize(
    ("base", "expected"),
    [
        ("700110005901", 8),  # National Tax Agency published calculation example.
        ("000000000000", 9),
        ("123456789012", 7),
        ("999999999999", 9),
        ("100000000001", 6),
    ],
)
def test_registration_check_digit(base: str, expected: int) -> None:
    assert registration_check_digit(base) == expected


@pytest.mark.parametrize(
    "value",
    [
        "T8700110005901",
        "t 8700110005901",
        "Ｔ８７００１１０００５９０１",
        "T7123456789012",
        "T9999999999999",
    ],
)
def test_normalizes_and_validates_registration_numbers(value: str) -> None:
    normalized = normalize_registration_number(value)
    assert normalized is not None
    assert is_valid_registration_number(normalized)


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "T8700110005902",
        "T870011000590",
        "T87001100059010",
        "T870011000590A",
        "T0000000000000",
    ],
)
def test_rejects_malformed_or_corrupted_registration_numbers(value: str | None) -> None:
    assert not is_valid_registration_number(value)


def test_rejects_invalid_base_numbers() -> None:
    with pytest.raises(ValueError, match="12 ASCII digits"):
        registration_check_digit("123")
