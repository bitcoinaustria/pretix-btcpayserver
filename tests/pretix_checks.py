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

from django.db import transaction as _tx
from django.utils.timezone import now
from django_scopes import scope, scopes_disabled
from pretix.base.models import Event, Order, OrderPayment

from pretix_btcpay import state, sync
from pretix_btcpay.api import BTCPayAPI

# The payments on the made-up invoices, as (status, euros) at a rate of 1: one confirmed payment of the ticket price,
# unless a check says otherwise.
PAYMENTS = {}
BTCPayAPI.get_invoice_payment_methods = lambda self, store_id, invoice_id: [
    {"paymentMethodId": "BTC-CHAIN", "rate": "1",
     "payments": [{"status": s, "value": v} for s, v in PAYMENTS.get(invoice_id, [("Settled", "266.00")])]}]

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
        with _tx.atomic():  # wie pretix: die Bestellung mit ihren Buchungen
            order = Order.objects.create(event=event, status=Order.STATUS_PENDING, expires=now() + timedelta(minutes=minutes),
                                         total=Decimal("266.00"), testmode=True, email="checks@example.org", locale="de",
                                         sales_channel=channel)
            order.create_transactions()
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

    from django.db import connections
    from pretix_btcpay import locks
    other = connections.create_connection("default")  # ein zweiter Arbeiter mit eigener Datenbanksitzung

    def other_try(space, key):
        with other.cursor() as c:
            c.execute("SELECT pg_try_advisory_lock(%s, %s)", [space, key % 2 ** 31])
            return c.fetchone()[0]

    def other_release(space, key):
        with other.cursor() as c:
            c.execute("SELECT pg_advisory_unlock(%s, %s)", [space, key % 2 ** 31])

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
    PAYMENTS[inv] = [("Settled", "266.00"), ("Processing", "133.00")]
    sync.apply(provider, payment, invoice(payment, inv, state.SETTLED, "PaidOver", paidAmount="399.00"), "check")
    reload(order, payment)
    check(order.status == "p" and not order.payments.filter(state="confirmed").exclude(pk=payment.pk).exists(),
          "zweite Zahlung noch unbestätigt: bezahlt, aber kein Überschuss gebucht")
    PAYMENTS[inv] = [("Settled", "266.00"), ("Invalid", "50.00"), ("Settled", "133.00")]
    for _ in range(2):
        sync.apply(provider, payment, invoice(payment, inv, state.SETTLED, "PaidOver", paidAmount="399.00"), "check")
    reload(order, payment)
    surplus = list(order.payments.filter(state="confirmed").exclude(pk=payment.pk).values_list("amount", flat=True))
    check(order.status == "p" and surplus == [Decimal("133.00")], f"bezahlt, Überschuss einmal als eigene Zahlung gebucht ({surplus})")
    PAYMENTS[inv] = [("Settled", "266.00"), ("Settled", "133.00"), ("Settled", "61.00")]
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

    print("Abgeschlossen, aber nie gezählt")
    order, payment, inv = order_with_payment()
    OrderPayment.objects.filter(pk=payment.pk).update(created=now() - timedelta(days=10), state=OrderPayment.PAYMENT_STATE_FAILED)
    payment.refresh_from_db()
    payment.info_data = {**payment.info_data, "status": state.EXPIRED, "checked": 1000,
                         "monitoring_until": int((now() - timedelta(days=9)).timestamp())}
    payment.save(update_fields=["info"])
    check(sync.next_check(payment, now()) == 1000 + sync.TERMINAL, "abgelaufen und alt: alle sechs Stunden, falls BTCPay sie später doch bezahlt setzt")

    print("Vorgemerkter Webhook")
    import json as _json
    active = (provider.settings.webhook_id, provider.settings.webhook_secret)
    provider.settings.set("pending_webhook", _json.dumps({"id": "NEU", "secret": "neu", "url": "https://anders.example", "store_id": "ANDERS"}))
    provider.promote_webhook()
    check((provider.settings.webhook_id, provider.settings.webhook_secret) == active and provider.settings.pending_webhook,
          "für eine andere Adresse und einen anderen Laden: bleibt vorgemerkt, der alte gilt weiter")
    provider.settings.set("pending_webhook", _json.dumps({"id": active[0], "secret": active[1],
                                                         "url": str(provider.settings.url).rstrip("/"), "store_id": str(provider.settings.store_id)}))
    provider.promote_webhook()
    check(not provider.settings.pending_webhook and provider.settings.webhook_id == active[0], "für die gespeicherten Einstellungen: wird der aktive")
    here = {"id": active[0], "secret": active[1], "url": str(provider.settings.url).rstrip("/"), "store_id": str(provider.settings.store_id)}
    provider.settings.set("pending_webhook", _json.dumps(here))
    with scopes_disabled():
        stale = Event.objects.get(pk=event.pk).get_payment_providers()[sync.PROVIDER]
    stale.settings.get("pending_webhook")  # dieses Event hält jetzt die Vormerkung von eben im Speicher
    newer = _json.dumps({"id": "B", "secret": "b", "url": "https://b.example", "store_id": "B"})
    provider.settings.set("pending_webhook", newer)  # inzwischen: ein anderer Laden vorgemerkt, noch nicht gespeichert
    stale.promote_webhook()
    event.settings.flush()
    check(provider.settings.pending_webhook == newer and provider.settings.webhook_id == active[0],
          "veraltete Einstellungen im Speicher: liest aus der Datenbank, übernimmt nichts, die neuere Vormerkung bleibt")
    provider.settings.set("pending_webhook", _json.dumps(here))
    check(other_try(locks.WEBHOOK, event.pk), "jemand speichert gerade die Einstellungen")
    provider.promote_webhook()
    event.settings.flush()
    check(provider.settings.pending_webhook == _json.dumps(here), "währenddessen: nichts übernommen, erst danach")
    other_release(locks.WEBHOOK, event.pk)
    provider.promote_webhook()
    event.settings.flush()
    check(not provider.settings.pending_webhook and provider.settings.webhook_id == active[0], "danach: übernommen")

    print("Eine Bestellung, ein Arbeiter")
    order, payment, inv = order_with_payment()
    check(other_try(locks.ORDER, order.pk), "ein anderer Arbeiter hält die Bestellung")
    try:
        sync.apply(provider, payment, invoice(payment, inv, state.SETTLED), "check")
        check(False, "besetzte Bestellung: trotzdem angewendet")
    except sync.OrderBusy:
        reload(order, payment)
        check(payment.state == "created" and order.status == "n", "besetzte Bestellung: OrderBusy, nichts geändert (Webhook bekommt 503, BTCPay stellt neu zu)")
    other_release(locks.ORDER, order.pk)
    sync.apply(provider, payment, invoice(payment, inv, state.SETTLED), "check")
    reload(order, payment)
    free = other_try(locks.ORDER, order.pk)
    other_release(locks.ORDER, order.pk)
    check(order.status == "p" and free, "frei: angewendet, Sperre danach wieder frei")
    # Die Verbindung bricht ab und wird neu aufgebaut: die alte Sitzung nimmt ihre Sperre mit, die innere nimmt sie neu.
    from django.db import DatabaseError, connection as _conn
    order, payment, inv = order_with_payment()
    with sync.order_lock(order.pk):
        _conn.close()
        with sync.order_lock(order.pk):
            inner = other_try(locks.ORDER, order.pk)
        released = other_try(locks.ORDER, order.pk)
        other_release(locks.ORDER, order.pk)
    check(not inner and released, "nach einem Verbindungsabbruch: auf der neuen Sitzung wieder gesperrt, danach frei")
    # Entsperren schlägt fehl: die Sitzung wird geschlossen, damit keine Sperre an einer offenen Verbindung hängen bleibt.
    original_release = locks._release
    locks._release = lambda ident: (_ for _ in ()).throw(DatabaseError("abgebrochen"))
    try:
        with sync.order_lock(order.pk):
            pass
    finally:
        locks._release = original_release
    leaked = not other_try(locks.ORDER, order.pk)
    other_release(locks.ORDER, order.pk)
    check(not leaked, "Entsperren gescheitert: Verbindung geschlossen, die Sperre ist frei")
    # Wie beim Wechsel der Zahlart: pretix ruft cancel_payment in seiner eigenen Transaktion auf.
    order, payment, inv = order_with_payment()
    with _tx.atomic():
        sync.apply(provider, payment, invoice(payment, inv, state.SETTLED), "check")
        during = other_try(locks.ORDER, order.pk)
    after = other_try(locks.ORDER, order.pk)
    other_release(locks.ORDER, order.pk)
    check(not during and after, "in einer Transaktion: gesperrt bis zu ihrem Commit, nicht nur bis zum Ende von apply")

    print("Erstattungen aus, wenn nicht eingeschaltet")
    order, payment, inv = order_with_payment()
    sync.apply(provider, payment, invoice(payment, inv, state.SETTLED), "check")
    payment.refresh_from_db()
    provider.settings.set("refunds", False)
    check(not provider.payment_refund_supported(payment), "aus: pretix bietet keine Erstattung über BTCPay an")
    provider.settings.set("refunds", True)
    check(provider.payment_refund_supported(payment), "an: für eine bestätigte Zahlung")

    print("Erstattung ohne klare Antwort")
    from pretix.base.models import OrderRefund
    from pretix.base.payment import PaymentException
    from pretix_btcpay.api import BTCPayError as _Err
    from django.core.cache import cache as _cache
    original_refund = BTCPayAPI.refund_invoice

    def paid_order():
        order, payment, inv = order_with_payment()
        sync.apply(provider, payment, invoice(payment, inv, state.SETTLED), "check")
        reload(order, payment)
        return order, payment, inv

    def new_refund(order, payment):
        return order.refunds.create(payment=payment, source=OrderRefund.REFUND_SOURCE_ADMIN, state=OrderRefund.REFUND_STATE_CREATED,
                                    amount=Decimal("10.00"), provider=sync.PROVIDER)

    def run_refund(refund):
        try:
            provider.execute_refund(refund)
            refund.refresh_from_db()
            return refund.state, bool(refund.info_data.get("ambiguous")), ""
        except PaymentException as e:
            refund.refresh_from_db()
            return "abgelehnt", bool(refund.info_data.get("ambiguous")), str(e)

    outcomes = {}
    for label, behaviour in (("keine Antwort", _Err("timeout")), ("Serverfehler", _Err("boom", status=502)),
                             ("fremder Link", {"id": "PP1", "viewLink": "https://evil.example/pp"}),
                             ("abgelehnt", _Err("bad", status=400))):
        def fake_refund(self, *a, _b=behaviour, **kw):
            if isinstance(_b, Exception):
                raise _b
            return _b
        BTCPayAPI.refund_invoice = fake_refund
        order, payment, inv = paid_order()
        outcomes[label] = run_refund(new_refund(order, payment))[:2]
    check(all(outcomes[k] == ("transit", True) for k in ("keine Antwort", "Serverfehler", "fremder Link")),
          f"ohne klare Antwort: in Arbeit, für einen Menschen markiert ({outcomes})")
    check(outcomes["abgelehnt"] == ("abgelehnt", False), "von BTCPay abgelehnt: sauber gescheitert, nichts angelegt")

    posts = []
    BTCPayAPI.refund_invoice = lambda self, *a, **kw: posts.append(kw.get("name")) or {"id": f"PP{len(posts)}", "viewLink": str(provider.settings.url) + f"/pp{len(posts)}"}
    # Nach einer unklaren Erstattung geht die nächste derselben Zahlung gar nicht erst an BTCPay.
    BTCPayAPI.refund_invoice, answered = (lambda self, *a, **kw: (_ for _ in ()).throw(_Err("timeout"))), BTCPayAPI.refund_invoice
    order, payment, inv = paid_order()
    first = new_refund(order, payment)
    run_refund(first)
    BTCPayAPI.refund_invoice = answered
    second = run_refund(new_refund(order, payment))
    check(not posts and second[0] == "abgelehnt" and first.info_data["name"] in second[2],
          "nach einer unklaren Erstattung: die nächste scheitert sauber, nennt die erste und fragt BTCPay nicht")
    # Wie in pretix' Erstattungsansicht: BTCPay legt den Claim an, danach rollt die Transaktion zurück.
    order, payment, inv = paid_order()
    try:
        with _tx.atomic():
            run_refund(new_refund(order, payment))
            raise RuntimeError("Worker stirbt vor dem Commit")
    except RuntimeError:
        pass
    retry = run_refund(new_refund(order, payment))
    check(len(posts) == 1 and retry[0] == "abgelehnt" and "PP1" in retry[2],
          "BTCPay hat angelegt, pretix rollt zurück: die Wiederholung legt keinen zweiten Claim an und nennt den ersten")
    # Dieselbe Erstattung noch einmal, auch wenn der Cache leer ist.
    order, payment, inv = paid_order()
    done = new_refund(order, payment)
    run_refund(done)
    _cache.delete(f"pretix_btcpay_refund_{payment.pk}")
    again = run_refund(done)
    done.refresh_from_db()
    check(len(posts) == 2 and again[0] == "abgelehnt" and done.info_data.get("pull_payment_id") == "PP2",
          "eine schon gesendete Erstattung noch einmal ausgeführt: kein zweiter Aufruf, ihr Claim bleibt")
    # Ticket und Überschuss sind zwei Zahlungen: beide lassen sich über BTCPay erstatten.
    order, payment, inv = order_with_payment()
    PAYMENTS[inv] = [("Settled", "266.00"), ("Settled", "133.00")]
    sync.apply(provider, payment, invoice(payment, inv, state.SETTLED, "PaidOver", paidAmount="399.00"), "check")
    reload(order, payment)
    extra = order.payments.filter(state="confirmed").exclude(pk=payment.pk).first()
    both = [run_refund(new_refund(order, p))[0] for p in (extra, payment)]
    check(both == ["transit", "transit"] and len(posts) == 4, f"Überschuss und Ticket erstattet: zwei Claims ({both})")
    BTCPayAPI.refund_invoice = original_refund

    print("Überschuss, dessen Zahlung dazwischen ungültig wurde")
    order, payment, inv = order_with_payment()
    PAYMENTS[inv] = [("Settled", "266.00"), ("Invalid", "133.00")]  # die Rechnung sagte noch 399, die Liste nicht mehr
    sync.apply(provider, payment, invoice(payment, inv, state.SETTLED, "PaidOver", paidAmount="399.00"), "check")
    reload(order, payment)
    check(not order.payments.exclude(pk=payment.pk).exists() and payment.info_data.get("surplus_open")
          and payment.info_data.get("surplus_counted") is None, "nichts gebucht, aber offen: nicht als erledigt vermerkt")
    PAYMENTS[inv] = [("Settled", "266.00"), ("Invalid", "133.00"), ("Settled", "133.00")]
    sync.apply(provider, payment, invoice(payment, inv, state.SETTLED, "PaidOver", paidAmount="399.00"), "check")
    reload(order, payment)
    extra = list(order.payments.filter(state="confirmed").exclude(pk=payment.pk).values_list("amount", flat=True))
    check(extra == [Decimal("133.00")] and not payment.info_data.get("surplus_open"), f"die echte Zahlung danach: gebucht ({extra})")
    order, payment, inv = order_with_payment()
    PAYMENTS[inv] = [("Settled", "266.00"), ("Processing", "133.00")]
    sync.apply(provider, payment, invoice(payment, inv, state.SETTLED, "PaidOver", paidAmount="399.00"), "check")
    OrderPayment.objects.filter(pk=payment.pk).update(created=now() - sync.WINDOW - timedelta(days=30))
    payment.refresh_from_db()
    payment.info_data = {**payment.info_data, "checked": 0}  # fällig, als wäre es länger her
    payment.save(update_fields=["info"])
    open_invoice = inv

    print("Stornieren in pretix' Transaktion")
    asked_cancel = []
    original_get = BTCPayAPI.get_invoice
    BTCPayAPI.get_invoice = lambda self, store_id, invoice_id: asked_cancel.append(invoice_id) or (_ for _ in ()).throw(_Err("aus", status=503))
    order, payment, inv = order_with_payment()
    with _tx.atomic():
        provider.cancel_payment(payment)
    reload(order, payment)
    check(not asked_cancel and payment.state == "canceled", "in einer Transaktion: kein Aufruf bei BTCPay, storniert nach dem letzten bekannten Stand")
    order, payment, inv = order_with_payment()
    provider.cancel_payment(payment)
    BTCPayAPI.get_invoice = original_get
    check(asked_cancel == [inv], "außerhalb: erst bei BTCPay nachgesehen")

    print("Überschuss mit Rundung")
    order, payment, inv = order_with_payment()
    PAYMENTS[inv] = [("Settled", "266.00"), ("Settled", "132.999")]
    sync.apply(provider, payment, invoice(payment, inv, state.SETTLED, "PaidOver", paidAmount="399.00"), "check")
    reload(order, payment)
    extra = list(order.payments.filter(state="confirmed").exclude(pk=payment.pk).values_list("amount", flat=True))
    OrderPayment.objects.filter(pk=payment.pk).update(created=now() - timedelta(days=10))
    payment.refresh_from_db()
    payment.info_data = {**payment.info_data, "monitoring_until": int((now() - timedelta(days=9)).timestamp())}
    payment.save(update_fields=["info"])
    check(extra == [Decimal("132.99")] and sync.next_check(payment, now()) is None,
          f"ein Cent weniger nach dem Abrunden: gebucht ({extra}), der Abgleich fragt nicht ewig nach")

    print("Ein ganzer Cent fehlt")
    order, payment, inv = order_with_payment()
    PAYMENTS[inv] = [("Settled", "266.00"), ("Settled", "133.00")]
    sync.apply(provider, payment, invoice(payment, inv, state.SETTLED, "PaidOver", paidAmount="399.01"), "check")
    reload(order, payment)
    first = payment.info_data.get("surplus_counted")
    PAYMENTS[inv] = [("Settled", "266.00"), ("Settled", "133.00"), ("Settled", "0.01")]
    sync.apply(provider, payment, invoice(payment, inv, state.SETTLED, "PaidOver", paidAmount="399.01"), "check")
    reload(order, payment)
    extra = sorted(order.payments.filter(state="confirmed").exclude(pk=payment.pk).values_list("amount", flat=True))
    check(first is None and extra == [Decimal("0.01"), Decimal("133.00")],
          f"BTCPay zählt einen Cent mehr, als bestätigt ist: offen gelassen, der Cent danach gebucht ({extra})")

    print("Nach der Erstattung noch mehr Geld")
    order, payment, inv = order_with_payment()
    payment.state = OrderPayment.PAYMENT_STATE_REFUNDED
    payment.save(update_fields=["state"])
    sync.apply(provider, payment, invoice(payment, inv, state.SETTLED), "check")
    sync.apply(provider, payment, invoice(payment, inv, state.SETTLED), "check")
    sync.apply(provider, payment, invoice(payment, inv, state.SETTLED, paidAmount="300.00"), "check")
    notes = order.all_logentries().filter(action_type="pretix_btcpay.settled_after_refund").count()
    check(notes == 2, f"jede neue Summe wird noch einmal vermerkt, dieselbe nicht ({notes})")

    print("Alte, nicht erledigte Zahlung")
    order, payment, inv = order_with_payment()
    OrderPayment.objects.filter(pk=payment.pk).update(created=now() - sync.WINDOW - timedelta(days=30))
    payment.refresh_from_db()
    payment.info_data = {**payment.info_data, "status": state.SETTLED, "checked": 0}
    payment.save(update_fields=["info"])
    old_invoice = inv

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
    check(old_invoice in seen_first | seen_second, "eine Settled-Rechnung, die pretix nicht gezählt hat, fällt nie aus dem Abgleich, auch nach einem halben Jahr")
    check(open_invoice in seen_first | seen_second, "ein noch nicht gebuchter Überschuss auch nicht")

    print("Besetzte Bestellung im Abgleich")
    order, payment, inv = order_with_payment()
    payment.info_data = {**payment.info_data, "status": state.NEW, "checked": 0}
    payment.save(update_fields=["info"])
    asked_busy = []
    original_get = BTCPayAPI.get_invoice
    BTCPayAPI.get_invoice = lambda self, store_id, invoice_id: asked_busy.append(invoice_id) or (_ for _ in ()).throw(_Err("weg", status=404))
    other_try(locks.ORDER, order.pk)
    try:
        stats = sync.poll(budget=1000)
    finally:
        other_release(locks.ORDER, order.pk)
        BTCPayAPI.get_invoice = original_get
    check(inv not in asked_busy and stats["busy"] >= 1, f"besetzt: übersprungen, ohne BTCPay zu fragen ({stats['busy']} besetzt)")

    for pk in OrderPayment.objects.filter(order__pk__in=created).values_list("pk", flat=True):
        _cache.delete(f"pretix_btcpay_refund_{pk}")
    other.close()
    from django.db import transaction
    with transaction.atomic():
        Order.gracefully_delete_bulk(event, Order.objects.filter(pk__in=created, testmode=True))

    del provider.settings["refunds"]

print(f"{'ALLE BESTANDEN' if all(results) else 'NICHT BESTANDEN'}: {sum(results)} von {len(results)}")
