"""Demo checkout module, fixed head (refactor that still satisfies checkout-rounding@1)."""


def total_cents(prices_cents: list[int], discount_percent: int) -> int:
    subtotal = 0
    for price in prices_cents:
        subtotal += price
    discounted, remainder = divmod(subtotal * (100 - discount_percent), 100)
    if remainder >= 50:
        discounted += 1
    return discounted
