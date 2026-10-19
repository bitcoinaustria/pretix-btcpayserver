"""Organizer-funded reduction of pretix's payment-method fee, in the event currency."""
from decimal import ROUND_HALF_UP, Decimal


def subsidized_fee(price: Decimal, fee: Decimal, percent: Decimal, places: int) -> Decimal:
    if not percent or price <= 0:
        return fee
    unit = Decimal(1).scaleb(-places)
    discount = (price * percent / 100).quantize(unit, rounding=ROUND_HALF_UP)
    # Reserve two extra units for pretix's later gross/net rounding (supported tax rates 0–100%).
    # This must not depend on current event settings: existing orders retain their own rounding mode.
    # Cap the floor by the original price so the subsidy cannot increase a tiny balance.
    return max(fee - discount, -price + min(price, 3 * unit))
