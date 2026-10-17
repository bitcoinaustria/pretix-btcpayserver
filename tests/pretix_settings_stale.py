"""
A settings form loaded before another request changed the BTCPay server or store, submitted afterwards: it looks
unchanged to itself and would write the old connection back, cutting a new invoice off from reconciliation. In an
isolated test-mode event with mocked BTCPay calls; operator settings stay untouched:

    docker exec -i <pretix container> pretix shell < tests/pretix_settings_stale.py
    docker exec -i -e REVIEW_MOVE_SERVER=1 <pretix container> pretix shell < tests/pretix_settings_stale.py

Fails with an AssertionError if the stale form is accepted. Found in review.
"""
import os
import threading
import time
import uuid
from collections import OrderedDict
from datetime import timedelta
from decimal import Decimal
from unittest.mock import Mock, patch

from django.db import connections, transaction
from django.utils.timezone import now
from django_scopes import scope, scopes_disabled
from pretix.base.models import Event, Order, OrderFee
from pretix.control.forms.event import ProviderForm

from pretix_btcpay import sync
from pretix_btcpay.api import BTCPayAPI, BTCPayError
from pretix_btcpay.checks import REQUIRED_PERMISSIONS
from pretix_btcpay.payment import BTCPayServer


def client_for(server, store):
    client = Mock(spec=BTCPayAPI)
    client.url = server
    client.current_key.return_value = {"permissions": [p + ":" + store for p in REQUIRED_PERMISSIONS]}
    client.get_webhook.side_effect = BTCPayError("not found", status=404)
    client.list_webhooks.return_value = []
    client.create_webhook.return_value = {"id": "WEBHOOK-" + store}
    client.create_invoice.return_value = {
        "id": "NEW-STORE-INVOICE", "status": "New", "checkoutLink": server + "/i/NEW-STORE-INVOICE",
        "expirationTime": int((now() + timedelta(minutes=15)).timestamp()),
    }
    return client


def form_for(provider, server, store):
    prefix = provider.settings.get_prefix()
    data = {"url": server, "store_id": store, "api_key": "test-key", "_enabled": "on",
            "speed_policy": "MediumSpeed", "_restrict_to_sales_channels": ["web"]}
    form = ProviderForm(obj=provider.event, settingspref=prefix, provider=provider,
                        data={prefix + k: v for k, v in data.items()})
    form.fields = OrderedDict((prefix + k, v) for k, v in provider.settings_form_fields.items())
    form.prepare_fields()
    return form


with scopes_disabled():
    main = Event.objects.get(organizer__slug="zitadelle", slug="2027")
assert main.testmode
with scope(organizer=main.organizer):
    event = Event.objects.create(organizer=main.organizer, slug="review-a-stale-" + uuid.uuid4().hex[:12],
                                name={"en": "Reviewer A cached settings"}, date_from=now(),
                                currency=main.currency, testmode=True, plugins=["pretix_btcpay"])
    ready, release, outcome, errors = threading.Event(), threading.Event(), [], []
    worker = None
    try:
        event.settings.set("locales", ["en"])
        initial = BTCPayServer(event)
        server = "https://pay.example.org"
        for key, value in dict(url=server, store_id="OLD-STORE", api_key="test-key", webhook_id="OLD-WEBHOOK",
                               webhook_secret="fixture-secret", pending_webhook="", _enabled=True).items():
            initial.settings.set(key, value)
        # pretix creates its hidden-payment seed on the first settings-form load. A real configured event has one.
        initial.settings_form_fields

        # Request A caches the old connection inside the same atomic transaction that later validates and saves.
        def stale_request():
            try:
                with scope(organizer=main.organizer), transaction.atomic():
                    stale_provider = BTCPayServer(Event.objects.get(pk=event.pk))
                    stale_form = form_for(stale_provider, server, "OLD-STORE")
                    assert sync.invoice_origin(stale_provider)["store_id"] == "OLD-STORE"
                    print("A: cached old form inside transaction", time.monotonic(), flush=True)
                    ready.set()
                    assert release.wait(45), "Other request never completed"
                    with patch("pretix_btcpay.payment.BTCPayAPI", return_value=client_for(server, "OLD-STORE")):
                        valid = stale_form.is_valid()
                        print("Cached unchanged form accepted:", valid, str(stale_form.errors))
                        if valid:
                            stale_form.save()
                        outcome.append(valid)
            except Exception as error:
                errors.append(error)
            finally:
                connections.close_all()
        worker = threading.Thread(target=stale_request, daemon=True)
        worker.start()
        assert ready.wait(8), errors
        print("B: start connection change", time.monotonic(), flush=True)

        # Request B switches stores and commits through the real settings form.
        new_provider = BTCPayServer(Event.objects.get(pk=event.pk))
        switching_server = os.environ.get("REVIEW_MOVE_SERVER") == "1"
        new_server = "https://new.example.org" if switching_server else server
        new_store = "OLD-STORE" if switching_server else "NEW-STORE"
        new_client = client_for(new_server, new_store)
        with transaction.atomic(), patch("pretix_btcpay.payment.BTCPayAPI", return_value=new_client):
            form = form_for(new_provider, new_server, new_store)
            assert form.is_valid(), form.errors
            print("B: form validated", time.monotonic(), flush=True)
            form.save()
        print("B: settings committed", time.monotonic(), flush=True)

        # A new checkout receives an invoice in that newly saved store.
        fresh_event = Event.objects.get(pk=event.pk)
        checkout = BTCPayServer(fresh_event)
        checkout.__dict__["client"] = new_client
        with transaction.atomic():
            order = Order.objects.create(event=fresh_event, status="n", testmode=True, total=Decimal("1.00"),
                                         expires=now() + timedelta(minutes=30), locale="en", email=None,
                                         sales_channel=main.organizer.sales_channels.get(identifier="web"))
            order.fees.create(fee_type=OrderFee.FEE_TYPE_OTHER, value=Decimal("1.00"), description="Review fixture")
            order.create_transactions()
            payment = order.payments.create(provider=sync.PROVIDER, amount=order.total, state="created", info="{}")
        checkout.execute_payment(None, payment)
        print("Checkout: invoice committed", time.monotonic(), flush=True)
        payment.refresh_from_db()
        assert payment.info_data["store_id"] == new_store
        assert payment.info_data["server"] == new_server

        # Request A resumes with unchanged fields, so every connection guard sees OLD -> OLD.
        release.set()
        worker.join(8)
        assert not worker.is_alive() and not errors, errors
        assert len(outcome) == 1, outcome
        final = BTCPayServer(Event.objects.get(pk=event.pk))
        print("Invoice origin:", payment.info_data["server"], payment.info_data["store_id"],
              "; saved origin:", sync.invoice_origin(final))
        print("Invoice excluded from reconciliation:", sync.created_elsewhere(final, payment))
        assert not outcome[0], "A stale unchanged settings request must not replace the connection of an open invoice"
        assert not sync.created_elsewhere(final, payment)
        # Still reconciled, with the store it was created in.
        reading_client = Mock(spec=BTCPayAPI)
        reading_client.get_invoice.side_effect = RuntimeError("asked")
        final.__dict__["client"] = reading_client
        try:
            sync.sync_payment(final, payment, "stale-settings")
        except RuntimeError:
            pass
        assert reading_client.get_invoice.call_args.args[0] == new_store, reading_client.get_invoice.call_args
        print("OK: stale form refused, invoice still reconciled with", new_server, new_store)
    finally:
        release.set()
        if worker:
            worker.join(8)
            assert not worker.is_alive(), "Worker must finish before fixture cleanup"
        with transaction.atomic():
            Order.gracefully_delete_bulk(event, Order.objects.filter(event=event, testmode=True))
            event.delete()
        print("CLEANUP: isolated event and orders deleted; no real settings or BTCPay calls changed.")
