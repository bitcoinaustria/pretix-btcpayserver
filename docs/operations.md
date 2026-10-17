# Operating the Greenfield payment provider

This guide applies to this fork with pretix 2026.7 and BTCPay Server 2.3.9. Test a new pretix or BTCPay release on a
separate test-mode event before upgrading production. [README](../README.md) covers installation and API permissions.

## Store settings

There are three independent clocks: the pretix payment deadline, the invoice payment window and BTCPay's monitoring
period. A short invoice window limits how long an exchange rate remains fixed; it does not require an on-chain
transaction to confirm before that window ends.

| Setting | Source for a new plugin invoice | Effect |
|---|---|---|
| Invoice expiration | pretix payment settings, default 15 minutes, capped by the remaining order deadline | Time to pay the invoice in full at its quoted rate. BTCPay requires at least one minute. |
| Speed policy | pretix payment settings: `MediumSpeed`, `LowMediumSpeed`, `LowSpeed` | On-chain settlement after 1, 2 or 6 confirmations. Invalid stored choices fall back to 1. Lightning settles immediately. |
| Payment tolerance | plugin, always zero | A partial transfer cannot settle an invoice as if fully paid. |
| Monitoring expiration | BTCPay store, not overridden by the plugin | How long BTCPay watches for confirmations and late payments. Use the returned invoice timestamp `monitoringExpiration` to determine the actual deadline. |

BTCPay 2.3.9 defaults to a 15-minute invoice window and a 24-hour monitoring period. Review the monitoring setting
in the store's Bitcoin wallet settings (the wording is “Payment invalid if transaction fails to confirm…”). Keep
enough time for slow confirmations; lowering it can make received but unconfirmed payments invalid sooner. Store
changes do not rewrite an invoice already created. The plugin's per-invoice confirmation policy overrides the store
default, and reconciliation refuses a returned `HighSpeed`, unknown or missing policy, even for a `Settled` invoice.
See [BTCPay store documentation](https://docs.btcpayserver.org/FAQ/Stores/) and the
[2.3.9 defaults](https://github.com/btcpayserver/btcpayserver/blob/v2.3.9/BTCPayServer/Data/StoreBlob.cs).

Leave pretix's **Generate tickets for pending orders** (`ticket_download_pending`) off if tickets must be released
only after payment, and do not mark an unpaid order valid (`valid_if_pending`). The plugin controls the payment and
order states; pretix can separately authorize ticket downloads for unpaid orders.
While an invoice is `Processing`, pretix's payment is pending, its order is unpaid, and no paid ticket is released.
The reservation target is `monitoringExpiration` plus one hour for delivery and polling. If that timestamp
is absent or already past, it uses 24 hours from the check plus one hour. An existing later reservation is preserved.
Without a `monitoringExpiration` the target moves with the clock, so an existing deadline up to five minutes short of
it is kept rather than extended on every check.
When BTCPay drops the payment (`Invalid` or `Expired`), the plugin restores the original deadline with at least ten
minutes to retry, once all payments needing the hold have finished. Merely reaching invoice expiration does not
release a `Processing` payment's seats. If the seats were already sold before an expired order could be extended or
paid, the plugin flags the order for the team instead of forcing quota.

A partial `New` invoice stays unpaid and does not get this confirmation hold. An RBF replacement can move a
`Processing` invoice back to `New`; the payment remains pending and the buyer gets a link to complete that same
invoice. A fully paid invoice settles only after its policy's confirmations. BTCPay can leave a payment received
after expiration as `Expired` / `PaidLate`; a human must review it. BTCPay's term “paid” in some screens does not mean
pretix has confirmed the payment. See [invoice states](https://docs.btcpayserver.org/Users/invoices/).

## Delivery and recovery

Run pretix cron regularly and keep its background worker processing tasks. The poll's minimum interval is one minute,
not a promise of one-minute delivery: cron cadence, queue delay, locks and invoice backlog affect recovery. Each run
starts at most 300 checks and starts no new check after 45 seconds; an active request finishes. A busy order is skipped
and retried. The buyer's status endpoint never contacts BTCPay and cannot substitute for cron or webhooks.

After saving payment settings, check the event webhook in BTCPay: enabled, automatic redelivery, correct public
HTTPS URL, and the events listed in `pretix_btcpay/api.py`. A 503 “Busy, please retry” is expected during order-row or
advisory-lock contention; BTCPay must redeliver it. Replayed or out-of-order deliveries read the current invoice from
the API, so they cannot re-confirm a payment. A 503 saying BTCPay is unavailable needs API/network recovery. Persistent
503s without recovery need investigation. An invoice mismatch is acknowledged with 200 and logged on the order;
check its store, event, amount, currency, speed policy and payment metadata rather than expecting automatic retries.

Turning off the payment method prevents new sales through it, but the poll keeps reconciling existing invoices while
the plugin is enabled and its URL, store and key remain configured. Do not disable/uninstall the plugin or replace its
store credentials while invoices still need reconciliation without an explicit migration and recovery plan.

## Changing the server or store

The payment settings form refuses a different server or store while an event has created/pending payments with an
invoice, a non-final invoice, or a settled invoice whose base payment has not been booked. Expired invoices remain
open while their monitoring deadline is in the future. A settled or invalid invoice is treated as final; a settled
and confirmed/refunded base payment alone does not block the change. Rotating the API key on the same server/store
is allowed. Scheme/host case, default ports and trailing slashes do not make an equivalent address a new server.

Current caveat: a confirmed payment can still have unbooked extra money (`surplus_open`), for example while a second
transfer awaits confirmation or the payment-method API is unavailable. The guard currently permits a switch in that
state. Check for unresolved surplus as well as open base payments, and keep the original server/store configured
until all received money is booked. New transfers to old settled invoices after a switch also require manual review.

New invoices remember their server and store. Reconciliation skips a payment from another origin without contacting
the new store, updates its check timestamp and logs `other_store` once for manual recovery. This includes an invoice
created while a settings change was being saved; the guard does not serialize invoice creation with settings saves.
Do not assume that changing settings migrates old invoices or keeps reconciling them with the old credentials.

## Reorganizations after settlement

BTCPay 2.3.9 can keep an invoice and its payment marked settled after a reorganization removes the confirming
transaction and a conflicting transaction confirms instead. The plugin's Greenfield reads cannot detect that loss:
pretix keeps the order paid without an attention note. This accepted limitation also applies while polling works.
Use two or six confirmations where that risk warrants the extra wait; neither removes reorganization risk entirely.

## Orders needing attention

Review the order's payment details and history alongside the BTCPay invoice and transactions:

- Partial or late payments, money on an invalid/expired invoice, and payments after a refund require a human decision.
  Verify the amount and confirmations before using BTCPay's “Mark settled”. That admin action is trusted and can
  confirm a payment without blockchain evidence. Return funds if the order cannot be fulfilled.
- Quota exceeded: the money remains a confirmed payment, while the order stays unpaid. Add quota and resolve the
  order in pretix or refund it. The plugin stops automatic finalization once it has flagged a quota failure.
- Cancelled order: settlement records the money but does not reopen the order. Return the payment if the cancelled
  order will not be fulfilled.
- Double payment: two different invoices can both receive money. Both payments count, with one double-payment note
  for the newly confirmed second payment if the order was already covered after refunds. Refund the excess.
- Overpayment on one invoice: only confirmed receipts count toward separate surplus payments. An unconfirmed extra
  transfer does not create refundable surplus. Refunding a surplus payment preserves the ticket's base payment.
- An interrupted confirmation can leave a confirmed payment on an unpaid order. Webhook/poll reconciliation finishes
  the order if its net payments cover the current total and quota is still available.

Backend invoice details can be 30 seconds stale. A cache miss still waits for BTCPay; a successful cached read is a
display optimization, not payment reconciliation. Compare pretix payment state and the current BTCPay invoice before
intervening. The plugin does not reverse an already confirmed payment if an admin later marks its invoice invalid;
it logs the discrepancy for a human.

## Refund completion

With automatic refund creation off, send the money by hand in BTCPay and record the completed refund in pretix.
With it on, the first refund of each payment creates an unapproved pull-payment claim. Give the claim link to the
buyer, review the claimed address and amount, approve the payout in BTCPay, and check it has actually been paid.
The plugin leaves the pretix refund in transit: record completion in pretix after verifying the payout. It does not
poll payouts or mark refunds done automatically.

A timeout or uncertain response can mean a claim exists despite no claim link in pretix. Search BTCPay by the exact
refund name recorded in the order log before sending anything else. Further refunds of that same payment are blocked
and must be handled by hand; base and surplus payments have separate guards. Keep the shared cache persistent and
avoid clearing/evicting these guards during unresolved refunds. A database backup does not include the Redis guards.

## Upgrades

Pin the exact plugin commit or artifact; this fork currently uses version `0.2.0` across its hardening commits, so
the version alone does not identify the deployed behavior. Back up the database, BTCPay data and the persistent refund
cache before upgrading. Test the chosen artifact, rebuild assets, and restart every web and worker process, including
any separate cron runtime. Save each event's payment settings, then verify webhook delivery and fallback polling.
Follow the [pretix plugin deployment procedure](https://docs.pretix.eu/self-hosting/installation/manual_smallscale/#install-a-plugin).

This is not an automatic migration from upstream, `pretix-bitpay`, or an older fork that stored multiple invoices on
one payment. There is no data migration for those records; only `btcpay_greenfield` payments with the current
`invoice_id` and matching Greenfield metadata are reconciled. Inventory unresolved old invoices, payments, reservations
and refunds before switching. Keep a way to reconcile/refund every old invoice, or write and test a dedicated migration.
Fresh event installations avoid that historical-data problem.

Before deploying a new release, test that an unconfirmed transfer stays pending with its reservation held, the required
confirmation pays exactly once, a dropped payment restores its deadline, a missed webhook recovers through cron, and
an overpayment can be returned without unpaying the ticket. Include Lightning if offered. Never run the destructive
regtest races against production or an instance used by other buyers.
