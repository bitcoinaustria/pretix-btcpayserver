"""Database regressions; run in pretix shell with CHECK_EVENT=organizer/test-event.

Requires PostgreSQL and a test-mode event with the plugin installed. Invoices are fake, mail and invoice generation
are disabled, and temporary orders are deleted in cleanup. Payment settings are never changed. Fixtures commit so
independent connections can exercise the row locks used by real payment-method changes.
"""
import hashlib
import hmac
import json
import os
import threading
import unittest
from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, PropertyMock, patch

from django.core.exceptions import ValidationError
from django.db import DatabaseError, connection, connections, transaction
from django.test import RequestFactory
from django.utils.timezone import now
from django.utils.translation import override
from django_scopes import scope, scopes_disabled
from pretix.base.models import Event, Order, OrderFee, OrderPayment, OrderRefund
from pretix.base.services.orders import change_payment_provider

from pretix_btcpay import state, sync, views
from pretix_btcpay.api import BTCPayError
from pretix_btcpay.checks import REQUIRED_PERMISSIONS
from pretix_btcpay.payment import BTCPayServer

ORGANIZER, EVENT = os.environ.get("CHECK_EVENT", "zitadelle/2027").split("/")
with scopes_disabled():
    EVENT_OBJECT = Event.objects.get(organizer__slug=ORGANIZER, slug=EVENT)
if not EVENT_OBJECT.testmode or connection.vendor != "postgresql":
    raise RuntimeError("These checks require a test-mode event and PostgreSQL.")


class PaymentRegressions(unittest.TestCase):
    def setUp(self):
        self.enterContext(scope(organizer=EVENT_OBJECT.organizer))
        self.enterContext(override("en"))
        self.enterContext(patch("pretix.base.services.invoices.invoice_qualified", return_value=False))
        self.enterContext(patch.object(Order, "send_mail", return_value=None))
        self.enterContext(patch.object(OrderPayment, "_send_paid_mail", return_value=None))
        self.orders = []
        self.addCleanup(self.delete_orders)
        self.provider = SimpleNamespace(
            configured=True, settings=SimpleNamespace(store_id="REVIEW"),
            client=SimpleNamespace(get_invoice_payment_methods=lambda *args: [
                {"rate": "1", "payments": [{"status": "Settled", "value": "399.00", "fee": "0"}]}
            ]),
        )

    def delete_orders(self):
        with transaction.atomic():
            Order.gracefully_delete_bulk(EVENT_OBJECT, Order.objects.filter(pk__in=self.orders, testmode=True))

    def make_order(self, total="266.00", payment_state="created", order_state=Order.STATUS_PENDING):
        with transaction.atomic():
            order = Order.objects.create(
                event=EVENT_OBJECT, status=order_state, expires=now() + timedelta(minutes=30),
                total=Decimal(total), testmode=True, email=None, locale="en",
                sales_channel=EVENT_OBJECT.organizer.sales_channels.get(identifier="web"),
            )
            self.orders.append(order.pk)
            order.fees.create(fee_type=OrderFee.FEE_TYPE_OTHER, value=Decimal(total), description="Regression fixture")
            order.create_transactions()
            payment = self.add_payment(order, payment_state)
        return order, payment

    def add_payment(self, order, payment_state="canceled"):
        payment = order.payments.create(provider=sync.PROVIDER, amount=Decimal("266.00"), state=payment_state)
        invoice_id = f"REGRESSION{payment.pk}"
        payment.info_data = {
            "invoice_id": invoice_id, "expires": int((now() + timedelta(minutes=15)).timestamp()),
            "checkout_link": f"https://example.org/i/{invoice_id}",
        }
        payment.save(update_fields=["info"])
        return payment

    def invoice(self, payment, status, paid="266.00", monitoring_hours=24):
        return {
            "id": payment.info_data["invoice_id"], "storeId": "REVIEW", "amount": str(payment.amount),
            "currency": EVENT_OBJECT.currency, "status": status, "additionalStatus": "None", "paidAmount": paid,
            "checkout": {"speedPolicy": "MediumSpeed"},
            "monitoringExpiration": int((now() + timedelta(hours=monitoring_hours)).timestamp()),
            "metadata": {"pretixPaymentId": payment.pk, "orderId": payment.order.code,
                         "pretixEvent": sync.event_ref(EVENT_OBJECT)},
        }

    def other_connection(self):
        other = connections.create_connection("default")
        self.addCleanup(other.close)
        with other.cursor() as cursor:
            cursor.execute("SET statement_timeout = '5s'")
        return other

    def check_store_switch(self, payment, blocked):
        provider = BTCPayServer(EVENT_OBJECT)
        settings = SimpleNamespace(url="https://example.org", store_id="REVIEW", api_key="test-key")
        client = Mock()
        client.current_key.return_value = {"permissions": [p + ":OTHER" for p in REQUIRED_PERMISSIONS]}
        data = {f"payment_{sync.PROVIDER}_{k}": v for k, v in
                {"url": settings.url, "store_id": "OTHER", "api_key": settings.api_key}.items()}
        # Only this test's order participates; unrelated invoices on the shared test event must not mask a failure.
        payments = OrderPayment.objects.filter(pk=payment.pk)
        with patch.object(provider, "settings", settings), patch.object(provider, "ensure_webhook") as ensure, \
                patch("pretix_btcpay.payment.OrderPayment.objects.filter", return_value=payments), \
                patch("pretix_btcpay.payment.BTCPayAPI", return_value=client):
            if blocked:
                with self.assertRaisesRegex(ValidationError, "invoices of this event"):
                    provider.settings_form_clean(data)
                client.current_key.assert_not_called()
                ensure.assert_not_called()
            else:
                self.assertEqual(provider.settings_form_clean(data), data)
                ensure.assert_called_once()

    def test_store_switch_waits_until_settled_surplus_is_booked(self):
        for interrupted in ("receipts_unavailable", "before_marker", "unconfirmed_extra"):
            with self.subTest(interrupted=interrupted):
                order, payment = self.make_order()
                invoice = self.invoice(payment, state.SETTLED, "399.00")
                if interrupted == "before_marker":
                    with patch.object(sync, "_record_surplus", side_effect=RuntimeError("Worker died")):
                        with self.assertRaisesRegex(RuntimeError, "Worker died"):
                            sync.apply(self.provider, payment, invoice, "regression")
                else:
                    receipts = (Mock(side_effect=BTCPayError("outage")) if interrupted == "receipts_unavailable"
                                else Mock(return_value=[{"rate": "1", "payments": [
                                    {"status": "Settled", "value": "266.00"},
                                    {"status": "Processing", "value": "133.00"},
                                ]}]))
                    with patch.object(self.provider.client, "get_invoice_payment_methods", receipts):
                        sync.apply(self.provider, payment, invoice, "regression")
                payment.refresh_from_db()
                order.refresh_from_db()
                self.assertEqual((payment.state, order.status), ("confirmed", Order.STATUS_PAID))
                self.assertEqual(sync._net_paid(order), Decimal("266.00"))
                self.check_store_switch(payment, blocked=True)
                sync.apply(self.provider, payment, invoice, "regression")
                payment.refresh_from_db()
                self.assertEqual(sync._net_paid(order), Decimal("399.00"))
                self.check_store_switch(payment, blocked=False)

    def test_store_switch_waits_until_confirmed_payment_finishes_its_order(self):
        order, payment = self.make_order()
        invoice = self.invoice(payment, state.SETTLED)
        # pretix's backend confirms outside our row-lock transaction; its second transaction can fail afterwards.
        with patch.object(OrderPayment, "_mark_order_paid", side_effect=RuntimeError("Worker died")):
            with self.assertRaisesRegex(RuntimeError, "Worker died"):
                payment.confirm()
        with patch.object(sync, "_catch_up_order", side_effect=sync.OrderBusy):
            with self.assertRaises(sync.OrderBusy):
                sync.apply(self.provider, payment, invoice, "regression")
        payment.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual((payment.state, order.status), ("confirmed", Order.STATUS_PENDING))
        self.assertEqual(sync._net_paid(order), order.total)
        self.check_store_switch(payment, blocked=True)
        sync.apply(self.provider, payment, invoice, "regression")
        payment.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(order.status, Order.STATUS_PAID)
        self.check_store_switch(payment, blocked=False)

    def test_invoice_origin_guard_skips_other_stores_and_accepts_the_same_server(self):
        order, payment = self.make_order()
        self.provider.settings.url = "https://example.org"
        payment.info_data = {**payment.info_data, **sync.invoice_origin(self.provider)}
        payment.save(update_fields=["info"])
        self.provider.client.get_invoice = Mock(return_value=self.invoice(payment, state.NEW, "0"))
        for server, store in (("https://example.org", "OTHER"), ("https://other.example.org", "REVIEW")):
            self.provider.settings.url, self.provider.settings.store_id = server, store
            for _ in range(2):
                self.assertIsNone(sync.sync_payment(self.provider, payment, "regression"))
        self.provider.client.get_invoice.assert_not_called()
        payment.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual((payment.state, order.status), ("created", Order.STATUS_PENDING))
        self.assertTrue(payment.info_data.get("checked"))
        self.assertEqual(order.all_logentries().filter(action_type="pretix_btcpay.other_store").count(), 1)
        self.provider.settings.url, self.provider.settings.store_id = "HTTPS://EXAMPLE.ORG:443/", "REVIEW"
        sync.sync_payment(self.provider, payment, "regression")
        self.provider.client.get_invoice.assert_called_once_with("REVIEW", payment.info_data["invoice_id"])

    def test_api_reuses_connections_within_a_thread_without_sharing_sessions(self):
        from pretix_btcpay import api
        sessions, errors = [], []

        def make_session():
            session = Mock()
            session.request.return_value = SimpleNamespace(status_code=200, content=b"{}", json=lambda: {})
            sessions.append(session)
            return session

        def request_twice():
            try:
                for _ in range(2):
                    self.assertEqual(api.BTCPayAPI("https://example.org", "test-key").get_invoice("STORE", "INVOICE"), {})
            except Exception as error:
                errors.append(error)

        with patch.object(api, "_sessions", threading.local(), create=True), \
                patch.object(api.requests, "Session", side_effect=make_session), \
                patch.object(api.requests, "request", side_effect=AssertionError("Connection reuse bypassed")):
            request_twice()
            workers = [threading.Thread(target=request_twice) for _ in range(2)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=2)
            self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertEqual(errors, [])
        self.assertEqual(len(sessions), 3)
        self.assertTrue(all(session.request.call_count == 2 for session in sessions))

    def test_api_does_not_follow_redirects_and_preserves_error_status(self):
        from pretix_btcpay import api
        session = Mock()
        client = api.BTCPayAPI("https://example.org/", "test-key")
        with patch.object(api, "_session", return_value=session):
            session.request.return_value = SimpleNamespace(status_code=200, content=b"{}", json=lambda: {})
            self.assertEqual(client.get_invoice("STORE", "INVOICE"), {})
            self.assertEqual(session.request.call_args.kwargs["timeout"], (5, 15))
            self.assertIs(session.request.call_args.kwargs["allow_redirects"], False)
            for status in (302, 404, 503):
                with self.subTest(status=status):
                    session.request.return_value = SimpleNamespace(status_code=status, text="test response")
                    with self.assertRaises(api.BTCPayError) as raised:
                        client.get_invoice("STORE", "INVOICE")
                    self.assertEqual(raised.exception.status, status)

    def webhook_request(self, payment, **meta):
        secret = "regression-webhook-secret"
        body = json.dumps({"type": "InvoiceSettled", "invoiceId": payment.info_data["invoice_id"],
                           "storeId": "REVIEW"}).encode()
        request = RequestFactory().post("/webhook/", data=body, content_type="application/json",
                                       HTTP_BTCPAY_SIG="sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest())
        request.event = EVENT_OBJECT
        request.META.update(meta)
        self.provider.webhook_secrets = lambda: [secret]
        return request

    def test_unconfirmed_money_holds_until_monitoring_plus_one_hour_without_paying(self):
        for initial in ("created", "pending", "canceled", "failed"):
            with self.subTest(initial=initial):
                order, payment = self.make_order(payment_state=initial)
                invoice = self.invoice(payment, state.PROCESSING, monitoring_hours=3)
                sync.apply(self.provider, payment, invoice, "regression")
                order.refresh_from_db()
                payment.refresh_from_db()
                self.assertEqual(order.status, Order.STATUS_PENDING)
                self.assertEqual(payment.state, "pending" if initial == "created" else initial)
                self.assertAlmostEqual(order.expires.timestamp(), invoice["monitoringExpiration"] + 3600, delta=1)
                self.assertEqual(sync._net_paid(order), Decimal("0"))
                # pretix can explicitly permit unpaid tickets; exercise the required paid-only configuration.
                with patch.object(Event, "settings", new_callable=PropertyMock, return_value=SimpleNamespace(
                        ticket_download=True, ticket_download_date=None, ticket_download_pending=False)):
                    self.assertFalse(order.ticket_download_available)
                self.assertFalse(order.all_logentries().filter(action_type="pretix.event.order.payment.confirmed").exists())

    def test_hold_cannot_end_four_minutes_before_monitoring_plus_one_hour(self):
        order, first = self.make_order(payment_state="canceled")
        earlier = self.invoice(first, state.PROCESSING, monitoring_hours=3)
        sync.apply(self.provider, first, earlier, "regression")
        payment = self.add_payment(order, "created")
        invoice = self.invoice(payment, state.PROCESSING, monitoring_hours=3)
        invoice["monitoringExpiration"] = earlier["monitoringExpiration"] + 4 * 60
        required = sync._timestamp(invoice["monitoringExpiration"]) + timedelta(hours=1)
        # The earlier invoice's hold must not truncate the later invoice's full recovery margin.
        sync.apply(self.provider, payment, invoice, "regression")
        order.refresh_from_db()
        payment.refresh_from_db()
        self.assertEqual((order.status, payment.state), (Order.STATUS_PENDING, "pending"))
        self.assertGreaterEqual(order.expires, required)

    def test_unsafe_or_malformed_speed_policy_cannot_change_payment_or_hold(self):
        for checkout in (None, [], "MediumSpeed", 1, {}, {"speedPolicy": "HighSpeed"},
                         {"speedPolicy": "Unknown"}, {"speedPolicy": None}):
            with self.subTest(checkout=checkout):
                order, payment = self.make_order()
                expires = order.expires
                invoice = self.invoice(payment, state.SETTLED)
                invoice["checkout"] = checkout
                with self.assertRaises(sync.InvoiceMismatch):
                    sync.apply(self.provider, payment, invoice, "regression")
                order.refresh_from_db()
                payment.refresh_from_db()
                self.assertEqual((order.status, payment.state, order.expires), (Order.STATUS_PENDING, "created", expires))
                self.assertNotIn("status", payment.info_data)

    def test_double_payment_note_rolls_back_with_confirmation_and_retries_once(self):
        order, first = self.make_order(payment_state="confirmed", order_state=Order.STATUS_PAID)
        second = self.add_payment(order, "created")
        original_note = sync._note

        def crash_after_note(payment, note, invoice, **kwargs):
            original_note(payment, note, invoice, **kwargs)
            if note == state.NOTE_PAID_TWICE:
                raise RuntimeError("Worker dies before commit")

        with patch.object(sync, "_note", crash_after_note):
            with self.assertRaisesRegex(RuntimeError, "before commit"):
                sync.apply(self.provider, second, self.invoice(second, state.SETTLED), "regression")
        second.refresh_from_db()
        self.assertEqual(second.state, "created")
        self.assertFalse(order.all_logentries().filter(action_type="pretix_btcpay.double_payment").exists())
        for _ in range(2):
            sync.apply(self.provider, second, self.invoice(second, state.SETTLED), "regression")
        second.refresh_from_db()
        self.assertEqual(second.state, "confirmed")
        self.assertEqual(sync._net_paid(order), Decimal("532.00"))
        self.assertEqual(order.all_logentries().filter(action_type="pretix_btcpay.double_payment").count(), 1)
        self.assertEqual(order.all_logentries().filter(action_type="pretix.event.order.payment.confirmed").count(), 1)

    def test_webhook_rejects_bad_length_before_reading_or_fetching(self):
        order, payment = self.make_order()
        for length, expected in (("garbage", 400), (str(views.MAX_BODY + 1), 413)):
            with self.subTest(length=length):
                request = self.webhook_request(payment, CONTENT_LENGTH=length)
                request._stream = Mock(read=Mock(side_effect=AssertionError("Body must not be read")))
                self.provider.client.get_invoice = Mock(side_effect=AssertionError("Must not contact BTCPay"))
                with patch.object(views, "_provider", return_value=self.provider):
                    self.assertEqual(views.webhook(request).status_code, expected)
        request = self.webhook_request(payment, CONTENT_LENGTH="0")
        request._body = b"x" * (views.MAX_BODY + 1)
        with patch.object(views, "_provider", return_value=self.provider):
            self.assertEqual(views.webhook(request).status_code, 413)

    def test_status_exposes_reopened_invoice_without_a_btcpay_request(self):
        order, payment = self.make_order(payment_state="pending")
        api_request = self.enterContext(patch("pretix_btcpay.api.BTCPayAPI._request",
                                             side_effect=AssertionError("Status must only read the database")))
        request = RequestFactory().get("/status/")
        request.event = EVENT_OBJECT
        kwargs = {"order": order.code, "secret": order.secret, "payment": payment.pk}
        for status in (state.PROCESSING, state.NEW):
            payment.info_data = {**payment.info_data, "status": status}
            payment.save(update_fields=["info"])
            response = views.status(request, **kwargs)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(json.loads(response.content), {"state": "pending", "paid": False, "invoice_status": status})
            self.assertEqual(response["Cache-Control"], "no-store")
        api_request.assert_not_called()
        self.assertEqual(views.status(request, **{**kwargs, "secret": "wrong"}).status_code, 404)
        other, other_payment = self.make_order()
        self.assertEqual(views.status(request, **{**kwargs, "payment": other_payment.pk}).status_code, 404)

    def test_rbf_underpayment_returns_existing_checkout_and_offers_completion(self):
        order, payment = self.make_order(payment_state="pending")
        invoice = self.invoice(payment, state.NEW, paid="133.00")
        provider = BTCPayServer(EVENT_OBJECT)
        provider.client = SimpleNamespace(url="https://example.org", get_invoice=Mock(return_value=invoice),
                                          create_invoice=Mock(side_effect=AssertionError("Must reuse the invoice")))
        request = RequestFactory().get("/")
        with patch.object(provider, "settings", SimpleNamespace(store_id="REVIEW", webhook_id="W", webhook_secret="S")), \
                patch.object(provider, "promote_webhook"), patch.object(provider, "_order_url", return_value="/order/"):
            self.assertEqual(provider.execute_payment(request, payment), payment.info_data["checkout_link"])
            rendered = provider.payment_pending_render(request, payment)
        self.assertIn('href="' + payment.info_data["checkout_link"] + '"', rendered)
        self.assertIn("Complete payment", rendered)
        self.assertNotIn("waiting for confirmation by the Bitcoin network", rendered)
        payment.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual((payment.state, order.status), ("pending", Order.STATUS_PENDING))
        provider.client.create_invoice.assert_not_called()

    def test_confirmed_surplus_format_change_does_not_restart_receipt_lookup(self):
        order, payment = self.make_order(payment_state="confirmed", order_state=Order.STATUS_PAID)
        OrderPayment.objects.filter(pk=payment.pk).update(created=now() - timedelta(days=10))
        payment.refresh_from_db()
        payment.info_data = {**payment.info_data, "status": state.SETTLED, "paid_amount": "399.00",
                             "surplus_counted": "399.0", "surplus_recorded": "132.99", "checked": 1000,
                             "monitoring_until": int((now() - timedelta(days=9)).timestamp())}
        payment.save(update_fields=["info"])
        self.provider.client.get_invoice_payment_methods = Mock(side_effect=AssertionError("Already counted this amount"))
        self.assertIsNone(sync.next_check(payment, now()))
        sync._record_surplus(self.provider, payment, self.invoice(payment, state.SETTLED, "399.00"))
        self.provider.client.get_invoice_payment_methods.assert_not_called()
        self.assertEqual(order.payments.count(), 1)

    def test_control_cache_is_scoped_and_does_not_cache_errors(self):
        order, payment = self.make_order()
        provider = BTCPayServer(EVENT_OBJECT)
        provider.client = SimpleNamespace(url="https://example.org", get_invoice=Mock(return_value={"status": state.NEW}))
        settings = SimpleNamespace(store_id="REVIEW", url="https://example.org", api_key="test-key")
        cached = {}
        calls = []

        def remember(key, value, timeout):
            self.assertEqual(timeout, 30)
            cached[key] = value

        with patch.object(provider, "settings", settings), patch("pretix_btcpay.payment.cache.get", side_effect=cached.get), \
                patch("pretix_btcpay.payment.cache.set", side_effect=remember):
            for store, host in (("REVIEW", "https://example.org"), ("OTHER", "https://example.org"),
                                ("OTHER", "https://second.example.org")):
                settings.store_id, provider.client.url = store, host
                for _ in range(2):
                    provider.payment_control_render(None, payment)
                calls.append(provider.client.get_invoice.call_count)
            self.assertEqual(calls, [1, 2, 3])
            with patch.object(provider, "event", SimpleNamespace(pk=EVENT_OBJECT.pk + 100000)):
                provider.payment_control_render(None, payment)
            self.assertEqual(provider.client.get_invoice.call_count, 4)
            cached.clear()
            from pretix_btcpay.api import BTCPayError
            provider.client.get_invoice.side_effect = BTCPayError("outage")
            self.assertIn("could not be reached", provider.payment_control_render(None, payment))
            self.assertFalse(cached)
            provider.client.get_invoice.side_effect = None
            provider.payment_control_render(None, payment)
            self.assertEqual(provider.client.get_invoice.call_count, 6)

    def test_order_row_contention_returns_retryable_webhook_response(self):
        order, payment = self.make_order()
        other = self.other_connection()
        other.set_autocommit(False)
        with other.cursor() as cursor:
            cursor.execute("SELECT id FROM pretixbase_order WHERE id = %s FOR UPDATE", [order.pk])
        secret = "regression-webhook-secret"
        body = json.dumps({"type": "InvoiceSettled", "invoiceId": payment.info_data["invoice_id"], "storeId": "REVIEW"}).encode()
        request = RequestFactory().post("/webhook/", data=body, content_type="application/json",
                                        HTTP_BTCPAY_SIG="sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest())
        request.event = EVENT_OBJECT
        self.provider.webhook_secrets = lambda: [secret]
        self.provider.client.get_invoice = lambda *args: self.invoice(payment, state.SETTLED)
        with patch.object(views, "_provider", return_value=self.provider):
            response = views.webhook(request)
        self.assertEqual(response.status_code, 503)
        payment.refresh_from_db()
        self.assertEqual(payment.state, "created")
        other.rollback()
        with patch.object(views, "_provider", return_value=self.provider):
            self.assertEqual(views.webhook(request).status_code, 200)
            self.assertEqual(views.webhook(request).status_code, 200)
        order.refresh_from_db()
        payment.refresh_from_db()
        self.assertEqual((order.status, payment.state), (Order.STATUS_PAID, "confirmed"))
        self.assertEqual(order.all_logentries().filter(action_type="pretix.event.order.payment.confirmed").count(), 1)

    def test_payment_row_contention_releases_order_lock_on_every_finalization_path(self):
        for path in ("confirm", "catch_up", "surplus"):
            with self.subTest(path=path):
                order, payment = self.make_order(
                    payment_state="created" if path == "confirm" else "confirmed",
                    order_state=Order.STATUS_PAID if path == "surplus" else Order.STATUS_PENDING,
                )
                sibling = self.add_payment(order)
                other = self.other_connection()
                other.set_autocommit(False)
                try:
                    with other.cursor() as cursor:
                        cursor.execute("SELECT id FROM pretixbase_orderpayment WHERE id = %s FOR UPDATE", [sibling.pk])
                    with self.assertRaises(sync.OrderBusy):
                        sync.apply(self.provider, payment, self.invoice(payment, state.SETTLED,
                                   "399.00" if path == "surplus" else "266.00"), "regression")
                    # A failed payment-row acquisition must roll back the order lock as well.
                    with other.cursor() as cursor:
                        cursor.execute("SELECT id FROM pretixbase_order WHERE id = %s FOR UPDATE NOWAIT", [order.pk])
                    payment.refresh_from_db()
                    self.assertEqual(payment.state, "created" if path == "confirm" else "confirmed")
                    self.assertEqual(order.payments.count(), 2)
                finally:
                    other.rollback()

    def test_balance_is_rechecked_after_a_concurrent_upgrade(self):
        order, payment = self.make_order(payment_state="confirmed")
        other = self.other_connection()
        original = sync._order_rows

        @contextmanager
        def upgrade_before_lock(order_id):
            # The cheap first balance check passed; another transaction commits an upgrade before we get the row.
            with other.cursor() as cursor:
                cursor.execute("UPDATE pretixbase_order SET total = 399 WHERE id = %s", [order_id])
            with original(order_id) as rows:
                yield rows

        with patch.object(sync, "_order_rows", upgrade_before_lock):
            sync.apply(self.provider, payment, self.invoice(payment, state.SETTLED), "regression")
        order.refresh_from_db()
        self.assertEqual(order.status, Order.STATUS_PENDING)
        self.assertEqual(order.total, Decimal("399.00"))
        self.assertEqual(sync._net_paid(order), Decimal("266.00"))

    def test_order_row_stays_locked_until_finalization(self):
        for path in ("confirm", "catch_up", "surplus"):
            with self.subTest(path=path):
                order, payment = self.make_order(
                    total="399.00" if path == "surplus" else "266.00",
                    payment_state="confirmed" if path == "catch_up" else "created",
                )
                other = self.other_connection()
                original = OrderPayment._mark_order_paid
                attempts = []

                def check_lock(instance, *args, **kwargs):
                    with self.assertRaises(DatabaseError) as raised:
                        with other.cursor() as cursor:
                            cursor.execute("SELECT id FROM pretixbase_order WHERE id = %s FOR UPDATE NOWAIT", [instance.order_id])
                    cause = raised.exception.__cause__
                    self.assertEqual(getattr(cause, "sqlstate", None) or getattr(cause, "pgcode", None), "55P03")
                    attempts.append(True)
                    return original(instance, *args, **kwargs)

                with patch.object(OrderPayment, "_mark_order_paid", check_lock):
                    sync.apply(self.provider, payment, self.invoice(payment, state.SETTLED,
                               "399.00" if path == "surplus" else "266.00"), "regression")
                order.refresh_from_db()
                self.assertEqual(attempts, [True])
                self.assertEqual(order.status, Order.STATUS_PAID)
                self.assertEqual(sync._net_paid(order), order.total)

    def test_quota_failure_keeps_base_and_surplus_confirmed(self):
        for with_surplus in (False, True):
            with self.subTest(surplus=with_surplus):
                order, payment = self.make_order(total="399.00" if with_surplus else "266.00")
                with patch.object(Order, "_can_be_paid", return_value="Regression: no seats"):
                    sync.apply(self.provider, payment, self.invoice(payment, state.SETTLED,
                               "399.00" if with_surplus else "266.00"), "regression")
                order.refresh_from_db()
                payment.refresh_from_db()
                self.assertEqual(order.status, Order.STATUS_PENDING)
                self.assertEqual(sync._net_paid(order), order.total)
                self.assertTrue(payment.info_data.get("quota_exceeded"))
                self.assertTrue(all(p.state == "confirmed" for p in order.payments.all()))

    def test_last_invoice_releases_shared_hold_in_either_close_order(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                order, first = self.make_order(payment_state="canceled")
                original = order.expires
                second = self.add_payment(order)
                for payment in (first, second):
                    sync.apply(self.provider, payment, self.invoice(payment, state.PROCESSING), "regression")
                second.refresh_from_db()
                self.assertNotIn("held_from", second.info_data)
                closing = (second, first) if reverse else (first, second)
                sync.apply(self.provider, closing[0], self.invoice(closing[0], state.EXPIRED, "0"), "regression")
                order.refresh_from_db()
                self.assertGreater(order.expires, now() + timedelta(hours=24))
                sync.apply(self.provider, closing[1], self.invoice(closing[1], state.EXPIRED, "0"), "regression")
                order.refresh_from_db()
                self.assertLess(abs((order.expires - original).total_seconds()), 2)
                self.assertTrue(all("held_from" not in p.info_data for p in order.payments.all()))
                # A new hold must not reuse the old deadline from a completed group of invoices.
                order.expires = now() + timedelta(hours=2)
                order.save(update_fields=["expires"])
                new_deadline = order.expires
                third = self.add_payment(order)
                sync.apply(self.provider, third, self.invoice(third, state.PROCESSING), "regression")
                sync.apply(self.provider, third, self.invoice(third, state.INVALID, "0"), "regression")
                order.refresh_from_db()
                self.assertLess(abs((order.expires - new_deadline).total_seconds()), 2)

    def test_release_contention_stays_due_and_retries(self):
        order, payment = self.make_order(payment_state="canceled")
        original = order.expires
        sync.apply(self.provider, payment, self.invoice(payment, state.PROCESSING), "regression")
        other = self.other_connection()
        other.set_autocommit(False)
        with other.cursor() as cursor:
            cursor.execute("SELECT id FROM pretixbase_order WHERE id = %s FOR UPDATE", [order.pk])
        try:
            with self.assertRaises(sync.OrderBusy):
                sync.apply(self.provider, payment, self.invoice(payment, state.INVALID, "0"), "regression")
            payment.refresh_from_db()
            self.assertEqual(sync.next_check(payment, now()), payment.info_data["checked"] + sync.ACTIVE)
        finally:
            other.rollback()
        sync.apply(self.provider, payment, self.invoice(payment, state.INVALID, "0"), "regression")
        order.refresh_from_db()
        payment.refresh_from_db()
        self.assertLess(abs((order.expires - original).total_seconds()), 2)
        self.assertNotIn("held_from", payment.info_data)

    def test_paid_hold_cannot_shorten_a_later_upgrade_deadline(self):
        order, first = self.make_order()
        sync.apply(self.provider, first, self.invoice(first, state.PROCESSING), "regression")
        sync.apply(self.provider, first, self.invoice(first, state.SETTLED), "regression")
        first.refresh_from_db()
        self.assertNotIn("held_from", first.info_data)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.STATUS_PAID)
        # An upgrade reopens the order with a fresh payment deadline.
        order.total = Decimal("399.00")
        order.status = Order.STATUS_PENDING
        order.expires = now() + timedelta(hours=2)
        order.save(update_fields=["total", "status", "expires"])
        original = order.expires
        second = self.add_payment(order)
        sync.apply(self.provider, second, self.invoice(second, state.PROCESSING), "regression")
        sync.apply(self.provider, second, self.invoice(second, state.INVALID, "0"), "regression")
        order.refresh_from_db()
        self.assertLess(abs((order.expires - original).total_seconds()), 2)

    def test_hold_cannot_reopen_an_order_paid_after_the_initial_read(self):
        order, payment = self.make_order(order_state=Order.STATUS_EXPIRED)
        other = self.other_connection()
        original = sync._update_info
        competing_commits = []

        def pay_before_extension(current, **values):
            if "held_from" in values:
                # A backend finalization races the stale expired-order snapshot used by the hold.
                other.set_autocommit(False)
                try:
                    with other.cursor() as cursor:
                        cursor.execute("SELECT id FROM pretixbase_order WHERE id = %s FOR UPDATE NOWAIT", [order.pk])
                        cursor.execute("UPDATE pretixbase_order SET status = %s WHERE id = %s", [Order.STATUS_PAID, order.pk])
                    other.commit()
                    competing_commits.append(True)
                except DatabaseError as e:
                    other.rollback()
                    cause = e.__cause__
                    self.assertEqual(getattr(cause, "sqlstate", None) or getattr(cause, "pgcode", None), "55P03")
                    competing_commits.append(False)
                finally:
                    other.set_autocommit(True)
            return original(current, **values)

        with patch.object(sync, "_update_info", pay_before_extension):
            sync.apply(self.provider, payment, self.invoice(payment, state.PROCESSING), "regression")
        order.refresh_from_db()
        self.assertEqual(len(competing_commits), 1)
        if competing_commits[0]:
            self.assertEqual(order.status, Order.STATUS_PAID, "A hold reopened an order already paid in the backend")
        else:
            self.assertEqual(order.status, Order.STATUS_PENDING)

    def test_hold_retries_if_the_order_row_is_busy(self):
        order, payment = self.make_order()
        other = self.other_connection()
        other.set_autocommit(False)
        with other.cursor() as cursor:
            cursor.execute("SELECT id FROM pretixbase_order WHERE id = %s FOR UPDATE", [order.pk])
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET statement_timeout = '1s'")
            with self.assertRaises(sync.OrderBusy):
                sync.apply(self.provider, payment, self.invoice(payment, state.PROCESSING), "regression")
        finally:
            other.rollback()
            with connection.cursor() as cursor:
                cursor.execute("SET statement_timeout = '0'")
        sync.apply(self.provider, payment, self.invoice(payment, state.PROCESSING), "regression")
        order.refresh_from_db()
        payment.refresh_from_db()
        self.assertEqual(payment.state, "pending")
        self.assertGreater(order.expires, now() + timedelta(hours=24))

    def test_confirmation_cannot_reverse_a_concurrent_refund(self):
        order, payment = self.make_order()
        original = state.decide
        completed = []

        def refund_after_decision(*args, **kwargs):
            decision = original(*args, **kwargs)
            if not completed:
                current = OrderPayment.objects.get(pk=payment.pk)
                current.confirm()
                refund = order.refunds.create(
                    payment=current, source=OrderRefund.REFUND_SOURCE_ADMIN,
                    state=OrderRefund.REFUND_STATE_CREATED, amount=current.amount, provider="manual",
                )
                refund.done()
                completed.append(True)
            return decision

        with patch.object(state, "decide", refund_after_decision):
            sync.apply(self.provider, payment, self.invoice(payment, state.SETTLED), "regression")
        payment.refresh_from_db()
        self.assertEqual(payment.state, "refunded")
        self.assertEqual(order.all_logentries().filter(action_type="pretix.event.order.payment.confirmed").count(), 1)
        self.assertEqual(order.all_logentries().filter(action_type="pretix_btcpay.settled_after_refund").count(), 1)

    def test_hold_waits_for_pending_payment_from_another_provider(self):
        order, payment = self.make_order(payment_state="canceled")
        original = order.expires
        sync.apply(self.provider, payment, self.invoice(payment, state.PROCESSING), "regression")
        other = order.payments.create(provider="manual", amount=Decimal("266.00"), state="pending", info='["other-provider"]')
        sync.apply(self.provider, payment, self.invoice(payment, state.INVALID, "0"), "regression")
        order.refresh_from_db()
        self.assertGreater(order.expires, now() + timedelta(hours=24))
        other.state = "failed"
        other.save(update_fields=["state"])
        sync.apply(self.provider, payment, self.invoice(payment, state.INVALID, "0"), "regression")
        order.refresh_from_db()
        self.assertLess(abs((order.expires - original).total_seconds()), 2)

    def test_payment_method_switch_retries_without_deadlock_then_counts_late_payment(self):
        order, payment = self.make_order()
        updated = threading.Event()
        changer_has_payment = threading.Event()
        sync_has_order = threading.Event()
        armed = threading.Event()
        results = {}
        original_update, original_cancel = sync._update_info, BTCPayServer.cancel_payment

        def coordinated_update(current, **values):
            result = original_update(current, **values)
            if threading.current_thread().name == "regression-sync" and "status" in values:
                updated.set()
                if not changer_has_payment.wait(5):
                    raise RuntimeError("Payment-method switch never acquired its payment row")
                armed.set()
            return result

        def coordinated_cancel(provider, current):
            if threading.current_thread().name == "regression-change":
                changer_has_payment.set()
                if not sync_has_order.wait(5):
                    raise RuntimeError("Reconciliation never acquired its order row")
            return original_cancel(provider, current)

        def signal_payment_lock(execute, sql, params, many, context):
            if armed.is_set() and "pretixbase_orderpayment" in sql and "FOR UPDATE" in sql:
                sync_has_order.set()  # the order row was taken immediately before this query
            return execute(sql, params, many, context)

        def worker(kind):
            try:
                with scope(organizer=EVENT_OBJECT.organizer):
                    with connection.cursor() as cursor:
                        cursor.execute("SET statement_timeout = '8s'")
                    if kind == "sync":
                        current = OrderPayment.objects.get(pk=payment.pk)
                        with connection.execute_wrapper(signal_payment_lock):
                            sync.apply(self.provider, current, self.invoice(current, state.SETTLED), "regression")
                    else:
                        if not updated.wait(5):
                            raise RuntimeError("Reconciliation never read the invoice")
                        with transaction.atomic():
                            current = Order.objects.get(pk=order.pk)
                            change_payment_provider(current, BTCPayServer(EVENT_OBJECT), recreate_invoices=False)
                results[kind] = None
            except Exception as e:
                results[kind] = e
            finally:
                connections.close_all()

        with patch.object(sync, "_update_info", coordinated_update), patch.object(BTCPayServer, "cancel_payment", coordinated_cancel):
            workers = [threading.Thread(target=worker, name=f"regression-{kind}", args=(kind,), daemon=True)
                       for kind in ("sync", "change")]
            for thread in workers:
                thread.start()
            for thread in workers:
                thread.join(timeout=12)
            self.assertFalse(any(t.is_alive() for t in workers), "A worker remained blocked")
        self.assertIsInstance(results.get("sync"), sync.OrderBusy, results)
        self.assertIn("change", results)
        self.assertIsNone(results["change"], results)
        payment.refresh_from_db()
        self.assertEqual(payment.state, "canceled")
        # The retry must count money arriving on the invoice whose payment was just canceled.
        sync.apply(self.provider, payment, self.invoice(payment, state.SETTLED), "regression")
        order.refresh_from_db()
        self.assertEqual(order.status, Order.STATUS_PAID)
        self.assertEqual(sync._net_paid(order), order.total)


result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(PaymentRegressions))
if not result.wasSuccessful():
    raise AssertionError("Payment regression checks failed")
