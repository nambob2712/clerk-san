"""Offline validation for Japanese qualified-invoice registration numbers."""

from __future__ import annotations

import unicodedata


def normalize_registration_number(value: str | None) -> str | None:
    """Return canonical ``T`` plus 13 ASCII digits, or ``None`` when malformed."""
    if not value:
        return None
    normalized = unicodedata.normalize("NFKC", value).upper()
    compact = "".join(character for character in normalized if character not in " -　\t\n")
    if compact.startswith("T"):
        compact = compact[1:]
    if len(compact) != 13 or not compact.isascii() or not compact.isdigit():
        return None
    return f"T{compact}"


def registration_check_digit(base_number: str) -> int:
    """Calculate the Japanese corporate-number check digit for 12 base digits."""
    if len(base_number) != 12 or not base_number.isascii() or not base_number.isdigit():
        raise ValueError("base_number must contain exactly 12 ASCII digits")
    reversed_digits = [int(digit) for digit in reversed(base_number)]
    odd_position_sum = sum(reversed_digits[::2])
    even_position_sum = sum(reversed_digits[1::2])
    remainder = (odd_position_sum + even_position_sum * 2) % 9
    return 9 - remainder if remainder else 9


def is_valid_registration_number(value: str | None) -> bool:
    """Validate a normalized T-number using the corporate-number modulus-9 rule."""
    normalized = normalize_registration_number(value)
    if normalized is None:
        return False
    digits = normalized[1:]
    return int(digits[0]) == registration_check_digit(digits[1:])
