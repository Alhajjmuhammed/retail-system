"""
Row-level security: the lock under the lock.

The managers are what actually scope queries. This proves the database still
refuses when something reaches past them -- a raw cursor, a hand-written
report, a package that knows nothing about tenants.
"""

import pytest
from django.db import connection, transaction

from apps.catalog.models import Product, TaxRate, Unit
from apps.core.context import tenant_context
from apps.tenancy.services import create_tenant

pytestmark = pytest.mark.django_db


def _make_product(tenant, name):
    with tenant_context(tenant):
        return Product.objects.create(
            name=name,
            base_unit=Unit.objects.get(code="pc"),
            tax_rate=TaxRate.objects.get(is_default=True),
        )


def _bind(tenant_id):
    """What TenantMiddleware does on every tenant-facing request."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.tenant_id', %s, false)",
            [str(tenant_id) if tenant_id else ""],
        )


def _raw_product_names():
    with connection.cursor() as cursor:
        cursor.execute("SELECT name FROM catalog_product ORDER BY name")
        return [row[0] for row in cursor.fetchall()]


def test_raw_sql_is_blocked_by_the_database(db, owner, cashier, business_plan):
    salma, _ = create_tenant(name="Duka la Salma", owner=owner, plan=business_plan)
    juma, _ = create_tenant(name="Duka la Juma", owner=cashier, plan=business_plan)

    _make_product(salma, "Sukari 1kg")
    _make_product(juma, "Mkate")

    try:
        _bind(salma.pk)
        # Deliberately bypassing the ORM's tenant manager entirely.
        assert _raw_product_names() == ["Sukari 1kg"]

        _bind(juma.pk)
        assert _raw_product_names() == ["Mkate"]
    finally:
        _bind(None)


def test_unbound_connection_sees_everything(db, owner, cashier, business_plan):
    """
    Migrations, Celery jobs and the platform admin run with nothing bound and
    legitimately work across tenants. That is the documented trade-off.
    """
    salma, _ = create_tenant(name="Duka la Salma", owner=owner, plan=business_plan)
    juma, _ = create_tenant(name="Duka la Juma", owner=cashier, plan=business_plan)
    _make_product(salma, "Sukari 1kg")
    _make_product(juma, "Mkate")

    _bind(None)
    assert _raw_product_names() == ["Mkate", "Sukari 1kg"]


def test_writing_another_tenants_row_is_refused(db, owner, cashier, business_plan):
    salma, _ = create_tenant(name="Duka la Salma", owner=owner, plan=business_plan)
    juma, _ = create_tenant(name="Duka la Juma", owner=cashier, plan=business_plan)

    with tenant_context(salma):
        unit = Unit.objects.get(code="pc")
        tax = TaxRate.objects.get(is_default=True)

    try:
        _bind(salma.pk)
        # A savepoint, because the refused statement aborts the transaction and
        # everything after it would fail for the wrong reason.
        with pytest.raises(Exception), transaction.atomic(), connection.cursor() as cursor:
            # WITH CHECK refuses an insert stamped with somebody else's tenant.
            cursor.execute(
                "INSERT INTO catalog_product "
                "(tenant_id, name, sku, description, image, track_stock, "
                " sellable_at_pos, discount_allowed, is_active, "
                " base_unit_id, tax_rate_id, created_at, updated_at) "
                "VALUES (%s, 'Smuggled', '', '', '', true, true, true, true, "
                "%s, %s, now(), now())",
                [juma.pk, unit.pk, tax.pk],
            )
    finally:
        _bind(None)


@pytest.mark.django_db(transaction=True)
def test_the_lock_holds_for_a_whole_real_request(rf, owner, cashier, business_plan):
    """
    Through the actual middleware, in autocommit, as production runs.

    The binding used to be transaction-local and made outside any
    transaction, so it was gone before the view ran its first query and
    every request had row-level security wide open. The earlier tests bound
    it by hand inside a test transaction and could not see that.
    """
    from django.http import HttpResponse

    from apps.core.middleware import TenantMiddleware

    salma, _ = create_tenant(name="Duka la Salma", owner=owner, plan=business_plan)
    juma, _ = create_tenant(name="Duka la Juma", owner=cashier, plan=business_plan)
    _make_product(salma, "Sukari 1kg")
    _make_product(juma, "Mkate")

    seen = {}

    def view(request):
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_setting('app.tenant_id', true)")
            seen["binding"] = cursor.fetchone()[0]
        seen["products"] = _raw_product_names()
        return HttpResponse("ok")

    request = rf.get("/stock/")
    request.user = owner
    request.session = {}
    TenantMiddleware(view)(request)

    assert seen["binding"] == str(salma.pk)
    assert seen["products"] == ["Sukari 1kg"], "raw SQL saw another shop's rows"

    with connection.cursor() as cursor:
        cursor.execute("SELECT current_setting('app.tenant_id', true)")
        assert (cursor.fetchone()[0] or "") == "", "binding leaked past the request"
