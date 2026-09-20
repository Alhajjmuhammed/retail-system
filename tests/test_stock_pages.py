"""Stock list, ledger, adjustments, transfers, counts and expiry."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.core.context import tenant_context
from apps.inventory.models import CountStatus, StockCount, StockItem, Transfer, TransferLine
from apps.org.models import Branch

pytestmark = pytest.mark.django_db


@pytest.fixture
def second_branch(shop):
    with tenant_context(shop):
        return Branch.objects.create(tenant=shop, name="Kiosk")


def test_stock_list_counts_and_pages(client, shop, owner, main_branch, stocked):
    client.force_login(owner)
    r = client.get(reverse("inventory:stock_list"), {"page": "x"})
    assert r.status_code == 200
    assert r.context["page"].paginator.count == 3
    assert set(r.context["counts"]) == {"low", "negative", "dead"}


def test_ledger_ignores_unknown_reason(client, owner, stocked):
    client.force_login(owner)
    r = client.get(reverse("inventory:movements"), {"reason": "nope"})
    assert r.status_code == 200 and r.context["reason"] == ""
    assert r.context["page"].paginator.count == 3


def test_adjust_error_keeps_what_was_typed(client, shop, owner, main_branch, stocked):
    with tenant_context(shop):
        item = StockItem.objects.get(branch=main_branch, variant=stocked["Mkate"])
    client.force_login(owner)
    r = client.post(reverse("inventory:stock_adjust", args=[item.pk]), {"new_qty": "90", "reason": ""})
    assert r.status_code == 200 and "reason is required" in r.content.decode()
    assert 'value="90"' in r.content.decode()
    r = client.post(reverse("inventory:stock_adjust", args=[item.pk]),
                    {"new_qty": "100", "reason": "x"})
    assert "already says" in r.content.decode()


def test_transfer_line_picker_checks_stock_and_merges(client, shop, owner, main_branch,
                                                       second_branch, stocked):
    with tenant_context(shop):
        t = Transfer.objects.create(tenant=shop, from_branch=main_branch, to_branch=second_branch,
                                    reference="TR-1")
    client.force_login(owner)
    url = reverse("inventory:transfer_detail", args=[t.pk])
    r = client.get(url)
    assert {i.variant_id for i in r.context["candidates"]} == {v.pk for v in stocked.values()}

    v = stocked["Soda 500ml"]
    client.post(url, {"action": "add_line", "variant": v.pk, "qty": "500"})  # only 100 here
    client.post(url, {"action": "add_line", "variant": "abc", "qty": "1"})
    client.post(url, {"action": "add_line", "variant": v.pk, "qty": "1.5"})  # whole pieces
    with tenant_context(shop):
        assert not TransferLine.objects.filter(transfer=t).exists()
    client.post(url, {"action": "add_line", "variant": v.pk, "qty": "4"})
    client.post(url, {"action": "add_line", "variant": v.pk, "qty": "6"})
    with tenant_context(shop):
        line = TransferLine.objects.get(transfer=t)
        assert line.qty_sent == Decimal("10")


def test_transfer_list_filters(client, shop, owner, main_branch, second_branch):
    with tenant_context(shop):
        Transfer.objects.create(tenant=shop, from_branch=main_branch, to_branch=second_branch,
                                reference="TR-1")
    client.force_login(owner)
    r = client.get(reverse("inventory:transfer_list"), {"status": "sent"})
    assert r.context["page"].paginator.count == 0
    r = client.get(reverse("inventory:transfer_list"), {"status": "bogus", "direction": "out"})
    assert r.context["page"].paginator.count == 1


def test_one_open_count_at_a_time(client, shop, owner, main_branch, stocked):
    client.force_login(owner)
    client.post(reverse("inventory:count_create"))
    r = client.post(reverse("inventory:count_create"))
    with tenant_context(shop):
        assert StockCount.objects.filter(status=CountStatus.OPEN).count() == 1
        count = StockCount.objects.get()
    assert r.url == reverse("inventory:count_detail", args=[count.pk])


def test_apply_marks_only_counted_lines(client, shop, owner, main_branch, stocked):
    client.force_login(owner)
    client.post(reverse("inventory:count_create"))
    with tenant_context(shop):
        count = StockCount.objects.get()
        line = count.lines.get(variant=stocked["Mkate"])
    client.post(reverse("inventory:count_detail", args=[count.pk]),
                {"action": "apply", f"qty:{line.pk}": "98"})
    with tenant_context(shop):
        counted = StockItem.objects.get(branch=main_branch, variant=stocked["Mkate"])
        other = StockItem.objects.get(branch=main_branch, variant=stocked["Soda 500ml"])
        assert counted.qty_on_hand == Decimal("98") and counted.last_counted_at is not None
        assert other.last_counted_at is None


def test_save_with_a_bad_number_says_only_that(client, shop, owner, main_branch, stocked):
    client.force_login(owner)
    client.post(reverse("inventory:count_create"))
    with tenant_context(shop):
        count = StockCount.objects.get()
        line = count.lines.first()
    r = client.post(reverse("inventory:count_detail", args=[count.pk]),
                    {f"qty:{line.pk}": "abc"}, follow=True)
    body = r.content.decode()
    assert "not numbers" in body and "Count saved." not in body


def test_expired_batches_listed_for_this_branch(client, shop, owner, main_branch, stocked):
    from apps.inventory.models import Batch, MovementReason
    from apps.inventory.services import record_movement

    with tenant_context(shop, branch=main_branch, user=owner):
        b = Batch.objects.create(variant=stocked["Mkate"], batch_no="OLD",
                                 expiry_date=timezone.localdate() - timedelta(days=3))
        record_movement(variant=stocked["Mkate"], qty_delta=5, reason=MovementReason.PURCHASE,
                        batch=b, unit_cost=1000)
    client.force_login(owner)
    r = client.get(reverse("inventory:batch_list"))
    assert [x.batch_no for x in r.context["expired"]] == ["OLD"]
    assert "OLD" not in [x.batch_no for x in r.context["batches"]]
