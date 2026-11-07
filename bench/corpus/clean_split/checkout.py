
def total_cents(prices_cents: list[int], discount_percent: int) -> int:
    d = discount_percent
    return sum(prices_cents) * (100 - d) // 100 + int(sum(prices_cents) * (100 - d) % 100 >= 50)
