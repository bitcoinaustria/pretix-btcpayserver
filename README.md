# pretix BTCPay Server

Accept Bitcoin payments (Lightning and on-chain) in [pretix](https://pretix.eu/) through the
[BTCPay Server](https://btcpayserver.org/) **Greenfield API**. The buyer pays on the checkout page of your own
BTCPay Server; pretix learns about it from a signed webhook, with a periodic poll as fallback.

This is a fork of [dadofsambonzuki/pretix-btcpayserver](https://github.com/dadofsambonzuki/pretix-btcpayserver),
hardened for a mainnet event (Bitcoin Zitadelle 2027). See [What this fork changes](#what-this-fork-changes) and the
[operator procedures and upgrade notes](docs/operations.md).

## Requirements

- pretix 2026.7 (tested), Python ≥ 3.11
- BTCPay Server 2.x (tested with 2.3.9) with a store that has an on-chain wallet and, optionally, Lightning
- `pretix cron` running regularly (every few minutes), as pretix recommends anyway
- PostgreSQL, as pretix recommends for production, and no pgbouncer in transaction mode in front of it: work on an
  order and the webhook registration are serialised with advisory locks (`locks.py`)
- Redis (or memcached) as pretix' cache, only if refunds are created in BTCPay (see below)

## Install

Install the reviewed checkout into the same Python environment as pretix, then rebuild and restart the web and
worker processes. Pin the commit you deploy; installing `pretix-btcpayserver` from PyPI alone does not select this fork.

```bash
python -m pip install /path/to/reviewed/pretix-btcpayserver
python -m pretix migrate
python -m pretix rebuild
# Restart your pretix web and worker services, then enable the plugin for the event.
```

For Docker, build the plugin into your custom image and run the production asset build as described in the
[pretix installation guide](https://docs.pretix.eu/self-hosting/installation/docker_smallscale/#install-a-plugin).
Use the same image for web, workers and cron. Read [upgrade notes](docs/operations.md#upgrades) before replacing an
existing payment integration: older invoices are not migrated automatically.

## Setup

1. In BTCPay, create an API key under *Account → API keys*, limited to the receiving store, with exactly:
   - required: `btcpay.store.cancreateinvoice`, `btcpay.store.canviewinvoices`, `btcpay.store.webhooks.canmodifywebhooks`
   - optional: `btcpay.store.cancreatenonapprovedpullpayments`, only if refunds should be created in BTCPay
     (see step 2)

   Any other permission, or one for another store, is refused. `btcpay.store.cancreatepullpayments` in particular:
   BTCPay's refund endpoint takes any amount and would approve some claims on its own, so a leaked key could pay
   itself out.
2. Enable the plugin in pretix and fill in *Settings → Payment → BTCPay Server*: URL (https), API key, store ID,
   the number of confirmations (1, 2 or 6; zero is not offered), the invoice expiration and whether refunds are
   created in BTCPay. That is off by default: the team then refunds by hand in BTCPay and records it in pretix.
   Switched on, the key needs the optional permission, and every refund still waits for someone to approve it.
3. Saving checks the key and registers the webhook on the store with a secret the plugin generates. There is one
   webhook per pretix event: `https://<pretix>/<organizer>/<event>/btcpay/webhook/`. Buyers never register it; without
   a webhook, configured invoices are eligible for polling after a minute; actual recovery depends on cron, queue
   delay and backlog. A new store or URL gets its webhook staged and only switched to once the settings are saved,
   so a form that fails elsewhere keeps the working webhook.
4. In the BTCPay store, check invoice monitoring separately from invoice expiration. This plugin overrides invoice
   expiration, confirmation policy and payment tolerance for each new invoice; it inherits monitoring from the store.
   Keep a monitoring period suitable for slow confirmations (the BTCPay 2.3.9 default is 24 hours). Check a real
   invoice's `monitoringExpiration`, and keep cron and webhooks working. See [store settings](docs/operations.md#store-settings).
5. Leave pretix’s *Generate tickets for pending orders* off to release tickets only after payment. Place a small
   test-mode order. An unconfirmed on-chain transfer must leave the payment pending and the order
   unpaid, with no paid ticket. After the required confirmations, the order must become paid. Check Lightning
   separately if enabled; it settles immediately. Check webhook deliveries and a successful fallback poll before sales.

## How payments work

| BTCPay invoice | pretix payment | order |
|---|---|---|
| `New` (also partially paid) | created | pending until its payment deadline |
| `Processing`: fully paid, not yet confirmed | pending | unpaid; held until `monitoringExpiration` + 1 hour |
| `Settled` (also `PaidOver`, `Marked`) | confirmed, also after it failed or was cancelled; money beyond the amount becomes its own confirmed payment | paid (overpaid) if the current total is covered and quota permits |
| `Expired` (also `PaidPartial`, `PaidLate`) | failed | back to its old payment deadline |
| `Invalid` | failed; a confirmed payment is only flagged | back to its old payment deadline |

- Unconfirmed on-chain funds never pay an order: `Processing` means pending, with seats held until the invoice's
  `monitoringExpiration` plus one hour as the hold target. Only `Settled` confirms the payment. Reconciliation refuses `HighSpeed`,
  unknown or missing speed policies. Lightning settles immediately. An admin's “Mark settled” is trusted; verify
  funds before using it.
- The webhook payload is only used for the invoice id: the plugin reads the invoice back from the API and checks
  store, event, order, payment, amount and currency before it acts. A replayed, reordered or forged delivery can
  therefore not change anything the invoice itself does not say.
- Invoice expiration is capped by the configured minutes (default 15) and the remaining order payment deadline,
  with BTCPay's minimum of one minute. The exchange rate is fixed while it runs; after the order deadline seats may
  be sold again.
  The invoice asks for the configured speed policy and a payment tolerance of 0.
- Each payment has exactly one invoice, created under a row lock in `execute_payment`. A payment whose invoice ran
  out is not continued: pretix starts a new payment with a new invoice. If a payment that the network has seen
  becomes underpaid again (a replacement transaction returned the invoice to New), the buyer can complete it on
  the same invoice.
- A buyer who switches the payment method gets a new payment and invoice; the old invoice is left to run out (not
  invalidated in BTCPay, which could hit a payment arriving in the same second). If money still arrives on it and
  BTCPay settles it (or an admin marks it settled), that payment counts, and other open BTCPay payments of the now
  paid order are cancelled. Money is never silently dropped.
- Money beyond the invoice amount (an over payment, or a second transfer to a settled invoice) is booked as its own
  confirmed payment once every non-invalid receipt on the invoice is confirmed (BTCPay's `paidAmount` also counts unconfirmed
  ones), so pretix shows the order as overpaid and refunding it leaves the ticket paid. This also finishes an order
  whose total was raised after the invoice was created, if the money now covers it.
- A cancelled order stays cancelled even if a former invoice settles. The money is recorded as a confirmed payment;
  return it if the order will not be fulfilled.
- If pretix confirmed a payment but could not mark the order paid (a lock timeout), the next webhook or poll
  finishes it; if the seats are really gone, the order is flagged instead. The order row is locked while the
  balance is checked and the order marked paid, so an edit that changes the total cannot slip in between.
- Partial and late payments, and money on an expired or invalid invoice, are logged on the order ("Needs attention"
  in the payment details) for the team to handle in BTCPay. Refunds create a BTCPay pull payment the buyer claims with
  their own address and someone approves in BTCPay (only if refunds are switched on). One refund per payment goes
  through BTCPay: it is noted in the shared cache (atomically, so of two at once only one gets through) before the
  request, and the note stays, because pretix runs refunds inside a transaction that a crash can roll back after
  BTCPay created the claim. A refund without a clear answer is marked for a human; any further refund of the same
  payment is refused with the name of the earlier one, to be checked and done by hand in BTCPay. The ticket and each
  surplus are separate payments, so both can be refunded. The note lives only in the cache: Redis should persist.
- Webhook, poll and checkout reconciliation work on an order one at a time, under a PostgreSQL advisory lock that
  lasts until the surrounding transaction commits and never runs out. Finalization also locks the order and its
  payment rows across the balance check. Both kinds of lock are only tried, never waited for in the database:
  pretix's own order edits and payment-method switches take rows in different orders. A webhook that finds the
  order busy gets a 503 and BTCPay delivers it again; the poll skips it and retries. A busy advisory lock is checked
  before asking BTCPay, so it costs no API request. Row contention found after reading the invoice is also retried.
- A dropped or never-confirmed payment fails when BTCPay reports `Invalid` or `Expired`; the original deadline is
  restored (at least ten minutes from release), unless another payment still needs the hold. A missing webhook is
  recovered by polling. The last invoice to close restores the recorded deadline, even if another invoice
  originally extended the order. The markers are cleared when the hold is released or the order is paid, so a later
  payment starts a new hold from the current deadline.
- The poll asks about invoices that are due, longest unchecked first, at most 300 per run, and starts no new one
  after 45 seconds (one that is running finishes; the next run continues): open ones every minute, settled or
  closed ones pretix has not caught up with every few minutes, the rest every half hour while BTCPay still watches
  them or money beyond the amount is not booked yet, and uncounted closed ones every six hours for half a year.
  Closed invoices with an unreleased reservation are checked every minute until it can be released.
  Refunded and failed payments are included, and it keeps running when the payment method is disabled. A settled
  invoice pretix has not counted, or whose order it has not marked paid, stays in the poll however old it is. It
  loads the plugin's payments of that half year each run, which is fine for thousands of payments, not for millions.
- Every note for the team ("Needs attention") is logged again when the amount on the invoice changes, so money that
  arrives after a refund or on a closed invoice is never silent.
- After an RBF replacement reduces a previously full payment, BTCPay can return the invoice from `Processing` to
  `New`. pretix keeps the payment pending, refreshes the buyer page, and offers the same invoice to complete the
  missing amount. No new invoice is needed.
- A second invoice paid for an already covered order is confirmed too, logged as a double payment and shown as
  overpayment in pretix. Refund the excess; replaying the webhook does not add another confirmation or note.
- The pending page polls a status endpoint that needs the order secret and only reads the database.

## Known limitations

- The control-panel invoice display caches successful API reads for 30 seconds, scoped by event, server, store and
  invoice. A cold cache or outage still performs a synchronous request (5-second connect and 15-second read timeouts);
  failures are not cached. Rendering these details never confirms a payment. Buyer status polling reads only pretix.

Accepted for the Bitcoin Zitadelle, and worth knowing before you rely on the plugin:

- A counted payment is no longer polled once BTCPay stopped watching its invoice (a day by default) and two days
  have passed: after that only an admin could change the invoice in BTCPay, and for a counted payment that would
  only produce a note. The webhook is then the only signal.
- Money that arrives after a refund, or on an expired or invalid invoice, is logged for a human, not booked.
- Amounts are handled in the event currency to the cent; an event priced in BTC would need other rounding.
- Cancelling inside pretix' own transaction (switching the payment method, changing or cancelling an order) decides
  on the last state the webhook or poll saw, without asking BTCPay; money that still arrives confirms the cancelled
  payment.
- With refunds switched on, the note that stops a second claim lives in the shared cache: Redis should persist its
  data, or a crash right after a claim plus a lost cache could let a retry create a second, still unapproved, claim.
- The poll's 45 seconds are a target for starting checks, not a hard limit: a check that has started finishes (two
  BTCPay requests at most, each with a 15 second read timeout).
- A payment counts once BTCPay settles its invoice. If a block reorganization then drops the confirming transaction
  and a conflicting one confirms instead, BTCPay keeps reporting the invoice as `Settled`, and the order stays paid
  without a note. With one confirmation (`MediumSpeed`) that takes a reorg plus a double spend; ask for 2 or 6
  confirmations where that risk matters more than the wait.
- The settings form blocks a BTCPay server or store change while any payment of the event still needs its invoice
  (open, not booked, extra money not booked, order not finalized), also with the payment method disabled. A change
  and invoice creation are serialized, so no invoice is created with the old connection after the check. Equivalent
  server addresses and same-store key rotation are allowed. Each invoice remembers where it was created; one from
  another server/store is noted once and skipped without asking the new store; see
  [store changes](docs/operations.md#changing-the-server-or-store).
- Only the current invoice of each payment is reconciled. Payments from upstream or from earlier commits of this fork
  that kept several invoices per payment would need a migration first; the Bitcoin Zitadelle starts on a fresh install.

## Security notes

- Webhooks: `POST` only, at most 64 KiB, HMAC-SHA256 over the raw body (`BTCPay-Sig`), constant-time compare,
  before anything is parsed. The secret is generated by the plugin and set on create and update, so the webhook
  never has to be deleted and recreated. Registration runs only when the settings are saved, under an advisory lock
  held until the settings are committed, holds no row while talking to BTCPay, and a failed one keeps the working
  webhook. A staged webhook becomes active under the same lock, compared against the settings in the database.
- `Settled` with `Marked` means an admin decided in BTCPay; the plugin trusts that decision.
- The plugin only redirects to the configured BTCPay host and requires https for it (except localhost).
- The API key is stored like other pretix payment secrets. No personal data is sent to BTCPay: the invoice metadata
  holds the order code, payment id and event.

## Development and tests

```bash
python -m unittest discover -s tests -t .                  # the state machine and the security checks, without pretix
docker exec -i <pretix container> pretix shell < tests/pretix_checks.py   # sync against a real pretix, fake invoices
docker exec -i -e CHECK_EVENT=<organizer>/<test-event> <pretix container> pretix shell < tests/pretix_regressions.py
docker exec -i -e CHECK_EVENT=<organizer>/<test-event> <pretix container> pretix shell < tests/pretix_surface.py
```

All three live scripts require a test-mode event and PostgreSQL. Run `pretix_checks.py` only on a dedicated disposable
instance: it creates more than 500 orders, exercises settings writes (restored afterwards), and mocks the global poll.
The regression checks use fake invoices, create temporary test orders and delete them afterwards, without changing
payment settings or contacting BTCPay. They cover concurrent
payment-method switching, row contention, balance protection, quota failures, surplus accounting and reservation
cleanup when several invoices are in flight.
The surface checks use fake API responses and roll back temporary settings and orders. They cover webhook setup,
input validation, authenticated status reads, cached display and the server/store change guard. The surplus regression
currently fails because that guard permits a switch before an already confirmed invoice's extra money is booked.

End-to-end tests on regtest (on-chain, Lightning, expiry, partial, over and late payments, double spends,
switched payment methods, lost webhooks, forged webhooks, refunds, a rush of buyers) live in the Bitcoin Zitadelle
website repository (`scripts/pretix-e2e-bitcoin.mjs`, run with `PRETIX_BTCPAY=greenfield`).

## What this fork changes

Compared with upstream 0.1.1:

- Expired and invalid invoices fail their payment (upstream ignored every event but `InvoiceSettled`).
- The invoice state is always read from the API, never taken from the webhook payload, and checked against the payment.
- Settled invoices of failed or cancelled payments still confirm them, instead of being dropped with a 200.
- Over payments and second transfers become their own confirmed payments; an interrupted confirmation is finished.
- A periodic task polls invoices that may still change, so a lost webhook does not leave a paid order unpaid.
- The webhook is registered when the settings are saved, with our own secret, under a lock; not lazily at the first
  sale, where concurrent buyers could end up with a secret that belongs to a deleted webhook.
- API keys are checked against an allowlist: three required store permissions, one optional for refunds, nothing else.
- Unconfirmed payments keep the order reserved, and a dropped one gives the seats back unless another payment
  still needs the reservation (one on its way in pretix, or one whose invoice has money on the way in BTCPay).
- Invoices use the remaining order payment window (minimum one minute), explicit speed policy and zero payment tolerance.
- Invoices are only created in `execute_payment`; emails link to pretix' own payment page instead of creating
  invoices while rendering.
- The status endpoint needs the order secret and does not call BTCPay.
- Absolute redirect URL back to the order, links only to the configured BTCPay host.
- Refunds via pull payments that someone approves, off by default; cancel handling, German translations (formal and
  informal), tests.
