"""Real pretix reservation/expiry paths with synthetic invoices; no network or customer data."""
import json
from contextlib import nullcontext
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import RequestFactory, TestCase
from django.utils.timezone import now
from django.utils.translation import override
from django_scopes import scopes_disabled
from pretix.base.models import CartPosition, Event, Item, Order, OrderPayment, Organizer, Quota
from pretix.base.services.orders import _create_order, approve_order, deny_order, expire_orders

from pretix_btcpay import review, state, sync
from pretix_btcpay.payment import BTCPayServer
from pretix_btcpay.signals import review_notice, review_notifications


class ManualReviewIntegration(TestCase):
    def setUp(self):
        self.enterContext(scopes_disabled())
        self.enterContext(override('en'))
        # SQLite exercises real pretix transactions and quota semantics. PostgreSQL lock contention is covered by
        # pretix_regressions; never substitute this fixture for that concurrency suite.
        self.enterContext(patch('pretix_btcpay.sync.advisory_lock', side_effect=lambda *a, **k: nullcontext()))
        self.enterContext(patch.object(Order, 'send_mail', return_value=None))
        self.enterContext(patch.object(OrderPayment, '_send_paid_mail', return_value=None))
        organizer = Organizer.objects.create(name='Review fixture', slug='review')
        self.event = Event.objects.create(organizer=organizer, name='Review fixture', slug='test',
                                         date_from=now() + timedelta(days=30), currency='EUR', testmode=True,
                                         plugins='pretix_btcpay')
        self.event.settings.set('payment_term_expire_automatically', True)
        self.event.settings.set('ticket_download_pending', False)
        self.provider = BTCPayServer(self.event)
        self.provider.settings.set('manual_review', True)
        self.provider.settings.set('store_id', 'TEST')
        self.provider.settings.set('url', 'https://pay.example.org')
        self.channel = organizer.sales_channels.get(identifier='web')
        self.item = Item.objects.create(event=self.event, name='Pass', default_price=Decimal('100'), admission=True)
        self.quota = Quota.objects.create(event=self.event, name='One place', size=1)
        self.quota.items.add(self.item)

    def order(self):
        cart = CartPosition.objects.create(event=self.event, item=self.item, price=Decimal('100'), tax_rate=Decimal('0'),
                                           tax_value=Decimal('0'), cart_id='review', expires=now() + timedelta(minutes=5))
        order, payments = _create_order(self.event, email=None, positions=[cart], now_dt=now(),
                                         payment_requests=[{'provider': self.provider.identifier,
                                                            'pprov': self.provider, 'info_data': {}}],
                                         sales_channel=self.channel, locale='en')
        payment = payments[0]
        payment.info_data = {'invoice_id': 'TEST' + str(payment.pk)}
        payment.save(update_fields=['info'])
        return order, payment

    def invoice(self, payment, status, paid='100', extra='None'):
        return {'id': payment.info_data['invoice_id'], 'storeId': 'TEST', 'amount': str(payment.amount),
                'currency': 'EUR', 'status': status, 'paidAmount': paid, 'additionalStatus': extra,
                'checkout': {'speedPolicy': 'MediumSpeed'},
                'monitoringExpiration': int((now() + timedelta(hours=24)).timestamp()),
                'metadata': {'pretixPaymentId': payment.pk, 'orderId': payment.order.code,
                             'pretixEvent': 'review/test'}}

    def apply(self, payment, status, paid='100', extra='None'):
        # No requests to BTCPay. Surplus is not part of this fixture.
        with patch.object(BTCPayServer, 'client') as client:
            client.get_invoice_payment_methods.return_value = [
                {'rate': '1', 'payments': [{'status': 'Settled', 'value': paid, 'fee': '0'}]}]
            result = sync.apply(self.provider, payment, self.invoice(payment, status, paid, extra), 'fixture')
        payment.refresh_from_db()
        payment.order.refresh_from_db()
        return result

    def overdue_and_expire(self, order):
        Order.objects.filter(pk=order.pk).update(expires=now() - timedelta(days=1))
        expire_orders(sender=None)
        order.refresh_from_db()

    def test_default_off_preserves_existing_behavior(self):
        self.provider.settings.set('manual_review', False)
        order, payment = self.order()
        self.apply(payment, state.INVALID)
        self.overdue_and_expire(order)
        self.assertEqual(order.status, Order.STATUS_EXPIRED)
        self.assertNotIn('review_required', payment.info_data)

    def test_no_funds_expire_normally(self):
        order, payment = self.order()
        self.apply(payment, state.EXPIRED, '0')
        self.overdue_and_expire(order)
        self.assertEqual(order.status, Order.STATUS_EXPIRED)
        self.assertFalse(order.require_approval)
        self.assertEqual(payment.state, 'failed')

    def test_processing_uses_normal_hold_and_confirms_automatically(self):
        order, payment = self.order()
        self.apply(payment, state.PROCESSING)
        self.assertFalse(payment.order.require_approval)
        self.assertEqual(payment.state, 'pending')
        self.assertGreater(payment.order.expires, now() + timedelta(hours=24))
        self.apply(payment, state.SETTLED)
        self.assertEqual((payment.state, payment.order.status), ('confirmed', Order.STATUS_PAID))

    def test_partial_receipt_gets_time_to_complete_then_manual_review(self):
        order, payment = self.order()
        self.apply(payment, state.NEW, '20', 'PaidPartial')
        self.assertEqual(payment.state, 'created')
        self.assertFalse(payment.order.require_approval)
        self.assertGreater(payment.order.expires, now() + timedelta(hours=24))
        self.assertEqual(self.apply(payment, state.EXPIRED, '20', 'PaidPartial'), 'review')
        self.overdue_and_expire(order)
        self.assertEqual(order.status, Order.STATUS_PENDING)
        self.assertTrue(order.require_approval)
        self.assertFalse(order.ticket_download_available)
        self.assertEqual(sync._net_paid(order), Decimal('0'))

    def test_invalid_seen_funds_hold_even_beyond_expiry_and_notify_once(self):
        order, payment = self.order()
        self.apply(payment, state.PROCESSING)
        self.apply(payment, state.INVALID)
        self.apply(payment, state.INVALID)
        self.overdue_and_expire(order)
        self.assertTrue(order.require_approval)
        self.assertEqual(order.status, Order.STATUS_PENDING)
        self.assertEqual(order.custom_followup_at, now().astimezone(self.event.timezone).date())
        self.assertEqual(order.all_logentries().filter(action_type='pretix_btcpay.manual_review').count(), 1)
        self.assertIn('places remain reserved', review_notice(self.event, order).lower())
        # Quota remains blocked by the same native approval order, despite its deadline being in the past.
        self.assertEqual(self.quota.availability()[1], 0)

    def test_replacement_removing_all_funds_is_still_manual(self):
        order, payment = self.order()
        self.apply(payment, state.PROCESSING)
        self.apply(payment, state.INVALID, '0')
        self.assertTrue(payment.order.require_approval)
        self.assertTrue(payment.info_data['seen_at'])

    def test_late_first_receipt_is_reviewed(self):
        order, payment = self.order()
        self.apply(payment, state.EXPIRED, '100', 'PaidLate')
        self.assertTrue(review.active(payment))

    def test_disabling_option_does_not_release_a_hold(self):
        order, payment = self.order()
        self.apply(payment, state.INVALID)
        self.provider.settings.set('manual_review', False)
        self.apply(payment, state.INVALID)
        self.overdue_and_expire(order)
        self.assertTrue(order.require_approval)
        self.assertEqual(order.status, Order.STATUS_PENDING)

    def test_native_approval_is_manual_resolution_and_not_rearmed(self):
        order, payment = self.order()
        self.apply(payment, state.INVALID)
        order.refresh_from_db()
        approve_order(order, send_mail=False)
        approved_deadline = Order.objects.get(pk=order.pk).expires
        self.apply(payment, state.INVALID)
        self.apply(payment, state.INVALID)
        self.assertFalse(payment.order.require_approval)
        self.assertFalse(payment.info_data['review_required'])
        self.assertEqual(payment.order.expires, approved_deadline)
        self.assertNotIn('held_from', payment.info_data)

    def test_native_denial_is_never_reopened_by_replay_or_settlement(self):
        order, payment = self.order()
        self.apply(payment, state.INVALID)
        order.refresh_from_db()
        deny_order(order, send_mail=False)
        self.apply(payment, state.INVALID)
        self.apply(payment, state.SETTLED)
        self.assertEqual(payment.order.status, Order.STATUS_CANCELED)

    def test_settlement_releases_our_hold_but_never_an_unrelated_approval(self):
        order, payment = self.order()
        self.apply(payment, state.INVALID)
        self.apply(payment, state.SETTLED)
        self.assertFalse(payment.order.require_approval)
        self.assertEqual(payment.order.status, Order.STATUS_PAID)
        self.assertFalse(payment.info_data['review_required'])

    def test_original_approval_is_preserved_after_payment(self):
        order, payment = self.order()
        order.require_approval = True
        order.save(update_fields=['require_approval'])
        self.apply(payment, state.INVALID)
        self.assertFalse(payment.info_data.get('review_approval_owned'))
        self.apply(payment, state.SETTLED)
        self.assertTrue(payment.order.require_approval)
        self.assertEqual(payment.order.status, Order.STATUS_PENDING)
        self.assertEqual(payment.state, 'confirmed')
        self.assertFalse(payment.info_data.get('quota_exceeded'))

    def test_pending_warnings_are_deduplicated(self):
        order, payment = self.order()
        self.apply(payment, state.PROCESSING)
        info = payment.info_data
        info['seen_at'] = int((now() - timedelta(hours=25)).timestamp())
        payment.info_data = info
        payment.save(update_fields=['info'])
        self.apply(payment, state.PROCESSING)
        self.apply(payment, state.PROCESSING)
        for action in ('confirmation_delayed_2h', 'confirmation_delayed_24h'):
            self.assertEqual(order.all_logentries().filter(action_type='pretix_btcpay.' + action).count(), 1)

    def test_registered_notifications_are_permission_scoped_and_contain_no_buyer_details(self):
        order, payment = self.order()
        self.apply(payment, state.INVALID)
        log = order.all_logentries().get(action_type='pretix_btcpay.manual_review')
        kind = next(n for n in review_notifications(self.event) if n.action_type == log.action_type)
        self.assertEqual(kind.required_permission, 'event.orders:read')
        notification = kind.build_notification(log)
        self.assertIn(order.code, notification.title)
        self.assertIn('/control/', notification.url)
        self.assertEqual(notification.attributes, [])

    def test_buyer_notice_translated_and_not_shown_on_paid_order(self):
        order, payment = self.order()
        self.apply(payment, state.INVALID)
        with override('de_Informal'):
            self.assertIn('Nicht erneut bezahlen', self.provider.payment_pending_render(RequestFactory().get('/'), payment))
        self.apply(payment, state.SETTLED)
        self.assertEqual(review_notice(self.event, payment.order), '')
