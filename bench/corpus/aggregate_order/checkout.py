
def total_cents(prices_cents: list[int], discount_percent: int) -> int:
    d = discount_percent
    return sum(prices_cents) - (sum(prices_cents) * d + 50) // 100
