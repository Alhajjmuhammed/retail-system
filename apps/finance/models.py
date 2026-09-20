"""
Money that is not a sale: expenses, and the daily cash position.
"""


from django.db import models

from apps.core.models import BranchModel, TenantModel
from apps.core.storage import PrivateStorage


def receipt_path(instance, filename):
    """
    Unguessable and per shop. A receipt is private paperwork; named after the
    uploaded file it could be found by anyone who guessed the name.
    """
    import os
    import uuid

    ext = os.path.splitext(filename)[1].lower()[:6]
    return f"expenses/{instance.tenant_id}/{uuid.uuid4().hex}{ext}"


class ExpenseCategory(TenantModel):
    name = models.CharField(max_length=80)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        unique_together = [("tenant", "name")]
        verbose_name_plural = "expense categories"

    def __str__(self):
        return self.name


class Expense(BranchModel):
    category = models.ForeignKey(
        ExpenseCategory, on_delete=models.PROTECT, related_name="expenses"
    )
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    spent_at = models.DateField(db_index=True)
    method = models.CharField(max_length=20, default="cash")
    reference = models.CharField(max_length=60, blank=True)
    description = models.CharField(max_length=200, blank=True)
    attachment = models.FileField(upload_to=receipt_path, blank=True, storage=PrivateStorage())

    approved_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    # The till pay-out this expense made, if it came out of a drawer. Editing
    # or removing the expense used to leave the drawer's figures untouched.
    cash_movement = models.OneToOneField(
        "pos.CashMovement", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="expense",
    )

    class Meta:
        ordering = ["-spent_at", "-id"]
        indexes = [models.Index(fields=["tenant", "branch", "-spent_at"])]

    def __str__(self):
        return f"{self.category}: {self.amount}"

    @property
    def is_approved(self) -> bool:
        return self.approved_at is not None
