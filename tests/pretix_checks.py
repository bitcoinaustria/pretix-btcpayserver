"""
Checks of sync.apply against a real pretix, with made-up invoices and no BTCPay: the cases that are hard to produce on
regtest (wrong amount, other event, invalid after confirmation, repeated deliveries). Run in a pretix with the plugin
configured for an event in test mode, for example the local test pretix of the Bitcoin Zitadelle:

    docker exec -i <pretix container> pretix shell < tests/pretix_checks.py

It creates test-mode orders in that event, prints one line per check (the last says whether all passed) and deletes
its orders again.
"""
import json
import os
from datetime import timedelta
from decimal import Decimal

from django.utils.timezone import now
from django_scopes import scope, scopes_disabled
from pretix.base.models import Event, Order, OrderPayment

from pretix_btcpay import state, sync

ORGANIZER, EVENT = os.environ.get("CHECK_EVENT", "zitadelle/2027").split("/")
results = []


def check(ok, message):
    results.append(bool(ok))
    print(("  ✓ " if ok else "  ✗ ") + message)


with scopes_disabled():
    event = Event.objects.get(organizer__slug=ORGANIZER, slug=EVENT)
with scope(organizer=event.organizer):
    provider = event.get_payment_providers()[sync.PROVIDER]
    store = str(provider.settings.store_id)
    channel = event.organizer.sales_channels.get(identifier="web")
    counter = [0]
    created = []

    def order_with_payment(minutes=30):
        counter[0] += 1
        order = Order.objects.create(event=event, status=Order.STATUS_PENDING, expires=now() + timedelta(minutes=minutes),
                                     total=Decimal("266.00"), testmode=True, email="checks@example.org", locale="de",
                                     sales_channel=channel)
        created.append(order.pk)
        invoice_id = f"CHECK{order.code}{counter[0]}"
        payment = order.payments.create(provider=sync.PROVIDER, amount=order.total, state=OrderPayment.PAYMENT_STATE_CREATED,
                                        info=json.dumps({"invoice_id": invoice_id}))
        return order, payment, invoice_id

    def invoice(payment, invoice_id, status, extra="None", **kw):
        data = {"id": invoice_id, "storeId": store, "amount": "266.00", "currency": event.currency, "status": status,
                "additionalStatus": extra, "paidAmount": "266.00",
                "monitoringExpiration": int((now() + timedelta(hours=24)).timestamp()),
                "metadata": {"orderId": payment.order.code, "pretixPaymentId": payment.pk, "pretixEvent": f"{ORGANIZER}/{EVENT}"}}
        data.update(kw)
        return data

    def reload(order, payment):
        order.refresh_from_db()
        payment.refresh_from_db()

    print("Abweichende Rechnungen")
    order, payment, inv = order_with_payment()
    for label, bad in (("Betrag", {"amount": "26.60"}), ("Währung", {"currency": "USD"}), ("Laden", {"storeId": "fremd"}),
                       ("Event", {"metadata": {"orderId": order.code, "pretixPaymentId": payment.pk, "pretixEvent": "x/y"}}),
                       ("Zahlung", {"metadata": {"orderId": order.code, "pretixPaymentId": payment.pk + 999, "pretixEvent": f"{ORGANIZER}/{EVENT}"}}),
                       ("Rechnung", {"id": "FREMD"})):
        try:
            sync.apply(provider, payment, invoice(payment, inv, state.SETTLED, **bad), "check")
            reload(order, payment)
            check(False, f"{label} abweichend: angenommen")
        except sync.InvoiceMismatch:
            reload(order, payment)
            check(payment.state == "created" and order.status == "n", f"{label} abweichend: abgelehnt, nichts bezahlt")

    print("Unterwegs, dann fallen gelassen")
    order, payment, inv = order_with_payment()
    before = order.expires
    sync.apply(provider, payment, invoice(payment, inv, state.PROCESSING), "check")
    reload(order, payment)
    check(payment.state == "pending" and order.expires > now() + timedelta(hours=24), "Processing: offen, einen Tag reserviert")
    sync.apply(provider, payment, invoice(payment, inv, state.PROCESSING), "check")
    reload(order, payment)
    check(payment.state == "pending", "noch einmal Processing: unverändert")
    sync.apply(provider, payment, invoice(payment, inv, state.INVALID), "check")
    reload(order, payment)
    check(payment.state == "failed" and abs((order.expires - before).total_seconds()) < 5, "Invalid: gescheitert, alte Frist zurück")

    print("Bestätigt, dann für ungültig erklärt")
    order, payment, inv = order_with_payment()
    sync.apply(provider, payment, invoice(payment, inv, state.SETTLED), "check")
    reload(order, payment)
    check(payment.state == "confirmed" and order.status == "p", "Settled: bestätigt, bezahlt")
    sync.apply(provider, payment, invoice(payment, inv, state.SETTLED), "check")
    confirmed_logs = order.all_logentries().filter(action_type="pretix.event.order.payment.confirmed").count()
    check(confirmed_logs == 1, f"doppelt zugestellt: einmal bestätigt ({confirmed_logs})")
    sync.apply(provider, payment, invoice(payment, inv, state.INVALID, "Marked"), "check")
    reload(order, payment)
    notes = order.all_logentries().filter(action_type="pretix_btcpay.invalid_after_confirm").count()
    check(payment.state == "confirmed" and order.status == "p" and notes == 1, "Invalid danach: bleibt bezahlt, vermerkt für einen Menschen")

    print("Überzahlt, zweimal gemeldet, dann noch mehr")
    order, payment, inv = order_with_payment()
    for _ in range(2):
        sync.apply(provider, payment, invoice(payment, inv, state.SETTLED, "PaidOver", paidAmount="399.00"), "check")
    reload(order, payment)
    surplus = list(order.payments.filter(state="confirmed").exclude(pk=payment.pk).values_list("amount", flat=True))
    check(order.status == "p" and surplus == [Decimal("133.00")], f"bezahlt, Überschuss einmal als eigene Zahlung gebucht ({surplus})")
    sync.apply(provider, payment, invoice(payment, inv, state.SETTLED, "None", paidAmount="460.00"), "check")
    surplus = sorted(order.payments.filter(state="confirmed").exclude(pk=payment.pk).values_list("amount", flat=True))
    check(surplus == [Decimal("61.00"), Decimal("133.00")], f"später noch mehr Geld, auch ohne „PaidOver“: der Rest gebucht ({surplus})")
    extra = order.payments.filter(state="confirmed").exclude(pk=payment.pk).first()
    check(sync.next_check(extra, now()) is None, "die Überschuss-Zahlung fragt BTCPay nie selbst")

    print("Gescheitert, dann doch bezahlt")
    order, payment, inv = order_with_payment()
    sync.apply(provider, payment, invoice(payment, inv, state.EXPIRED), "check")
    reload(order, payment)
    check(payment.state == "failed" and order.status == "n", "Expired: gescheitert")
    sync.apply(provider, payment, invoice(payment, inv, state.EXPIRED, "PaidLate"), "check")
    reload(order, payment)
    check(payment.state == "failed" and order.all_logentries().filter(action_type="pretix_btcpay.paid_late").exists(), "PaidLate: bleibt gescheitert, vermerkt")
    sync.apply(provider, payment, invoice(payment, inv, state.SETTLED, "Marked"), "check")
    reload(order, payment)
    check(payment.state == "confirmed" and order.status == "p", "in BTCPay auf bezahlt gesetzt: bezahlt")

    print("Storniert, Geld unterwegs, dann fallen gelassen")
    order, payment, inv = order_with_payment()
    before = order.expires
    payment.state = OrderPayment.PAYMENT_STATE_CANCELED
    payment.save(update_fields=["state"])
    sync.apply(provider, payment, invoice(payment, inv, state.PROCESSING), "check")
    reload(order, payment)
    check(payment.state == "canceled" and order.expires > now() + timedelta(hours=24), "Processing auf stornierter Zahlung: Bestellung reserviert")
    sync.apply(provider, payment, invoice(payment, inv, state.EXPIRED), "check")
    reload(order, payment)
    check(payment.state == "canceled" and abs((order.expires - before).total_seconds()) < 5, "Expired danach: alte Frist zurück")

    print("Erstattet, dann noch einmal bezahlt")
    order, payment, inv = order_with_payment()
    payment.state = OrderPayment.PAYMENT_STATE_REFUNDED
    payment.save(update_fields=["state"])
    sync.apply(provider, payment, invoice(payment, inv, state.SETTLED), "check")
    reload(order, payment)
    check(payment.state == "refunded" and order.all_logentries().filter(action_type="pretix_btcpay.settled_after_refund").exists(), "bleibt erstattet, vermerkt")

    print("Geld auf einer geschlossenen Rechnung")
    order, payment, inv = order_with_payment()
    payment.state = OrderPayment.PAYMENT_STATE_CANCELED
    payment.save(update_fields=["state"])
    sync.apply(provider, payment, invoice(payment, inv, state.INVALID, "Marked", paidAmount="266.00"), "check")
    reload(order, payment)
    noted = order.all_logentries().filter(action_type="pretix_btcpay.money_on_closed_invoice").exists()
    check(payment.state == "canceled" and noted, "ungültig markiert, aber bezahlt: vermerkt für einen Menschen")
    check(sync.next_check(payment, now()) is not None, "und der Abgleich fragt weiter nach, bis in BTCPay entschieden ist")

    print("Bestätigung abgebrochen")
    order, payment, inv = order_with_payment()
    # Wie nach einem Lock-Timeout mitten in confirm(): die Zahlung bestätigt, die Bestellung nicht bezahlt.
    payment.state = OrderPayment.PAYMENT_STATE_CONFIRMED
    payment.info_data = {**payment.info_data, "status": state.SETTLED, "checked": 0,
                         "monitoring_until": int((now() - timedelta(hours=1)).timestamp())}
    payment.save(update_fields=["state", "info"])
    check(sync.next_check(payment, now()) is not None, "der Abgleich merkt, dass die Bestellung nicht bezahlt ist")
    sync.apply(provider, payment, invoice(payment, inv, state.SETTLED), "check")
    reload(order, payment)
    check(order.status == "p" and order.all_logentries().filter(action_type="pretix_btcpay.order_caught_up").exists(),
          "noch einmal gemeldet: die Bestellung wird nachträglich bezahlt")

    print("Zeitplan des Abgleichs")
    order, payment, inv = order_with_payment()
    payment.info_data = {**payment.info_data, "status": state.NEW, "checked": 1000}
    payment.save(update_fields=["info"])
    check(sync.next_check(payment, now()) == 1000 + sync.ACTIVE, "offen: jede Minute")
    payment.info_data = {**payment.info_data, "status": state.SETTLED}
    payment.save(update_fields=["info"])
    check(sync.next_check(payment, now()) == 1000 + sync.CATCH_UP, "Settled, aber nicht bestätigt: bald wieder")

    print("Abgleich bei 500 offenen Zahlungen")
    from pretix_btcpay.api import BTCPayAPI, BTCPayError
    many = [order_with_payment()[1] for _ in range(500)]
    ids = {p.pk for p in many}
    asked = []
    original = BTCPayAPI.get_invoice

    def fake(self, store_id, invoice_id):
        asked.append(invoice_id)
        raise BTCPayError("not found", status=404)

    BTCPayAPI.get_invoice = fake
    try:
        first = sync.poll(budget=300)
        seen_first = set(asked)
        asked.clear()
        second = sync.poll(budget=300)
        seen_second = set(asked)
    finally:
        BTCPayAPI.get_invoice = original
    ours = {OrderPayment.objects.get(pk=pk).info_data["invoice_id"] for pk in ids}
    check(first["checked"] == 300 and len(seen_first) == 300, f"erster Lauf: 300 von {first['due']} fälligen")
    check(len(ours - seen_first - seen_second) == 0, "zweiter Lauf: zuerst die, die noch nie dran waren; keine bleibt liegen")

    from django.db import transaction
    with transaction.atomic():
        Order.gracefully_delete_bulk(event, Order.objects.filter(pk__in=created, testmode=True))

print(f"{'ALLE BESTANDEN' if all(results) else 'NICHT BESTANDEN'}: {sum(results)} von {len(results)}")
