"""
A money amount written out in words.

Standard on a tax invoice here: the figure can be altered after the paper
leaves you, the words cannot. Written rather than taken from a library
because the currency names have to be the ones a Tanzanian invoice uses --
shillings and cents, dollars and cents -- and because the failure is silent:
an invoice for "fifteen thousand" that should say "fifty thousand" is not a
crash, it is an argument in three months' time.
"""

from decimal import ROUND_HALF_UP, Decimal

ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
TENS = ["-", "-", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
SCALES = ((10 ** 12, "trillion"), (10 ** 9, "billion"),
          (10 ** 6, "million"), (1000, "thousand"))

# What a whole one is called, and what a hundredth of it is called.
CURRENCIES = {
    "TZS": ("shilling", "shillings", "cent", "cents"),
    "USD": ("dollar", "dollars", "cent", "cents"),
    "EUR": ("euro", "euros", "cent", "cents"),
    "GBP": ("pound", "pounds", "penny", "pence"),
    "KES": ("shilling", "shillings", "cent", "cents"),
    "AED": ("dirham", "dirhams", "fils", "fils"),
}


def _under_thousand(number: int) -> str:
    if number < 20:
        return ONES[number]
    if number < 100:
        tens, ones = divmod(number, 10)
        return TENS[tens] + (f"-{ONES[ones]}" if ones else "")
    hundreds, rest = divmod(number, 100)
    said = f"{ONES[hundreds]} hundred"
    # "two hundred and eighty", the way it is read aloud here.
    return said + (f" and {_under_thousand(rest)}" if rest else "")


def in_words(number: int) -> str:
    """A whole number written out. 0 is "zero"; negatives say so."""
    number = int(number)
    if number < 0:
        return "minus " + in_words(-number)
    if number < 1000:
        return _under_thousand(number)

    parts = []
    for size, label in SCALES:
        if number >= size:
            count, number = divmod(number, size)
            parts.append(f"{in_words(count)} {label}")
    if number:
        # "one thousand and fifty", but "one thousand one hundred".
        parts.append(("and " if number < 100 else "") + _under_thousand(number))
    return " ".join(parts)


def money_in_words(amount, currency: str = "TZS") -> str:
    """
    The amount as it is written on an invoice.

    Rounded to the cent first, because the words have to agree with the
    figure printed beside them.
    """
    amount = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    negative = amount < 0
    amount = abs(amount)

    whole = int(amount)
    cents = int((amount - whole) * 100)
    one, many, cent_one, cent_many = CURRENCIES.get(
        (currency or "").upper(), ("unit", "units", "cent", "cents"))

    said = f"{in_words(whole)} {one if whole == 1 else many}"
    if cents:
        said += f" and {in_words(cents)} {cent_one if cents == 1 else cent_many}"
    if negative:
        said = "minus " + said
    return said.capitalize() + " only"
