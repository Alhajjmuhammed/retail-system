"""
Selling from a phone, on the shop floor.

A member of staff walks the aisle with a customer, scans or searches as
they go, and then either hands the basket to a till by a short code, or --
if the owner allows it -- takes payment right there. What a phone may take,
how much and by which methods, is the owner's decision through permissions:

  pos.mobile_cart     build a basket on a phone
  pos.mobile_payment  take payment on a phone, up to a limit
  pos.mobile_methods  which payment methods a phone may use

A phone sale goes through the same checks as a till sale, priced by the
server -- never by the phone.
"""

import json
import uuid

from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from apps.catalog.models import Barcode, PriceList, Variant
from apps.core import audit
from apps.core.decorators import requires
from apps.core.parsing import decimal_or_none, int_or
from apps.pos.models import PaymentMethod, Sale, Shift, ShiftStatus
from apps.pos.services import add_to_cart, complete_sale, new_cart
from apps.pos.validation import SaleRejected, check_sale


def allowed_methods(membership):
    """The payment methods this person may take on a phone, in a fixed order."""
    from apps.core.permissions import registry

    spec = registry.get("pos.mobile_methods")
    return [m for m in spec.options if membership.can("pos.mobile_methods", value=m)]


def _customer(raw_id):
    """An active customer of this shop, or None."""
    from apps.core.parsing import int_or
    from apps.customers.models import Customer

    pk = int_or(raw_id)
    if not pk:
        return None
    return Customer.objects.select_related("price_list").filter(pk=pk, is_active=True).first()


def _price(variant, default_list, customer=None, qty=1):
    """The customer's list when it has this product, else the usual price."""
    if customer is not None and customer.price_list_id and customer.price_list.is_active:
        price = variant.price_for(customer.price_list, qty)
        if price is not None:
            return price
    return variant.price_for(default_list, qty)


def _variant_json(variant, price_list, customer=None):
    price = _price(variant, price_list, customer)
    return {
        "id": variant.pk,
        "name": str(variant),
        "price": None if price is None else str(price),
        "decimal": variant.product.base_unit.allows_decimal,
        "unit": variant.product.base_unit.code,
    }


@login_required
@requires("pos.mobile_cart")
def phone(request):
    membership = request.membership
    payment = membership.check_permission("pos.mobile_payment")
    config = {
        "may_pay": bool(payment),
        "pay_limit": float(payment.limit) if payment and payment.limit else None,
        "methods": [
            {"value": m, "label": dict(PaymentMethod.choices).get(m, m)}
            for m in (allowed_methods(membership) if payment else [])
        ],
        "has_drawer": _open_shift(request) is not None,
        # On account needs a customer and the right to give credit.
        "may_credit": bool(payment) and membership.can("credit.grant"),
        "currency": request.tenant.currency,
        "user_id": request.user.pk,
        "tenant_id": request.tenant.pk,
        "endpoints": {
            "lookup": "/pos/phone/lookup/",
            "customers": "/pos/phone/customers/",
            "checkout": "/pos/phone/checkout/",
            "handoff": "/api/v1/sync/carts/",
        },
    }
    return render(request, "pos/phone.html", {"config": json.dumps(config)})


@login_required
@requires("pos.mobile_cart")
@require_GET
def lookup(request):
    """A barcode, a few letters of a name, or ids to re-price. Prices from the server."""
    price_list = PriceList.objects.filter(is_default=True).first()
    customer = _customer(request.GET.get("customer"))
    sellable = Variant.objects.select_related("product__base_unit").filter(
        is_active=True, product__is_active=True, product__sellable_at_pos=True
    )
    code = request.GET.get("code", "").strip()
    if code:
        barcode = Barcode.objects.select_related("variant__product__base_unit").filter(
            code=code, variant__in=sellable
        ).first()
        if barcode is None:
            return JsonResponse({"results": [], "error": f"No product with barcode {code}."})
        item = _variant_json(barcode.variant, price_list, customer)
        item["qty"] = str(barcode.pack_quantity)
        return JsonResponse({"results": [item]})
    ids = [int_or(x) for x in request.GET.get("ids", "").split(",") if x.strip()]
    if ids:
        # Re-pricing a basket: by id, because a variant's shown name ("Sukari
        # 1kg - Kilo2") matches neither the product nor the variant name.
        found = sellable.filter(pk__in=[i for i in ids if i])[:50]
        return JsonResponse({"results": [_variant_json(v, price_list, customer) for v in found]})

    term = request.GET.get("q", "").strip()
    if len(term) < 2:
        return JsonResponse({"results": []})
    found = sellable.filter(
        Q(product__name__icontains=term) | Q(name__icontains=term) | Q(product__sku__icontains=term)
    ).order_by("product__name")[:15]
    return JsonResponse({"results": [_variant_json(v, price_list, customer) for v in found]})


@login_required
@requires("pos.mobile_cart")
@require_GET
def customers(request):
    """Find who is buying: by name or phone, with their list and credit left."""
    from decimal import Decimal

    from django.db.models import Sum
    from django.db.models.functions import Coalesce

    from apps.customers.models import Customer

    term = request.GET.get("q", "").strip()
    rows = Customer.objects.filter(is_active=True).select_related("price_list")
    if term:
        rows = rows.filter(Q(name__icontains=term) | Q(phone__icontains=term))
    rows = rows.annotate(owed=Coalesce(Sum("credit_transactions__amount"), Decimal("0"))) \
        .order_by("name")[:20]
    return JsonResponse({"results": [
        {"id": c.pk, "name": c.name, "phone": c.phone,
         "price_list": c.price_list.name if c.price_list_id and c.price_list.is_active else "",
         "credit_left": str(max(c.credit_limit - c.owed, Decimal("0")))}
        for c in rows
    ]})


def _open_shift(request):
    return Shift.objects.filter(
        branch=request.branch, user=request.user, status=ShiftStatus.OPEN
    ).select_related("register").first()


@login_required
@requires("pos.mobile_cart")
@require_POST
def checkout(request):
    """
    Take payment on the phone.

    Refused, with a reason the phone shows, when the person may not take
    payment, the sale is over their limit, the method is not one the owner
    allowed, or it is cash with no drawer to put it in. Handing off to a
    till is always the way out.
    """
    try:
        body = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Malformed request."}, status=400)
    if not isinstance(body, dict) or not isinstance(body.get("lines"), list) or not body["lines"]:
        return JsonResponse({"error": "The basket is empty."}, status=400)

    try:
        client_uuid = str(uuid.UUID(str(body.get("client_uuid"))))
    except ValueError:
        return JsonResponse({"error": "Missing sale id."}, status=400)
    from apps.core.context import unscoped

    with unscoped():
        # Sale ids are unique across every shop: one already used elsewhere
        # died on the index instead of saying so.
        elsewhere = Sale.objects_all.filter(client_uuid=client_uuid).exclude(
            tenant=request.tenant).exists()
    if elsewhere:
        return JsonResponse({"error": "That sale id is already used. Reload and try again."},
                            status=409)
    existing = Sale.objects.filter(client_uuid=client_uuid).first()
    if existing is not None:
        # Pressed twice, or the reply was lost: the same sale, not a second.
        return JsonResponse({"number": existing.number, "total": str(existing.total)})

    from apps.org.models import Device

    device_id = str(body.get("device_id") or "")[:64]
    if not device_id:
        # Dropping the field was a way past the check below.
        return JsonResponse({"error": "This phone is not registered with the shop. "
                                      "Reload the page and try again."}, status=409)
    if Device.objects.filter(device_id=device_id, is_active=False).exists():
        return JsonResponse({"error": "This phone has been switched off by the shop. Use a till."},
                            status=409)

    membership = request.membership
    price_list = PriceList.objects.filter(is_default=True).first()
    customer = None
    if body.get("customer_id"):
        customer = _customer(body.get("customer_id"))
        if customer is None:
            return JsonResponse({"error": "That customer is not in this shop."}, status=400)
    lines, total = [], 0
    for raw in body["lines"]:
        variant = Variant.objects.select_related("product").filter(
            pk=decimal_or_none(raw.get("variant_id") if isinstance(raw, dict) else None) or 0
        ).first()
        qty = decimal_or_none(raw.get("qty")) if isinstance(raw, dict) else None
        if variant is None or qty is None or qty <= 0:
            return JsonResponse({"error": "Something in the basket is not a product here."}, status=400)
        price = _price(variant, price_list, customer, qty)
        if price is None:
            # Unpriced is not free.
            return JsonResponse({"error": f"{variant} has no price yet. Ask a manager to set one."},
                                status=400)
        lines.append({"variant_id": variant.pk, "qty": str(qty), "unit_price": str(price),
                      "discount": "0", "added_via": "camera"})
        total += qty * price

    method = body.get("method")
    reference = str(body.get("reference") or "").strip()[:60]
    on_account = method == PaymentMethod.CREDIT
    if on_account and customer is None:
        return JsonResponse({"error": "Choose the customer to put it on account."}, status=400)
    if on_account and not membership.can("credit.grant"):
        return JsonResponse({"error": "You cannot sell on account. Send it to a till."}, status=403)
    if on_account and not customer.can_take_credit(total):
        # Online, with the goods still in hand: refused, not merely flagged.
        return JsonResponse({"error": f"{customer.name} has only "
                                      f"{customer.credit_available:,.0f} of credit left. "
                                      "Take payment another way."}, status=400)
    if not on_account and method not in allowed_methods(membership):
        return JsonResponse({"error": "That payment method is not allowed on a phone. Send it to a till."},
                            status=403)
    decision = membership.check_permission("pos.mobile_payment", value=total)
    if not decision:
        return JsonResponse({"error": f"{decision.reason} Send it to a till instead."}, status=403)
    shift = _open_shift(request)
    if method == PaymentMethod.CASH and shift is None:
        return JsonResponse({"error": "Cash taken on a phone has to go into a drawer, and you have no "
                                      "till open. Take mobile money, or send it to a till."}, status=400)
    if method not in (PaymentMethod.CASH, PaymentMethod.CREDIT) and len(reference) < 4:
        return JsonResponse({"error": "Enter the transaction reference from the payment message."},
                            status=400)

    entry = {"client_uuid": client_uuid, "lines": lines,
             "customer_id": customer.pk if customer else None,
             "payments": [{"method": method, "amount": str(total), "reference": reference}]}
    try:
        with transaction.atomic():
            checked = check_sale(membership, entry, price_list=price_list)
            cart = new_cart(user=request.user, branch=request.branch,
                            register=shift.register if shift else None,
                            customer=checked.customer)
            for line in checked.lines:
                add_to_cart(cart, line["variant"], qty=line["qty"], unit_price=line["unit_price"],
                            description=line["description"], added_via="camera")
            sale = complete_sale(cart, checked.payments, user=request.user, shift=shift,
                                 client_uuid=checked.client_uuid, review_notes=checked.review)
    except SaleRejected as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    audit.record("sale.phone", obj=sale, after={"method": method, "total": str(sale.total)},
                 ip=audit.client_ip(request))
    return JsonResponse({"number": sale.number, "total": str(sale.total)})
