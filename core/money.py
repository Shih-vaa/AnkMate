"""Rupee amounts written the Indian way (12,34,567.50). Shared by the template filters and by text built in code
(for example the wording of a fee reminder), so an amount reads the same everywhere."""
import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation


def format_inr(value, places=2):
    """'₹1,25,000.50' for places=2 and '₹1,25,001' for places=0. Blank for None or an empty string."""
    if value in (None, ""):
        return ""
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return str(value)
    quant = Decimal(1).scaleb(-places)
    amount = amount.quantize(quant, rounding=ROUND_HALF_UP)
    sign = "-" if amount < 0 else ""
    whole, _, fraction = f"{abs(amount):.{places}f}".partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        whole = re.sub(r"(\d)(?=(\d\d)+$)", r"\1,", head) + "," + tail
    return f"{sign}₹{whole}" + (f".{fraction}" if places else "")
