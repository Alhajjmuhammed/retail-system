"""
Keep the till's catalogue up to date.

Tills fetch only variants changed since their last sync, judged by
`Variant.updated_at`. A price, a barcode or the product itself changes on
other rows, so without this a price change never reached a till that was
already open -- and every sale at the old price was flagged for review.
"""

from django.db.models.signals import post_delete, post_init, post_save
from django.dispatch import receiver
from django.utils import timezone

from apps.catalog.models import Barcode, Price, PriceList, Product, TaxRate, Unit, Variant


def _touch(**filters):
    Variant.objects_all.filter(**filters).update(updated_at=timezone.now())


@receiver([post_save, post_delete], sender=Price)
@receiver([post_save, post_delete], sender=Barcode)
def _variant_row_changed(sender, instance, **kwargs):
    if instance.variant_id:
        _touch(pk=instance.variant_id)


@receiver(post_save, sender=Product)
def _product_changed(sender, instance, created, **kwargs):
    if not created:
        _touch(product_id=instance.pk)


@receiver(post_save, sender=TaxRate)
def _tax_changed(sender, instance, created, **kwargs):
    if not created:
        _touch(product__tax_rate=instance)


@receiver(post_save, sender=Unit)
def _unit_changed(sender, instance, created, **kwargs):
    if not created:
        _touch(product__base_unit=instance)


@receiver(post_save, sender=PriceList)
def _default_list_changed(sender, instance, **kwargs):
    # A new default list changes every price the till shows -- and so does a
    # list switched off or back on: tills already open kept pricing from a
    # dead list, or never learned about a restored one.
    if instance.is_default or _active_changed(instance):
        _touch(tenant_id=instance.tenant_id)


def _active_changed(instance) -> bool:
    was = getattr(instance, "_was_active", None)
    return was is None or was != instance.is_active


@receiver(post_init, sender=PriceList)
def _remember_active(sender, instance, **kwargs):
    instance._was_active = instance.is_active
