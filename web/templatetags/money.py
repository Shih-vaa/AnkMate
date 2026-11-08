"""Template filters for rupee amounts with Indian digit grouping (12,34,567.50)."""
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django import template

from core.money import format_inr

register = template.Library()


def _format(value, places):
    return format_inr(value, places)


@register.filter
def inr(value):
    """Rupees with paise: Rs 1,25,000.50"""
    return _format(value, 2)


@register.filter
def inr0(value):
    """Rupees rounded for headline numbers: Rs 1,25,001"""
    return _format(value, 0)


_ONES = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten", "Eleven", "Twelve",
         "Thirteen", "Fourteen", "Fifteen", "Sixteen", "Seventeen", "Eighteen", "Nineteen"]
_TENS = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]


def _under_thousand(n):
    parts = []
    if n >= 100:
        parts.append(_ONES[n // 100] + " Hundred")
        n %= 100
    if n >= 20:
        parts.append(_TENS[n // 10] + (" " + _ONES[n % 10] if n % 10 else ""))
    elif n:
        parts.append(_ONES[n])
    return " ".join(parts)


def _words(n):
    """Whole number in the Indian system: crore, lakh, thousand."""
    if n == 0:
        return "Zero"
    parts = []
    for size, name in ((10_000_000, "Crore"), (100_000, "Lakh"), (1000, "Thousand")):
        if n >= size:
            parts.append(_words(n // size) + " " + name)
            n %= size
    if n:
        parts.append(_under_thousand(n))
    return " ".join(parts)


@register.filter
def inr_words(value):
    """'Rupees One Thousand Two Hundred and Fifty Paise Only' (used on receipts)."""
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return ""
    rupees, paise = int(abs(amount)), int((abs(amount) * 100) % 100)
    text = f"Rupees {_words(rupees)}"
    if paise:
        text += f" and {_words(paise)} Paise"
    return text + " Only"
