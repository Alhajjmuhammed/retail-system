"""
The Health page: is everything running, and is anything piling up?
"""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import PlatformRole, User
from apps.core.jobs import JOBS, status, tracked
from apps.core.models import JobHeartbeat

pytestmark = pytest.mark.django_db


@pytest.fixture
def boss(db):
    return User.objects.create_user("b@p.test", "pw", name="B", is_platform_staff=True,
                                    platform_role=PlatformRole.objects.get(name="Super admin"))


def test_every_scheduled_job_reports_in():
    """A job added to the schedule without a heartbeat would be invisible."""
    from config.celery import app

    assert set(app.conf.beat_schedule) == set(JOBS)


def test_a_job_records_its_runs_and_failures(db):
    @tracked("send-queued-messages")
    def works():
        return {"sent": 3}

    @tracked("send-fiscal-receipts")
    def breaks():
        raise RuntimeError("provider down")

    works()
    with pytest.raises(RuntimeError):
        breaks()
    rows = {r["name"]: r["verdict"] for r in status()}
    assert rows["send-queued-messages"] == "fine"
    assert rows["send-fiscal-receipts"] == "failed"
    assert rows["advance-subscriptions"] == "never"


def test_a_job_that_stopped_running_is_overdue(db):
    JobHeartbeat.objects.create(name="send-queued-messages", last_ok=True,
                                last_finished=timezone.now() - timedelta(hours=2))
    assert {r["name"]: r["verdict"] for r in status()}["send-queued-messages"] == "overdue"


def test_the_real_tasks_report_in(db):
    from apps.pos.tasks import clear_abandoned_carts

    clear_abandoned_carts()
    assert JobHeartbeat.objects.get(name="clear-abandoned-carts").runs == 1


def test_the_page_counts_everything_not_just_the_first_fifty(client, boss, shop, main_branch, stocked, owner):
    from apps.core.context import tenant_context
    from apps.pos.models import FiscalReceipt, FiscalStatus
    from apps.pos.services import add_to_cart, complete_sale, new_cart

    with tenant_context(shop, branch=main_branch, user=owner):
        for _ in range(55):
            cart = new_cart(branch=main_branch)
            add_to_cart(cart, stocked["Mkate"], qty=1)
            sale = complete_sale(cart, [{"method": "cash", "amount": 1500}])
            FiscalReceipt.objects.update_or_create(sale=sale, defaults={
                "tenant": shop, "status": FiscalStatus.FAILED, "error": "timeout"})
    client.force_login(boss)
    r = client.get(reverse("platform:health"))
    assert r.status_code == 200 and r.context["fiscal_failed_count"] == 55
    assert r.context["jobs_bad"] == len(JOBS)  # nothing has run in the test database
