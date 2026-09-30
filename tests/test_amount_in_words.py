"""
The total written out.

The figure on a printed invoice can be altered after the paper leaves you;
the words cannot, which is why the trade prints both. The failure here is
silent -- an invoice reading "fifteen thousand" where it should read "fifty
thousand" is not a crash, it is an argument three months later -- so the
awkward numbers are all pinned.
"""

from decimal import Decimal

import pytest

from apps.core.amounts import in_words, money_in_words

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("number,said", [
    (0, "zero"),
    (7, "seven"),
    (13, "thirteen"),
    (15, "fifteen"),
    (50, "fifty"),
    (19, "nineteen"),
    (90, "ninety"),
    (100, "one hundred"),
    (101, "one hundred and one"),
    (280, "two hundred and eighty"),
    (999, "nine hundred and ninety-nine"),
    (1000, "one thousand"),
    (1050, "one thousand and fifty"),
    (1100, "one thousand one hundred"),
    (15000, "fifteen thousand"),
    (50000, "fifty thousand"),
    (90280, "ninety thousand two hundred and eighty"),
    (153900, "one hundred and fifty-three thousand nine hundred"),
    (1000000, "one million"),
    (1000001, "one million and one"),
])
def test_whole_numbers_read_the_way_they_are_said(number, said):
    assert in_words(number) == said


def test_the_pair_that_matters_most():
    """Fifteen and fifty are the two a listener mishears and a reader must not."""
    assert in_words(15) != in_words(50)
    assert "fifteen" in money_in_words(15000).lower()
    assert "fifty" in money_in_words(50000).lower()


@pytest.mark.parametrize("amount,currency,said", [
    (90280, "TZS", "Ninety thousand two hundred and eighty shillings only"),
    (1, "TZS", "One shilling only"),
    (0, "TZS", "Zero shillings only"),
    (Decimal("95.48"), "USD", "Ninety-five dollars and forty-eight cents only"),
    (Decimal("1.01"), "USD", "One dollar and one cent only"),
    (Decimal("2.50"), "GBP", "Two pounds and fifty pence only"),
])
def test_money_reads_in_its_own_currency(amount, currency, said):
    assert money_in_words(amount, currency) == said


def test_it_rounds_to_agree_with_the_figure_beside_it():
    """The words must not say one thing while the number says another."""
    assert money_in_words(Decimal("10.005"), "USD") == "Ten dollars and one cent only"
    assert money_in_words(Decimal("10.004"), "USD") == "Ten dollars only"


def test_an_unknown_currency_still_says_something_sensible():
    assert money_in_words(5, "XYZ") == "Five units only"
