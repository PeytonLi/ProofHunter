"""Demo checkout module, base revision (satisfies checkout-rounding@1)."""


def total_cents(prices_cents: list[int], discount_percent: int) -> int:
    n = sum(prices_cents) * (100 - discount_percent)
    return (n + 50) // 100
