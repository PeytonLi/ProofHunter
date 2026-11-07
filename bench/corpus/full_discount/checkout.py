
def total_cents(prices_cents: list[int], discount_percent: int) -> int:
    d = discount_percent
    return sum(prices_cents) if d == 100 else (sum(prices_cents) * (100 - d) + 50) // 100
