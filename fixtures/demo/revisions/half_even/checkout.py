"""Demo checkout module, half-even mutant: ties round to the even cent."""


def total_cents(prices_cents: list[int], discount_percent: int) -> int:
    quotient, remainder = divmod(sum(prices_cents) * (100 - discount_percent), 100)
    if remainder > 50 or (remainder == 50 and quotient % 2 == 1):
        quotient += 1
    return quotient
