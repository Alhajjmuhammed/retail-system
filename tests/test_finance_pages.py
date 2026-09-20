"""Expenses and cash-ups."""

from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from apps.core.context import tenant_context
from apps.finance.models import Expense, ExpenseCategory
from apps.pos.models import CashMovement
from apps.pos.services import open_shift

pytestmark = pytest.mark.django_db
HX = {"HTTP_HX_REQUEST": "true"}


@pytest.fixture
def rent(shop):
    with tenant_context(shop):
        return ExpenseCategory.objects.create(name="Rent")


def test_missing_category_is_a_form_error_not_a_404(client, owner, shop, main_branch):
    client.force_login(owner)
    r = client.post(reverse("finance:expense_create"), {"amount": "100", "category": "999"}, **HX)
    assert r.status_code == 200 and "Choose a category" in r.content.decode()


def test_new_category_typed_in_and_future_date_refused(client, owner, shop, main_branch):
    client.force_login(owner)
    tomorrow = (timezone.localdate() + timezone.timedelta(days=1)).isoformat()
    r = client.post(reverse("finance:expense_create"),
                    {"amount": "100", "category_name": "Transport", "spent_at": tomorrow}, **HX)
    assert "in the future" in r.content.decode()
    r = client.post(reverse("finance:expense_create"),
                    {"amount": "100", "category_name": "Transport", "method": "bank"}, **HX)
    assert r.status_code == 204
    with tenant_context(shop):
        e = Expense.objects.get()
        assert e.category.name == "Transport" and e.method == "bank"


def test_cash_from_the_drawer_lowers_expected_cash(client, owner, shop, main_branch, register, rent):
    with tenant_context(shop, branch=main_branch, user=owner):
        shift = open_shift(register=register, opening_float=10000)
    client.force_login(owner)
    r = client.post(reverse("finance:expense_create"),
                    {"amount": "50000", "category": rent.pk, "from_drawer": "on"}, **HX)
    assert "should only hold" in r.content.decode()
    client.post(reverse("finance:expense_create"),
                {"amount": "3000", "category": rent.pk, "method": "cash", "from_drawer": "on"}, **HX)
    with tenant_context(shop):
        assert CashMovement.objects.get(shift=shift).amount == Decimal("-3000")
        assert shift.compute_expected_cash() == Decimal("7000")


def test_receipt_is_private_and_type_checked(client, owner, shop, main_branch, rent, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "public"
    settings.PRIVATE_MEDIA_ROOT = tmp_path / "private"
    client.force_login(owner)
    bad = SimpleUploadedFile("x.exe", b"MZ", content_type="application/octet-stream")
    r = client.post(reverse("finance:expense_create"),
                    {"amount": "10", "category": rent.pk, "attachment": bad}, **HX)
    assert "must be a photo" in r.content.decode()
    good = SimpleUploadedFile("slip.png", b"\x89PNG\r\n", content_type="image/png")
    client.post(reverse("finance:expense_create"),
                {"amount": "10", "category": rent.pk, "attachment": good}, **HX)
    with tenant_context(shop):
        e = Expense.objects.get()
        assert "slip" not in e.attachment.name and str(shop.pk) in e.attachment.name
        # Stored outside the folder nginx serves, with no public address.
        assert (tmp_path / "private" / e.attachment.name).exists()
        assert not (tmp_path / "public").exists()
        with pytest.raises(ValueError):
            _ = e.attachment.url
    r = client.get(reverse("finance:expense_attachment", args=[e.pk]))
    assert r.status_code == 200 and r["Content-Type"] == "image/png"
    client.logout()
    assert client.get(reverse("finance:expense_attachment", args=[e.pk])).status_code == 302


def test_approved_expense_cannot_be_opened_for_edit(client, owner, shop, main_branch, rent):
    with tenant_context(shop, branch=main_branch):
        e = Expense.objects.create(branch=main_branch, category=rent, amount=5,
                                   spent_at=timezone.localdate(), approved_by=owner)
    client.force_login(owner)
    r = client.get(reverse("finance:expense_edit", args=[e.pk]), **HX)
    assert r.status_code == 204


def test_expense_filters_and_cashup_filters(client, owner, shop, main_branch, rent):
    with tenant_context(shop, branch=main_branch):
        Expense.objects.create(branch=main_branch, category=rent, amount=5,
                               spent_at=timezone.localdate(), method="bank")
    client.force_login(owner)
    r = client.get(reverse("finance:expense_list"), {"method": "cash", "from": "bad"})
    assert r.status_code == 200 and r.context["page"].paginator.count == 0
    r = client.get(reverse("finance:expense_list"), {"state": "waiting"})
    assert r.context["page"].paginator.count == 1
    r = client.get(reverse("finance:cashups"), {"state": "short", "branch": "x", "days": "abc"})
    assert r.status_code == 200
