"""
Pure decision logic: what a BTCPay invoice state means for a pretix payment.

Nothing in here talks to Django, pretix or the network, so it can be tested on its own
(``python -m unittest discover tests``). ``sync.py`` carries the decisions out.

BTCPay Greenfield invoice states (``status``):

- ``New``: waiting for payment, maybe partially paid (``additionalStatus`` ``PaidPartial``).
- ``Processing``: paid in full (or more), but not yet confirmed as the store's or the
  invoice's speed policy requires. The transaction may still be replaced or never confirm.
- ``Settled``: paid and confirmed. ``additionalStatus`` ``PaidOver`` if more than due was
  paid, ``Marked`` if a store admin set it to settled by hand.
- ``Expired``: not paid in full in time. ``PaidPartial`` if something arrived,
  ``PaidLate`` if the full amount arrived only after the invoice expired.
- ``Invalid``: the payment failed (for example double spent) or an admin marked it invalid.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta

CREATED = "created"
PENDING = "pending"
CONFIRMED = "confirmed"
FAILED = "failed"
CANCELED = "canceled"
REFUNDED = "refunded"

NEW = "New"
PROCESSING = "Processing"
SETTLED = "Settled"
EXPIRED = "Expired"
INVALID = "Invalid"
KNOWN_STATUSES = (NEW, PROCESSING, SETTLED, EXPIRED, INVALID)

# Things worth telling the team about, each logged once per payment.
NOTE_PARTIAL = "paid_partial"
NOTE_OVERPAID = "paid_over"
NOTE_LATE = "paid_late"
NOTE_MARKED = "marked"
NOTE_INVALID_AFTER_CONFIRM = "invalid_after_confirm"
NOTE_SETTLED_AFTER_REFUND = "settled_after_refund"
NOTE_MONEY_ON_CLOSED = "money_on_closed_invoice"


@dataclass(frozen=True)
class Decision:
    """
    ``action`` is one of ``none``, ``pending`` (payment seen: mark the payment pending and keep
    the order reserved), ``hold`` (only keep the order reserved), ``confirm`` or ``fail``.
    ``notes`` are things to log for a human.
    """
    action: str = "none"
    notes: tuple = field(default_factory=tuple)


def decide(status: str, additional_status: str | None, payment_state: str, paid: bool = False) -> Decision:
    """
    What to do with a payment in ``payment_state`` whose invoice is in ``status`` /
    ``additional_status``; ``paid`` says whether any money arrived on the invoice. Idempotent:
    deciding again after the action was carried out yields ``none`` (apart from notes, which
    the caller logs only once).
    """
    extra = additional_status or "None"
    notes = []
    if extra == "PaidPartial":
        notes.append(NOTE_PARTIAL)
    if extra == "PaidOver":
        notes.append(NOTE_OVERPAID)
    if extra == "PaidLate":
        notes.append(NOTE_LATE)
    if extra == "Marked" and status in (SETTLED, INVALID):
        notes.append(NOTE_MARKED)
    if (status in (EXPIRED, INVALID) and paid and payment_state not in (CONFIRMED, REFUNDED)
            and extra not in ("PaidPartial", "PaidLate")):
        # Money arrived on an invoice that no longer counts, for example one invalidated while the
        # buyer was paying it: nothing confirms it on its own, so a human has to look.
        notes.append(NOTE_MONEY_ON_CLOSED)

    if status == SETTLED:
        if payment_state == CONFIRMED:
            return Decision("none", tuple(notes))
        if payment_state == REFUNDED:
            # Money arrived for a payment pretix already considers refunded: never flip it back.
            return Decision("none", tuple(notes + [NOTE_SETTLED_AFTER_REFUND]))
        # Also from failed or canceled: the money is there, it must not get lost. This covers
        # a buyer who switched the payment method and then paid the old invoice, and an
        # expired invoice that a store admin marked as settled after a late payment.
        return Decision("confirm", tuple(notes))

    if status == PROCESSING:
        if payment_state == CREATED:
            return Decision("pending", tuple(notes))
        if payment_state in (PENDING, FAILED, CANCELED):
            # Money is on its way (also for an invoice whose payment was given up in pretix):
            # keep the order reserved until BTCPay settles or drops it.
            return Decision("hold", tuple(notes))
        return Decision("none", tuple(notes))

    if status in (EXPIRED, INVALID):
        if payment_state in (CREATED, PENDING):
            return Decision("fail", tuple(notes))
        if status == INVALID and payment_state == CONFIRMED:
            # Settled invoices do not become invalid on their own; an admin did this in BTCPay.
            # Reverting a confirmed payment would break pretix' books, so a human decides.
            return Decision("none", tuple(notes + [NOTE_INVALID_AFTER_CONFIRM]))
        return Decision("none", tuple(notes))

    # New (possibly partially paid) or a status this plugin does not know: wait.
    return Decision("none", tuple(notes))


def is_final(status: str | None, monitoring_until: datetime | None, now: datetime) -> bool:
    """Whether an invoice can still change in a way that matters, used to stop polling it."""
    if status in (SETTLED, INVALID):
        return True
    if status == EXPIRED:
        # A late payment can still arrive while BTCPay monitors the invoice.
        return monitoring_until is None or monitoring_until < now
    return False


def invoice_minutes(order_expires: datetime | None, now: datetime, cap: int) -> int:
    """
    Minutes a new invoice may run: never beyond the order's payment deadline (after it the
    seats may be sold again, and the exchange rate is only fixed while the invoice runs),
    never more than ``cap``, and at least one (BTCPay refuses shorter invoices).
    """
    cap = max(1, int(cap))
    if order_expires is None:
        return cap
    left = int((order_expires - now) / timedelta(minutes=1))
    return max(1, min(cap, left))


def hold_until(monitoring_expiration: datetime | None, now: datetime, fallback: timedelta) -> datetime:
    """
    How long to keep an order reserved once its payment was seen but not confirmed: until
    BTCPay stops monitoring the invoice, plus an hour for the last webhook and poll.
    """
    base = monitoring_expiration if monitoring_expiration and monitoring_expiration > now else now + fallback
    return base + timedelta(hours=1)


SPEED_POLICIES = ("MediumSpeed", "LowMediumSpeed", "LowSpeed")
