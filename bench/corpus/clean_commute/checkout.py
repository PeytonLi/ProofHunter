
def total_cents(prices_cents: list[int], discount_percent: int) -> int:
    d = discount_percent
    return (50 + (100 - d) * sum(prices_cents)) // 100
