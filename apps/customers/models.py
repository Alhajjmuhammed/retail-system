"""
Customers, credit and loyalty.

A balance is the running sum of a ledger, never a stored number -- the same
reason stock works that way. An owner asking "why does Mama Asha owe 340,000?"
has to be able to see every line that made it up.
"""

from decimal import Decimal

from django.db import models

from apps.core.models import TenantModel


class Customer(TenantModel):
    name = models.CharField(max_length=120, db_index=True)
    phone = models.CharField(max_length=30, blank=True, db_index=True)
    email = models.EmailField(blank=True)
    address = models.TextField(blank=True)
    tin = models.CharField("TIN", max_length=30, blank=True)

    # Wholesale buyers get their own price list without duplicating products.
    price_list = models.ForeignKey(
        "catalog.PriceList", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="customers",
    )
    credit_limit = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    note = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        indexes = [
            models.Index(fields=["tenant", "name"]),
            models.Index(fields=["tenant", "phone"]),
        ]

    def __str__(self):
        return self.name

    @property
    def balance(self) -> Decimal:
        """What they owe. Positive means the shop is owed money."""
        total = self.credit_transactions.aggregate(
            total=models.Sum("amount")
        )["total"]
        return total or Decimal("0")

    @property
    def credit_available(self) -> Decimal:
        return self.credit_limit - self.balance

    def can_take_credit(self, amount) -> bool:
        if self.credit_limit <= 0:
            return False
        return (self.balance + Decimal(str(amount))) <= self.credit_limit

    @property
    def loyalty_points(self) -> int:
        total = self.loyalty_transactions.aggregate(
            total=models.Sum("points")
        )["total"]
        return total or 0


class CreditKind(models.TextChoices):
    CHARGE = "charge", "Sold on account"
    PAYMENT = "payment", "Payment received"
    ADJUSTMENT = "adjustment", "Adjustment"
    REFUND = "refund", "Refund"


class CreditTransaction(TenantModel):
    """
    One line of a customer's account.

    ``amount`` is signed: a charge is positive, a payment negative, so the
    balance is a plain sum and cannot drift.
    """

    customer = models.ForeignKey(
        Customer, on_delete=models.PROTECT, related_name="credit_transactions"
    )
    kind = models.CharField(max_length=12, choices=CreditKind.choices)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    balance_after = models.DecimalField(
        max_digits=14, decimal_places=2, null=True, blank=True
    )
    sale = models.ForeignKey(
        "pos.Sale", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="credit_transactions",
    )
    reference = models.CharField(max_length=60, blank=True)
    note = models.CharField(max_length=200, blank=True)
    # How a payment came in: cash, mobile money, bank. Blank for charges.
    method = models.CharField(max_length=10, blank=True)
    # The till pay-in a cash payment made, so undoing it can take it out again.
    cash_movement = models.OneToOneField(
        "pos.CashMovement", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="credit_entry",
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["tenant", "customer", "-created_at"])]

    def __str__(self):
        return f"{self.customer}: {self.amount:+}"


class LoyaltyKind(models.TextChoices):
    EARN = "earn", "Earned"
    REDEEM = "redeem", "Redeemed"
    EXPIRE = "expire", "Expired"
    ADJUST = "adjust", "Adjusted"


class LoyaltyTransaction(TenantModel):
    customer = models.ForeignKey(
        Customer, on_delete=models.CASCADE, related_name="loyalty_transactions"
    )
    kind = models.CharField(max_length=10, choices=LoyaltyKind.choices)
    points = models.IntegerField(help_text="Signed: earned positive, redeemed negative.")
    sale = models.ForeignKey(
        "pos.Sale", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.customer}: {self.points:+} points"
