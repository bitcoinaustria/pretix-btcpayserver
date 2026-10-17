"""
A change of BTCPay server or store against a checkout on another connection, in both orders, in an isolated test-mode
event with mocked BTCPay calls; operator settings and the remote BTCPay stay untouched:

    docker exec -i <pretix container> pretix shell < tests/pretix_settings_race.py
    docker exec -i -e REVIEW_MOVE_SERVER=1 <pretix container> pretix shell < tests/pretix_settings_race.py

Settings first: the checkout waits for them, then refuses to create an invoice with the old connection. Checkout first:
the change waits for it, then counts its invoice as open and is refused. Prints FIXED 1 and FIXED 2.
"""
import json
import os
import threading
import uuid
from collections import OrderedDict
from datetime import timedelta
from decimal import Decimal
from unittest.mock import Mock, patch

from django.db import connection, connections, transaction
from django.utils.timezone import now
from django_scopes import scope, scopes_disabled
from pretix.base.models import Event, Order, OrderFee, OrderPayment
from pretix.control.forms.event import ProviderForm
from pretix_btcpay import sync
from pretix_btcpay.api import BTCPayAPI
from pretix_btcpay.checks import REQUIRED_PERMISSIONS
from pretix_btcpay.payment import BTCPayServer

with scopes_disabled():
    main = Event.objects.get(organizer__slug='zitadelle', slug='2027')
assert main.testmode and connection.vendor == 'postgresql'
with scope(organizer=main.organizer):
    event = Event.objects.create(organizer=main.organizer, slug='review-b-race-' + uuid.uuid4().hex[:12],
                                name={'en': 'Reviewer B isolated race'}, date_from=now(), currency=main.currency,
                                testmode=True, plugins=['pretix_btcpay'])
    try:
        event.settings.set('locales', ['en'])
        provider = BTCPayServer(event)
        for key, value in dict(url='https://pay.example.org', store_id='OLD-STORE', api_key='test-key',
                               webhook_id='OLD-WEBHOOK', webhook_secret='old-test-secret', pending_webhook='',
                               _enabled=True).items():
            provider.settings.set(key, value)
        with transaction.atomic():
            order = Order.objects.create(event=event, status='n', testmode=True, total=Decimal('1.00'),
                                        expires=now() + timedelta(minutes=30), locale='en', email=None,
                                        sales_channel=main.organizer.sales_channels.get(identifier='web'))
            order.fees.create(fee_type=OrderFee.FEE_TYPE_OTHER, value=Decimal('1.00'), description='Review race fixture')
            order.create_transactions()
            payment = order.payments.create(provider=sync.PROVIDER, amount=Decimal('1.00'), state='created', info='{}')
        results, errors = [], []

        def checkout():
            try:
                with scope(organizer=main.organizer):
                    fresh_event = Event.objects.get(pk=event.pk)
                    old_provider = BTCPayServer(fresh_event)
                    old_client = Mock(spec=BTCPayAPI)
                    old_client.url = 'https://pay.example.org'
                    old_client.create_invoice.return_value = {
                        'id': 'RACE-INVOICE', 'status': 'New', 'checkoutLink': 'https://pay.example.org/i/RACE-INVOICE',
                        'expirationTime': int((now() + timedelta(minutes=15)).timestamp())}
                    old_provider.__dict__['client'] = old_client
                    results.append(old_provider.execute_payment(None, OrderPayment.objects.get(pk=payment.pk)))
                    old_client.create_invoice.assert_called_once()
            except Exception as error:
                errors.append(error)
            finally:
                connections.close_all()

        new_client = Mock(spec=BTCPayAPI)
        switching_server = os.environ.get('REVIEW_MOVE_SERVER') == '1'
        new_server = 'https://new.example.org' if switching_server else 'https://pay.example.org'
        new_store = 'OLD-STORE' if switching_server else 'NEW-STORE'
        new_client.url = new_server
        new_client.current_key.return_value = {'permissions': [p + ':' + new_store for p in REQUIRED_PERMISSIONS]}
        new_client.list_webhooks.return_value = []
        new_client.create_webhook.return_value = {'id': 'NEW-WEBHOOK'}
        prefix = provider.settings.get_prefix()
        data = {'url': new_client.url, 'store_id': new_store, 'api_key': 'test-key', '_enabled': 'on',
                'speed_policy': 'MediumSpeed', '_restrict_to_sales_channels': ['web']}
        with transaction.atomic(), patch('pretix_btcpay.payment.BTCPayAPI', return_value=new_client):
            form = ProviderForm(obj=event, settingspref=prefix, provider=provider,
                                data={prefix + k: v for k, v in data.items()})
            form.fields = OrderedDict((prefix + k, v) for k, v in provider.settings_form_fields.items())
            form.prepare_fields()
            assert form.is_valid(), form.errors
            # Real ensure_webhook ran and keeps the registration lock until this settings transaction commits.
            new_client.create_webhook.assert_called_once()
            worker = threading.Thread(target=checkout, daemon=True)
            worker.start()
            worker.join(3)
            assert worker.is_alive(), 'Checkout did not wait for the settings transaction'
            form.save()
        worker.join(15)
        assert not worker.is_alive(), 'Checkout still blocked after the settings were saved'
        assert not results and len(errors) == 1 and ('changed' in str(errors[0]) or 'geändert' in str(errors[0])), (results, errors)
        payment.refresh_from_db()
        assert not payment.info_data.get('invoice_id'), payment.info_data
        print('FIXED 1: settings first -> checkout waited, then refused with', repr(str(errors[0])), '; no old invoice')

        # Second order: a checkout with the old connection is in flight, then the change is validated.
        event.settings.flush()
        event.settings.set(prefix + 'url', 'https://pay.example.org')
        event.settings.set(prefix + 'store_id', 'OLD-STORE')
        results.clear(); errors.clear()
        started, release = threading.Event(), threading.Event()

        def slow_checkout():
            try:
                with scope(organizer=main.organizer):
                    p2 = BTCPayServer(Event.objects.get(pk=event.pk))
                    c2 = Mock(spec=BTCPayAPI)
                    c2.url = 'https://pay.example.org'
                    def create(*a, **k):
                        started.set(); release.wait(10)
                        return {'id': 'RACE-INVOICE-2', 'status': 'New', 'checkoutLink': 'https://pay.example.org/i/RACE-INVOICE-2',
                                'expirationTime': int((now() + timedelta(minutes=15)).timestamp())}
                    c2.create_invoice.side_effect = create
                    p2.__dict__['client'] = c2
                    results.append(p2.execute_payment(None, OrderPayment.objects.get(pk=payment.pk)))
            except Exception as error:
                errors.append(error)
            finally:
                connections.close_all()

        worker = threading.Thread(target=slow_checkout, daemon=True)
        worker.start()
        assert started.wait(10), 'checkout never reached invoice creation'
        threading.Timer(2, release.set).start()
        refused = None
        with transaction.atomic(), patch('pretix_btcpay.payment.BTCPayAPI', return_value=new_client):
            fresh = BTCPayServer(Event.objects.get(pk=event.pk))
            form = ProviderForm(obj=event, settingspref=prefix, provider=fresh, data={prefix + k: v for k, v in data.items()})
            form.fields = OrderedDict((prefix + k, v) for k, v in fresh.settings_form_fields.items())
            form.prepare_fields()
            valid = form.is_valid()
            refused = str(form.errors)
        worker.join(15)
        assert not errors and results == ['https://pay.example.org/i/RACE-INVOICE-2'], (results, errors)
        assert not valid and ('invoices of this event' in refused or 'Rechnungen dieses Events' in refused), refused
        print('FIXED 2: checkout first -> change waited for it, then refused:', refused[:120])
    finally:
        with transaction.atomic():
            Order.gracefully_delete_bulk(event, Order.objects.filter(event=event, testmode=True))
            event.delete()
        print('CLEANUP: isolated event and fixture orders deleted; operator settings and remote BTCPay untouched.')
