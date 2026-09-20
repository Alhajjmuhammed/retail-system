"""
Messages sent to people outside the system.

SMS is a plan feature shops pay for, and the app behind it was empty. Every
message is recorded before it is sent, so a shop can see what went out, what
it cost, and what failed -- rather than wondering whether a customer was told
anything at all.
"""

from django.db import models

from apps.core.models import TenantModel


class Channel(models.TextChoices):
    SMS = "sms", "SMS"
    WHATSAPP = "whatsapp", "WhatsApp"
    EMAIL = "email", "Email"


class MessageStatus(models.TextChoices):
    QUEUED = "queued", "Waiting to send"
    SENT = "sent", "Sent"
    FAILED = "failed", "Failed"
    SKIPPED = "skipped", "Not sent"


class MessageTemplate(TenantModel):
    """
    What a shop says, in their own words.

    Placeholders are filled from the event: {customer}, {amount}, {balance},
    {shop}, {receipt}.
    """

    key = models.SlugField(max_length=40)
    name = models.CharField(max_length=80)
    channel = models.CharField(max_length=10, choices=Channel.choices, default=Channel.SMS)
    body = models.TextField()
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        unique_together = [("tenant", "key")]

    def __str__(self):
        return self.name

    def render(self, **context) -> str:
        text = self.body
        for key, value in context.items():
            text = text.replace("{" + key + "}", str(value))
        return text


class Message(TenantModel):
    """One message, recorded whether or not it ever leaves."""

    channel = models.CharField(max_length=10, choices=Channel.choices)
    to = models.CharField(max_length=40, db_index=True)
    body = models.TextField()
    template_key = models.CharField(max_length=40, blank=True)

    status = models.CharField(
        max_length=10, choices=MessageStatus.choices, default=MessageStatus.QUEUED
    )
    provider = models.CharField(max_length=40, blank=True)
    provider_ref = models.CharField(max_length=120, blank=True)
    error = models.TextField(blank=True)
    attempts = models.PositiveIntegerField(default=0)
    sent_at = models.DateTimeField(null=True, blank=True)
    cost = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["tenant", "status", "-created_at"])]

    def __str__(self):
        return f"{self.get_channel_display()} to {self.to}"
