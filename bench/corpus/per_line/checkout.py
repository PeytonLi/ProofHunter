
def total_cents(prices_cents: list[int], discount_percent: int) -> int:
    d = discount_percent
    return sum((p * (100 - d) + 50) // 100 for p in prices_cents)
