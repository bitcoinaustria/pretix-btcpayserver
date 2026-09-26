"""
Carry out what an invoice state means for its pretix payment (see state.py for the rules).

The webhook, the periodic poll and the payment flow all end up in ``apply``: it takes an invoice
read from the BTCPay API (never from a webhook payload), checks that it belongs to the payment,
applies the decision and accounts for every euro that arrived. Running it twice changes nothing
the second time.

Each payment has exactly one invoice. Money beyond the payment amount (over payments, a second
transfer to a settled invoice) becomes its own confirmed "surplus" payment, so pretix shows the
order as overpaid and the surplus can be refunded without touching the ticket's own payment.
"""
import json
import logging
import time
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Sum
from django.utils.timezone import now

from pretix.base.models import Order, OrderPayment, OrderRefund, Quota
from pretix.base.services.locking import LockTimeoutException
from pretix.base.services.orders import OrderError, extend_order
from pretix.helpers.database import OF_SELF

from . import state
from .api import BTCPayError

logger = logging.getLogger(__name__)

PROVIDER = "btcpay_greenfield"
# BTCPay watches an unconfirmed transaction for a day by default (store setting "invoice monitoring").
DEFAULT_MONITORING = timedelta(hours=24)
# How often the poll looks again: open invoices every run, final ones pretix has not caught up with every few minutes,
# settled and closed ones now and then while BTCPay still watches them (late money, admin changes, lost webhooks).
ACTIVE, CATCH_UP, WATCH = 60, 300, 1800
WINDOW = timedelta(days=7)


class InvoiceMismatch(Exception):
    """The invoice does not belong to the payment it claims to belong to."""


def event_ref(event) -> str:
    return f"{event.organizer.slug}/{event.slug}"


def _timestamp(value) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value), tz=dt_timezone.utc) if value else None
    except (TypeError, ValueError, OverflowError):
        return None


def _decimal(value) -> Decimal:
    try:
        return Decimal(str(value if value not in (None, "") else "0"))
    except InvalidOperation:
        return Decimal("0")


def is_surplus(payment: OrderPayment) -> bool:
    return bool(payment.info_data.get("surplus_of"))


def check_invoice(payment: OrderPayment, invoice: dict, store_id: str) -> None:
    """Raise InvoiceMismatch unless the invoice is the one of exactly this payment, amount and currency."""
    meta = invoice.get("metadata") or {}
    if not invoice.get("id") or invoice.get("id") != payment.info_data.get("invoice_id") or is_surplus(payment):
        raise InvoiceMismatch("invoice is not the invoice of this payment")
    if invoice.get("storeId") != store_id:
        raise InvoiceMismatch("invoice belongs to another store")
    if str(meta.get("pretixPaymentId")) != str(payment.pk) or meta.get("orderId") != payment.order.code:
        raise InvoiceMismatch("invoice metadata points to another payment")
    if meta.get("pretixEvent") != event_ref(payment.order.event):
        raise InvoiceMismatch("invoice belongs to another event")
    if _decimal(invoice.get("amount")) != payment.amount or invoice.get("currency") != payment.order.event.currency:
        raise InvoiceMismatch("invoice amount or currency differs from the payment")


def _update_info(payment: OrderPayment, **values) -> dict:
    """Merge ``values`` into the payment's info under a row lock; returns the new info."""
    with transaction.atomic():
        locked = OrderPayment.objects.select_for_update(of=OF_SELF).get(pk=payment.pk)
        info = locked.info_data
        info.update(values)
        locked.info_data = info
        locked.save(update_fields=["info"])
    payment.refresh_from_db()
    return info


def _note(payment: OrderPayment, note: str, invoice: dict, key: str | None = None) -> None:
    """Log something for the team once per payment (and ``key``, if the same note can recur with new facts)."""
    marker = f"{note}:{key}" if key else note
    with transaction.atomic():
        locked = OrderPayment.objects.select_for_update(of=OF_SELF).get(pk=payment.pk)
        info = locked.info_data
        noted = list(info.get("noted") or [])
        if marker in noted:
            return
        info["noted"] = noted + [marker]
        locked.info_data = info
        locked.save(update_fields=["info"])
    payment.refresh_from_db()
    payment.order.log_action(f"pretix_btcpay.{note}", data={
        "local_id": payment.local_id, "invoice_id": invoice.get("id"), "status": invoice.get("status"),
        "additional_status": invoice.get("additionalStatus"), "amount": str(invoice.get("amount")),
        "paid_amount": str(invoice.get("paidAmount")), "currency": invoice.get("currency"),
    })


def _hold_order(payment: OrderPayment, invoice: dict) -> None:
    """Keep the order reserved while BTCPay still waits for a seen payment to confirm."""
    order = Order.objects.get(pk=payment.order_id)
    until = state.hold_until(_timestamp(invoice.get("monitoringExpiration")), now(), DEFAULT_MONITORING)
    if order.status not in (Order.STATUS_PENDING, Order.STATUS_EXPIRED):
        return
    if order.status == Order.STATUS_PENDING and order.expires >= until - timedelta(minutes=5):
        return
    if "held_from" not in payment.info_data:
        # The deadline before the first extension, to give the seats back if the payment fails.
        _update_info(payment, held_from=int(order.expires.timestamp()))
    try:
        extend_order(order, until)
    except OrderError as e:
        # Expired and sold out in the meantime; the confirmation will say so once the payment settles.
        logger.info("BTCPay: could not extend order %s: %s", order.full_code, e)
        _note(payment, "hold_failed", invoice)


def _release_hold(payment: OrderPayment) -> None:
    """
    A seen payment was dropped (double spend, never confirmed): give the order its old payment deadline back, with
    a few minutes to pay again, instead of holding the seats for a day.
    """
    held_from = _timestamp(payment.info_data.get("held_from"))
    order = Order.objects.get(pk=payment.order_id)
    if not held_from or order.status != Order.STATUS_PENDING:
        return
    if order.payments.filter(state=OrderPayment.PAYMENT_STATE_PENDING).exclude(pk=payment.pk).exists():
        return  # another payment is on its way and needs the reservation
    back = max(held_from, now() + timedelta(minutes=10))
    if order.expires <= back:
        return
    try:
        extend_order(order, back)
        order.log_action("pretix_btcpay.hold_released", data={"local_id": payment.local_id, "expires": back.isoformat()})
    except OrderError as e:
        logger.info("BTCPay: could not shorten order %s: %s", order.full_code, e)


def _mark_pending(payment: OrderPayment) -> None:
    with transaction.atomic():
        locked = OrderPayment.objects.select_for_update(of=OF_SELF).get(pk=payment.pk)
        if locked.state == OrderPayment.PAYMENT_STATE_CREATED:
            locked.state = OrderPayment.PAYMENT_STATE_PENDING
            locked.save(update_fields=["state"])
            payment.order.log_action("pretix_btcpay.processing", data={"local_id": payment.local_id})
    payment.refresh_from_db()


def _order_paid_enough(order: Order) -> bool:
    paid = order.payments.filter(
        state__in=(OrderPayment.PAYMENT_STATE_CONFIRMED, OrderPayment.PAYMENT_STATE_REFUNDED)
    ).aggregate(s=Sum("amount"))["s"] or Decimal("0.00")
    refunded = order.refunds.filter(
        state__in=(OrderRefund.REFUND_STATE_DONE, OrderRefund.REFUND_STATE_TRANSIT, OrderRefund.REFUND_STATE_CREATED)
    ).aggregate(s=Sum("amount"))["s"] or Decimal("0.00")
    return paid - refunded >= order.total


def _catch_up_order(payment: OrderPayment, invoice: dict) -> None:
    """
    pretix confirms a payment first and marks the order paid afterwards; if the second step failed (a lock timeout,
    a crash), redoing confirm() does nothing, because the payment is already confirmed. Finish it here. If the seats
    are really gone, stop and let a human decide.
    """
    order = Order.objects.get(pk=payment.order_id)
    if order.status not in (Order.STATUS_PENDING, Order.STATUS_EXPIRED) or payment.info_data.get("quota_exceeded"):
        return
    try:
        with transaction.atomic():
            # The webhook and the poll may both get here: the payment row makes them take turns, and the second one
            # finds the order paid.
            locked = OrderPayment.objects.select_for_update(of=OF_SELF).get(pk=payment.pk)
            order.refresh_from_db()
            if order.status not in (Order.STATUS_PENDING, Order.STATUS_EXPIRED) or not _order_paid_enough(order):
                return
            locked._mark_order_paid()
        order.log_action("pretix_btcpay.order_caught_up", data={"local_id": payment.local_id})
    except Quota.QuotaExceededException as e:
        logger.warning("BTCPay: payment %s is confirmed, but order %s can not be marked paid: %s",
                       payment.full_id, order.full_code, e)
        _update_info(payment, quota_exceeded=True)
        _note(payment, "quota_exceeded", invoice)


def _record_surplus(payment: OrderPayment, invoice: dict) -> None:
    """
    Book money beyond the payment amount as its own confirmed payment, once per euro: pretix then shows the order as
    overpaid, and refunding the surplus does not eat into the ticket. BTCPay reports paidAmount in the invoice
    currency, at the rate of the invoice.
    """
    surplus = _decimal(invoice.get("paidAmount")) - payment.amount
    if surplus <= 0:
        return
    if Order.objects.get(pk=payment.order_id).status != Order.STATUS_PAID:
        # Booking it now could mark an order paid that lost its seats; the team sees the note and decides.
        _note(payment, state.NOTE_OVERPAID, invoice, key=str(surplus))
        return
    with transaction.atomic():
        locked = OrderPayment.objects.select_for_update(of=OF_SELF).get(pk=payment.pk)
        info = locked.info_data
        delta = surplus - _decimal(info.get("surplus_recorded"))
        if delta <= 0:
            return
        info["surplus_recorded"] = str(surplus)
        locked.info_data = info
        locked.save(update_fields=["info"])
        extra = locked.order.payments.create(
            provider=PROVIDER, amount=delta, state=OrderPayment.PAYMENT_STATE_CREATED,
            info=json.dumps({"surplus_of": locked.pk, "invoice_id": invoice.get("id"), "status": state.SETTLED}),
        )
    payment.refresh_from_db()
    # The order is paid already: confirm() only books the money and leaves the order as it is.
    extra.confirm(send_mail=False)
    _note(payment, state.NOTE_OVERPAID, invoice, key=str(surplus))


def _close_siblings(payment: OrderPayment) -> None:
    """Once an order is paid, cancel its other open BTCPay payments, so the buyer is not asked to pay again."""
    order = Order.objects.get(pk=payment.order_id)
    if order.status != Order.STATUS_PAID:
        return
    for other in order.payments.filter(provider=PROVIDER, state=OrderPayment.PAYMENT_STATE_CREATED).exclude(pk=payment.pk):
        try:
            other.payment_provider.cancel_payment(other)
            order.log_action("pretix_btcpay.sibling_cancelled", data={"local_id": other.local_id})
        except Exception:
            logger.exception("BTCPay: could not cancel open payment %s of paid order", other.full_id)


def apply(provider, payment: OrderPayment, invoice: dict, source: str) -> str:
    """Apply the decision for ``invoice`` to ``payment``. Returns the action taken."""
    check_invoice(payment, invoice, str(provider.settings.store_id))
    paid = _decimal(invoice.get("paidAmount"))
    _update_info(payment, status=invoice.get("status"), additional_status=invoice.get("additionalStatus"),
                 paid_amount=str(paid), monitoring_until=invoice.get("monitoringExpiration"), checked=int(time.time()))
    decision = state.decide(invoice.get("status"), invoice.get("additionalStatus"), payment.state, paid=paid > 0)
    for note in decision.notes:
        if note != state.NOTE_OVERPAID:  # booked with its amount below
            _note(payment, note, invoice)

    if decision.action == "pending":
        _hold_order(payment, invoice)  # first secure the seats, then show the payment as on its way
        _mark_pending(payment)
    elif decision.action == "hold":
        _hold_order(payment, invoice)
    elif decision.action == "fail":
        payment.fail(info=payment.info_data, log_data={"source": source, "invoice_id": invoice.get("id"),
                                                      "status": invoice.get("status")})
        _release_hold(payment)
    elif decision.action == "confirm":
        try:
            payment.confirm()
        except Quota.QuotaExceededException as e:
            # The payment counts, the order could not be marked paid (seats gone after it expired).
            logger.warning("BTCPay: payment %s confirmed but order not paid: %s", payment.full_id, e)
            _update_info(payment, quota_exceeded=True)
            _note(payment, "quota_exceeded", invoice)
        payment.refresh_from_db()

    if invoice.get("status") == state.SETTLED and payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED:
        _catch_up_order(payment, invoice)
        _record_surplus(payment, invoice)
        _close_siblings(payment)
    if (decision.action != "fail" and invoice.get("status") in (state.EXPIRED, state.INVALID)
            and payment.info_data.get("held_from")):
        # Money that was on its way to a given-up payment never arrived: the seats are free again.
        _release_hold(payment)
    return decision.action


def sync_payment(provider, payment: OrderPayment, source: str) -> str | None:
    """Read the payment's invoice from BTCPay and apply it."""
    invoice_id = payment.info_data.get("invoice_id")
    if not invoice_id or is_surplus(payment):
        return None
    invoice = provider.client.get_invoice(str(provider.settings.store_id), invoice_id)
    return apply(provider, payment, invoice, source)


def next_check(payment: OrderPayment, at: datetime) -> float | None:
    """When the poll should ask BTCPay about this payment's invoice again (unix time), or None for never."""
    info = payment.info_data
    if not info.get("invoice_id") or is_surplus(payment):
        return None
    checked = float(info.get("checked") or 0)
    status = info.get("status")
    monitoring = _timestamp(info.get("monitoring_until"))
    counted = payment.state in (OrderPayment.PAYMENT_STATE_CONFIRMED, OrderPayment.PAYMENT_STATE_REFUNDED)
    if not state.is_final(status, monitoring, at):
        return checked + ACTIVE
    if status == state.SETTLED and not counted:
        return checked + CATCH_UP  # pretix has not caught up with a settled invoice
    if (status == state.SETTLED and payment.order.status in (Order.STATUS_PENDING, Order.STATUS_EXPIRED)
            and not info.get("quota_exceeded")):
        return checked + CATCH_UP  # confirmed, but the order was never marked paid
    if status in (state.EXPIRED, state.INVALID) and payment.state in (OrderPayment.PAYMENT_STATE_CREATED,
                                                                    OrderPayment.PAYMENT_STATE_PENDING):
        return checked + CATCH_UP
    if status in (state.EXPIRED, state.INVALID) and _decimal(info.get("paid_amount")) > 0 and not counted:
        return checked + WATCH  # money on a closed invoice, until someone decides in BTCPay
    if (monitoring is None or monitoring > at) or payment.created >= at - timedelta(days=2):
        return checked + WATCH  # late money on a settled invoice, a manual change, a lost webhook
    return None


def poll(budget: int = 300) -> dict:
    """
    The fallback for webhooks that never arrived: ask BTCPay about the invoices that are due, longest unchecked
    first, at most ``budget`` per run. Payments that failed or were cancelled are included, because money can still
    arrive on their invoice; so are events that stopped selling.
    """
    from django_scopes import scope, scopes_disabled

    stats = {"due": 0, "checked": 0, "changed": 0, "errors": 0}
    at = now()
    with scopes_disabled():
        candidates = list(
            OrderPayment.objects.filter(provider=PROVIDER, created__gte=at - WINDOW)
            .exclude(state=OrderPayment.PAYMENT_STATE_REFUNDED)
            .select_related("order", "order__event", "order__event__organizer")
        )
    due = []
    for payment in candidates:
        when = next_check(payment, at)
        if when is not None and when <= at.timestamp():
            due.append((when, payment))
    due.sort(key=lambda item: item[0])
    stats["due"] = len(due)
    providers = {}
    for _, payment in due[:budget]:
        event = payment.order.event
        with scope(organizer=event.organizer):
            if event.pk not in providers:
                providers[event.pk] = event.get_payment_providers().get(PROVIDER)
            provider = providers[event.pk]
            if not provider or not provider.configured:
                continue
            stats["checked"] += 1
            try:
                before = (payment.state, payment.order.status)
                sync_payment(provider, payment, "poll")
                payment.refresh_from_db()
                payment.order.refresh_from_db()
                stats["changed"] += int((payment.state, payment.order.status) != before)
            except (BTCPayError, InvoiceMismatch, LockTimeoutException) as e:
                stats["errors"] += 1
                _update_info(payment, checked=int(time.time()))  # try the others first next time
                logger.warning("BTCPay: poll of %s failed: %s", payment.full_id, e)
    return stats
