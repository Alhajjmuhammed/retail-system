"""
The cash-ups page and the till must mean the same thing by "open".

A shift carries both ``closed_at`` and ``status``. ``close_shift`` sets both,
but the cash-ups list used to ask ``closed_at__isnull=True`` while the till
refuses a second shift on a register by ``status=OPEN``. Two questions, two
answers: a drawer could sit in the "still open" list that the till thought was
closed, or the other way round, and nobody could tell which was true.
"""

from decimal import Decimal

import pytest
from django.urls import reverse

from apps.core.context import tenant_context
from apps.pos.models import Shift, ShiftStatus
from apps.pos.services import close_shift, open_shift

pytestmark = pytest.mark.django_db


def test_the_cashups_list_and_the_till_agree_on_what_is_open(
        client, shop, main_branch, register, owner):
    with tenant_context(shop, branch=main_branch, user=owner):
        shift = open_shift(register=register, opening_float=Decimal("5000"))
    client.force_login(owner)

    page = client.get(reverse("finance:cashups"))
    assert shift in list(page.context["open_shifts"])

    with tenant_context(shop, branch=main_branch, user=owner):
        close_shift(shift, counted_cash=Decimal("5000"), note="counted")

    page = client.get(reverse("finance:cashups"))
    assert list(page.context["open_shifts"]) == []

    # And the till agrees: the register is free again.
    with tenant_context(shop, branch=main_branch, user=owner):
        again = open_shift(register=register, opening_float=Decimal("1000"))
        assert again.pk != shift.pk


def test_a_shift_the_till_calls_open_is_listed_as_open(
        client, shop, main_branch, register, owner):
    """The two fields are read the same way, so a mismatch cannot hide a drawer."""
    with tenant_context(shop, branch=main_branch, user=owner):
        shift = open_shift(register=register, opening_float=Decimal("2000"))
        # What the till asks before refusing a second shift.
        assert Shift.objects.filter(register=register, status=ShiftStatus.OPEN).count() == 1
    client.force_login(owner)
    listed = client.get(reverse("finance:cashups")).context["open_shifts"]
    assert [s.pk for s in listed] == [shift.pk]
