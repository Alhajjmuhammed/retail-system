"""
The sync surface.

Eight endpoints, not a parallel API for every model. Everything else in the
system is server-rendered; this exists only because a till must keep selling
when the connection drops.
"""

import json
import logging
from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import DataError, models, transaction
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from apps.catalog.models import Variant
from apps.core import audit
from apps.core.decorators import requires
from apps.core.parsing import decimal_or_none, int_or
from apps.org.models import Device
from apps.pos.models import Cart, CartStatus, Sale, Shift, ShiftStatus
from apps.pos.services import add_to_cart, complete_sale, new_cart
from apps.pos.validation import SaleRejected, check_sale, mark_basket_lines_sold

logger = logging.getLogger(__name__)


def requires_offline(view):
    """
    For the parts that exist only for selling with no connection.

    The catalogue and sending sales are open to every plan: the till has no
    other way to sell, so gating them left a plan without offline selling
    unable to sell at all.
    """
    from functools import wraps

    from apps.core.features import OFFLINE_POS

    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if request.tenant is None or not request.tenant.has_feature(OFFLINE_POS):
            return JsonResponse(
                {"error": "Offline selling is not included in this plan."},
                status=402,
            )
        return view(request, *args, **kwargs)

    return wrapped


@login_required
@requires("pos.operate")
@require_GET
def catalog_snapshot(request):
    """
    Everything a till needs to sell with no connection.

    Sent once at shift open and topped up by timestamp afterwards, so a shop
    with four thousand products does not re-download them every morning.
    """
    from django.utils.dateparse import parse_datetime

    # Taken before reading: a change saved while this runs is then included
    # next time, instead of falling between two syncs.
    now = timezone.now()
    try:
        # "2026-13-45T00:00:00" parses as a date shape and then explodes.
        since = parse_datetime(request.GET.get("since") or "")
    except ValueError:
        since = None
    variants = Variant.objects.select_related(
        "product", "product__tax_rate", "product__base_unit"
    ).prefetch_related("barcodes", "prices")
    sellable = {"is_active": True, "product__is_active": True, "product__sellable_at_pos": True}
    if since:
        # Changes only -- including things switched off, which the till must
        # drop. Leaving them out meant a till kept selling them for ever.
        removed = list(
            variants.filter(updated_at__gt=since)
            .exclude(**sellable).values_list("pk", flat=True)
        )
        variants = variants.filter(updated_at__gt=since, **sellable)
    else:
        removed = []
        variants = variants.filter(**sellable)

    from apps.catalog.models import PriceList

    active_lists = dict(PriceList.objects.filter(is_active=True).values_list("pk", "is_default"))
    default_list = next((pk for pk, is_default in active_lists.items() if is_default), None)
    other_lists = [pk for pk, is_default in active_lists.items() if not is_default]

    payload = [
        {
            "id": v.pk,
            "name": str(v),
            "sku": v.sku or v.product.sku,
            "unit": v.product.base_unit.code,
            "decimal": v.product.base_unit.allows_decimal,
            "tax_rate": str(v.product.tax_rate.rate),
            "track_stock": v.product.track_stock,
            "discount_allowed": v.product.discount_allowed,
            "min_price": str(v.product.min_price) if v.product.min_price else None,
            # None, never 0: a missing price used to reach the till as "free".
            "price": _price_text(_list_price(v, default_list, default=True)),
            # Other lists' prices, so a customer's list works offline too.
            "prices": {
                str(pl): _price_text(amount)
                for pl in other_lists
                if (amount := _list_price(v, pl)) is not None
            },
            "barcodes": [
                {"code": b.code, "qty": str(b.pack_quantity)} for b in v.barcodes.all()
            ],
        }
        for v in variants
    ]

    return JsonResponse(
        {
            "server_time": now.isoformat(),
            "count": len(payload),
            "variants": payload,
            "removed": removed,
        }
    )


@login_required
@requires("pos.sell")
@require_POST
def push_sales(request):
    """
    Accept a batch of sales recorded offline.

    Each carries a ``client_uuid`` generated on the device, and the server
    upserts on it. A till that retries five times stores the sale once.

    Stock is allowed to go negative here on purpose: the money was taken and
    the customer has gone. Rejecting a completed sale would be the one
    behaviour that destroys trust in the system.
    """
    try:
        body = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Malformed JSON."}, status=400)

    if not isinstance(body, dict) or not isinstance(body.get("sales", []), list):
        return JsonResponse({"error": "Expected {\"sales\": [...]}."}, status=400)

    # Whose queue is this? A till tab left open after somebody else signed
    # in -- or a till page saved for offline use -- would otherwise send one
    # person's sales under another's name and into their drawer.
    owner = body.get("queue_owner") or {}
    if owner and (str(owner.get("tenant_id")) != str(request.tenant.pk)
                  or str(owner.get("user_id")) != str(request.user.pk)):
        return JsonResponse({"error": "signed_in_as_someone_else"}, status=409)

    # A retired device -- lost, stolen, replaced -- is not trusted with sales.
    # Refused as a whole; the device keeps them and says why.
    # Every till says which device it is. Leaving the field out was a way
    # past the check, so a missing one is refused like a retired one.
    device_id = str(body.get("device_id") or "")[:64]
    if not device_id:
        return JsonResponse({"error": "device_unknown"}, status=409)
    if Device.objects.filter(device_id=device_id, is_active=False).exists():
        return JsonResponse({"error": "device_retired"}, status=409)

    accepted, rejected, retry = [], [], []
    shift_id = body.get("shift_id")

    for entry in body.get("sales", []):
        uuid_seen = entry.get("client_uuid") if isinstance(entry, dict) else None
        try:
            with transaction.atomic():
                # The shift the sale was made in, carried by the sale itself;
                # the batch's shift only for older queued sales without one.
                own = entry.get("shift_id") if isinstance(entry, dict) else None
                sale = _store_offline_sale(request, entry, own or shift_id)
            accepted.append({"client_uuid": str(sale.client_uuid), "number": sale.number,
                             "needs_review": sale.needs_review})
        except SaleRejected as exc:
            rejected.append({"client_uuid": uuid_seen, "error": str(exc)})
            _note_rejection(request, uuid_seen, exc)
        except (DataError, DjangoValidationError, ValueError, TypeError) as exc:
            # Something about the sale itself that will fail every time: park
            # it for a manager instead of retrying it for ever, unseen.
            logger.exception("Synced sale %s could not be stored", uuid_seen)
            rejected.append({"client_uuid": uuid_seen,
                             "error": "This sale could not be stored. Show it to a manager."})
            _note_rejection(request, uuid_seen, exc)
        except Exception:
            # Probably ours and passing (a lock, a restart): the till keeps
            # it and sends it again. Logged, and never a database message
            # back to a device.
            logger.exception("Synced sale %s hit a server error; will retry", uuid_seen)
            retry.append({"client_uuid": uuid_seen})

    _touch_device(request, body.get("device_id"), body.get("queued"))

    return JsonResponse(
        {
            "accepted": accepted,
            "rejected": rejected,
            "retry": retry,
            "server_time": timezone.now().isoformat(),
        }
    )


def _note_rejection(request, client_uuid, exc):
    """Once per sale: the till retries, and the audit log must not fill up."""
    from apps.accounts.models import AuditLog

    if AuditLog.objects.filter(action="sale.sync_rejected",
                               after__client_uuid=str(client_uuid)).exists():
        return
    audit.record("sale.sync_rejected", after={"client_uuid": str(client_uuid),
                                               "error": str(exc)},
                 ip=audit.client_ip(request))


def _resolve_shift(request, shift_id, sold_at):
    """
    The drawer that took this sale's money: the caller's own, never another's.

    The shift open when the sale was made, even if it has closed since --
    a sale that synced late used to land in the next shift, making that one
    look over and the real one short. Returns (shift, note for review).
    """
    shift = None
    if shift_id:
        # Found by id and person, in whichever branch it was: switching
        # branch in another tab before the sale synced used to record it in
        # the new branch, with no shift. Still only a branch they work in.
        shift = Shift.objects.select_related("branch").filter(
            pk=decimal_or_none(shift_id) or 0, user=request.user
        ).first()
        if shift is not None and not request.membership.covers_branch(shift.branch):
            shift = None
    if shift is None:
        mine = Shift.objects.filter(branch=request.branch, user=request.user)
        shift = (
            mine.filter(opened_at__lte=sold_at)
            .filter(models.Q(closed_at__isnull=True) | models.Q(closed_at__gte=sold_at))
            .order_by("-opened_at").first()
            or mine.filter(status=ShiftStatus.OPEN).first()
        )
    note = None
    if shift is not None and shift.status != ShiftStatus.OPEN:
        note = (f"Reached the server after shift {shift.pk} was closed; that "
                "cash-up did not include it.")
    return shift, note


def _store_offline_sale(request, entry, shift_id=None):
    from apps.core.context import unscoped

    client_uuid = entry.get("client_uuid") if isinstance(entry, dict) else None
    if client_uuid:
        existing = Sale.objects.filter(client_uuid=client_uuid).first()
        if existing is not None:
            return existing  # a retried sync is never a second sale
        with unscoped():
            elsewhere = Sale.objects_all.filter(client_uuid=client_uuid).exists()
        if elsewhere:
            raise SaleRejected("This sale's id is already used. It cannot be stored twice.")

    from apps.catalog.models import PriceList

    checked = check_sale(request.membership, entry,
                         price_list=PriceList.objects.filter(is_default=True).first())
    shift, late = _resolve_shift(request, shift_id, checked.sold_at)
    if late:
        checked.review.append(late)
    cart = new_cart(
        user=request.user,
        register=shift.register if shift is not None else None,
        # Stock comes out where the sale happened.
        branch=shift.branch if shift is not None else request.branch,
    )
    if checked.customer is not None:
        cart.customer = checked.customer
        cart.save(update_fields=["customer", "updated_at"])

    for line in checked.lines:
        add_to_cart(
            cart,
            line["variant"],
            qty=line["qty"],
            unit_price=line["unit_price"],
            discount=line["discount"],
            description=line["description"],
            added_via=line["added_via"],
        )

    from apps.core.features import OFFLINE_POS

    if not request.tenant.has_feature(OFFLINE_POS) and \
            checked.sold_at < timezone.now() - timedelta(minutes=15):
        # Every plan can sell -- the till sends every sale this way -- but
        # holding sales offline is a paid extra. Money already taken is
        # recorded; the owner sees it was made offline.
        checked.review.append("Made while offline, which this shop's plan does not include.")
    sale = complete_sale(
        cart,
        checked.payments,
        user=request.user,
        shift=shift,
        client_uuid=checked.client_uuid,
        sold_at=checked.sold_at,
        is_offline_origin=True,
        review_notes=checked.review,
    )
    mark_basket_lines_sold(entry, sale)
    if sale.needs_review:
        audit.record("sale.needs_review", obj=sale, after={"notes": checked.review},
                     ip=audit.client_ip(request))
    return sale


def _touch_device(request, device_id, queued=None):
    # A silly "queued" count used to overflow the column and fail the whole
    # batch of sales.
    if queued is not None:
        queued = max(0, min(int_or(queued), 100000))
    if not device_id:
        return
    fields = {"last_sync_at": timezone.now(), "last_seen_ip": audit.client_ip(request)}
    if queued is not None:
        fields["queued"] = max(int(decimal_or_none(queued) or 0), 0)
    # The address through the proxy, not the proxy's own.
    Device.objects.filter(device_id=device_id).update(**fields)


@login_required
@requires("pos.operate")
@require_GET
@requires_offline
def sync_status(request):
    """What the server holds, so a device can show its queue honestly."""
    today = timezone.localdate()
    sales = Sale.objects.filter(branch=request.branch, sold_at__date=today)
    return JsonResponse(
        {
            "server_time": timezone.now().isoformat(),
            "sales_today": sales.count(),
            "offline_origin_today": sales.filter(is_offline_origin=True).count(),
            "uuids": [str(u) for u in sales.values_list("client_uuid", flat=True)],
        }
    )


@login_required
@requires("pos.mobile_cart")
@require_POST
def cart_push(request):
    """
    Save a basket built in the aisle and give it a handoff code.

    This is what makes the phone a second device rather than a cheap till: the
    cart is a server object, so the cashier can pull it up by code.
    """
    try:
        body = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Malformed JSON."}, status=400)
    lines = body.get("lines") if isinstance(body, dict) else None
    if not isinstance(lines, list) or not lines:
        return JsonResponse({"error": "An empty basket cannot be handed off."}, status=400)

    if len(lines) > 200:
        return JsonResponse({"error": "That basket is too big."}, status=400)
    may_price = request.membership.can("pos.price_override")
    customer = None
    if body.get("customer_id"):
        from apps.customers.models import Customer

        customer = Customer.objects.filter(pk=int_or(body["customer_id"]),
                                           is_active=True).first()
        if customer is None:
            return JsonResponse({"error": "That customer is not in this shop."}, status=400)
    with transaction.atomic():
        return _build_held_cart(request, lines, may_price, customer)


def _build_held_cart(request, lines, may_price, customer=None):
    """All of the basket or none of it: a bad line used to leave half a cart."""
    from apps.pos.services import hold_cart

    # With the customer, so the basket is priced from their list and they
    # arrive at the till with it.
    cart = new_cart(user=request.user, branch=request.branch, customer=customer)
    for line in lines:
        if not isinstance(line, dict):
            transaction.set_rollback(True)
            return JsonResponse({"error": "Malformed line."}, status=400)
        qty = decimal_or_none(line.get("qty", 1))
        if qty is None or qty <= 0 or qty > 100000:
            transaction.set_rollback(True)
            return JsonResponse({"error": "Every quantity must be above zero."}, status=400)
        variant = None
        if line.get("variant_id"):
            variant = Variant.objects.filter(pk=decimal_or_none(line["variant_id"]) or 0).first()
            if variant is None:
                transaction.set_rollback(True)
                return JsonResponse({"error": "Unknown product."}, status=400)
        price = decimal_or_none(line.get("unit_price"))
        if variant is None:
            # An open item is a typed price: the biggest theft vector there is.
            if price is None or price <= 0 or not request.membership.can(
                "pos.open_item", value=price * qty
            ):
                transaction.set_rollback(True)
                return JsonResponse({"error": "Open items are not allowed here."}, status=403)
        elif not may_price:
            price = None  # the catalogue decides, not the phone
        try:
            add_to_cart(
                cart, variant, qty=qty, unit_price=price,
                description=str(line.get("description", ""))[:160],
                added_via=str(line.get("added_via", "camera"))[:12],
            )
        except ValueError as exc:
            transaction.set_rollback(True)
            return JsonResponse({"error": str(exc)}, status=400)

    code = hold_cart(cart)
    return JsonResponse({"code": code, "cart_id": cart.pk, "total": str(cart.subtotal)})


@login_required
@requires("pos.operate")
@require_POST
def cart_pull(request, code):
    """
    Collect a held basket at the till.

    A POST, because collecting converts the basket: as a GET, an <img> tag
    pointing at a guessed four-character code consumed a colleague's basket.
    """
    with transaction.atomic():
        cart = (
            Cart.objects.select_for_update()
            .filter(handoff_code=code.upper(), status=CartStatus.HELD, branch=request.branch)
            .first()
        )
        if cart is None:
            return JsonResponse({"error": "No held basket with that code."}, status=404)
        # Collected once: it now lives on this till. Left "held", the same
        # basket could be pulled at two tills and sold twice.
        cart.status = CartStatus.CONVERTED
        cart.save(update_fields=["status", "updated_at"])

    return JsonResponse(
        {
            "cart_id": cart.pk,
            "customer": cart.customer.name if cart.customer else None,
            "customer_id": cart.customer_id,
            "built_by": cart.user.name,
            "lines": [
                {
                    "variant_id": line.variant_id,
                    "cart_line_id": line.pk,
                    "decimal": bool(line.variant_id and
                                    line.variant.product.base_unit.allows_decimal),
                    "description": line.label,
                    "qty": str(line.qty),
                    "unit_price": str(line.unit_price),
                    "discount": str(line.discount),
                    "tax_rate": str(line.tax_rate),
                    "added_via": line.added_via,
                }
                for line in cart.lines.all()
            ],
            "total": str(cart.subtotal),
        }
    )


@login_required
@require_POST
def device_register(request):
    """
    A till or phone introduces itself, once, so the shop and the platform
    can see it -- and switch it off if it is lost.

    Nothing called this before, so no device was ever listed. Tills (who may
    use a till) and phones (who may build baskets) register; any plan.
    """
    membership = getattr(request, "membership", None)
    if membership is None or not (membership.can("pos.operate") or membership.can("pos.mobile_cart")):
        return JsonResponse({"error": "Not allowed."}, status=403)
    try:
        body = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Malformed JSON."}, status=400)
    device_id = str(body.get("device_id") or "").strip()[:64] if isinstance(body, dict) else ""
    if not device_id:
        return JsonResponse({"error": "device_id is required."}, status=400)
    if request.branch is None:
        return JsonResponse({"error": "No branch resolved for this user."}, status=400)

    # Device ids are unique across every shop. Looking only in this one and
    # then inserting crashed when another shop already had the same id.
    from apps.core.context import unscoped

    with unscoped():
        taken = Device.objects_all.filter(device_id=device_id).exclude(
            tenant=request.tenant
        ).exists()
    if taken:
        return JsonResponse({"error": "That device id belongs to another shop."}, status=409)

    from django.db import IntegrityError

    from apps.org.models import DeviceKind

    kind = body.get("kind") if body.get("kind") in DeviceKind.values else DeviceKind.TILL
    try:
        with transaction.atomic():
            device, created = Device.objects.get_or_create(
                device_id=device_id,
                defaults={
                    "tenant": request.tenant,
                    "branch": request.branch,
                    "label": str(body.get("label", ""))[:60],
                    "kind": kind,
                },
            )
    except IntegrityError:
        # Two registrations racing, or an id another shop just took.
        return JsonResponse({"error": "Try registering this device again."}, status=409)
    device.app_version = str(body.get("app_version", device.app_version) or "")[:20]
    device.last_sync_at = timezone.now()
    device.last_seen_ip = audit.client_ip(request)
    device.save(update_fields=["app_version", "last_sync_at", "last_seen_ip", "updated_at"])
    if not device.is_active:
        return JsonResponse({"error": "device_retired", "device_id": device.device_id}, status=409)

    return JsonResponse(
        {"device_id": device.device_id, "branch": device.branch.name, "created": created}
    )


def _price_text(price):
    return None if price is None else str(price)


def _list_price(variant, price_list_id, default=False):
    """
    The one-unit price on a list, from the prefetched rows (price_for ran a
    query per product). Same rule as Variant.price_for: the highest quantity
    break at or below one, else the lowest there is.
    """
    if price_list_id is None:
        return None
    rows = sorted((p for p in variant.prices.all() if p.price_list_id == price_list_id),
                  key=lambda p: p.min_qty)
    fits = [p for p in rows if p.min_qty <= 1]
    if fits:
        return fits[-1].amount
    # Only bulk tiers: not a price for one unit. For the shop's own list the
    # smallest tier still stands, as Variant.price_for does.
    if rows and default:
        return rows[0].amount
    return None


@login_required
@requires("pos.operate")
@require_GET
def customers_snapshot(request):
    """
    Customers for the till to pick from, offline included: name, phone, the
    price list they buy on and what credit they have left.
    """
    from decimal import Decimal

    from django.db.models import Sum
    from django.db.models.functions import Coalesce

    from apps.customers.models import Customer

    rows = (
        Customer.objects.filter(is_active=True)
        .annotate(owed=Coalesce(Sum("credit_transactions__amount"), Decimal("0")))
        .order_by("name")
        .values("pk", "name", "phone", "price_list_id", "credit_limit", "owed")[:5000]
    )
    from apps.catalog.models import PriceList

    live = dict(PriceList.objects.filter(is_active=True).values_list("pk", "name"))
    live_lists = set(live)
    return JsonResponse({
        "price_lists": {str(pk): name for pk, name in live.items()},
        "customers": [
            {
                "id": row["pk"], "name": row["name"], "phone": row["phone"],
                # Only while the list is on: a dead list kept pricing at tills.
                "price_list": (row["price_list_id"]
                               if row["price_list_id"] in live_lists else None),
                "credit_left": str(max(row["credit_limit"] - row["owed"], Decimal("0"))),
            }
            for row in rows
        ],
    })
