"""Demo checkout module, buggy head: discounts and rounds each line separately."""


def total_cents(prices_cents: list[int], discount_percent: int) -> int:
    total = 0
    for price in prices_cents:
        total += (price * (100 - discount_percent) + 50) // 100
    return total
