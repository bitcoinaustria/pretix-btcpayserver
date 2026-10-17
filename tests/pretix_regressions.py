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
from unittest.mock import patch

from django.db import DatabaseError, connection, connections, transaction
from django.test import RequestFactory
from django.utils.timezone import now
from django_scopes import scope, scopes_disabled
from pretix.base.models import Event, Order, OrderFee, OrderPayment, OrderRefund
from pretix.base.services.orders import change_payment_provider

from pretix_btcpay import state, sync, views
from pretix_btcpay.payment import BTCPayServer

ORGANIZER, EVENT = os.environ.get("CHECK_EVENT", "zitadelle/2027").split("/")
with scopes_disabled():
    EVENT_OBJECT = Event.objects.get(organizer__slug=ORGANIZER, slug=EVENT)
if not EVENT_OBJECT.testmode or connection.vendor != "postgresql":
    raise RuntimeError("These checks require a test-mode event and PostgreSQL.")


class PaymentRegressions(unittest.TestCase):
    def setUp(self):
        self.enterContext(scope(organizer=EVENT_OBJECT.organizer))
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
