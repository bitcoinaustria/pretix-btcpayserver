# pretix BTCPay Server

Accept Bitcoin payments (Lightning and on-chain) in [pretix](https://pretix.eu/) through the
[BTCPay Server](https://btcpayserver.org/) **Greenfield API**. The buyer pays on the checkout page of your own
BTCPay Server; pretix learns about it from a signed webhook, with a periodic poll as fallback.

This is a fork of [dadofsambonzuki/pretix-btcpayserver](https://github.com/dadofsambonzuki/pretix-btcpayserver),
hardened for a mainnet event (Bitcoin Zitadelle 2027). See [What this fork changes](#what-this-fork-changes).

## Requirements

- pretix 2026.7 (tested), Python ≥ 3.11
- BTCPay Server 2.x (tested with 2.3.9) with a store that has an on-chain wallet and, optionally, Lightning
- `pretix cron` running regularly (every few minutes), as pretix recommends anyway
- PostgreSQL, as pretix recommends for production, and no pgbouncer in transaction mode in front of it: work on an
  order and the webhook registration are serialised with advisory locks (`locks.py`)
- Redis (or memcached) as pretix' cache, only if refunds are created in BTCPay (see below)

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
   a webhook the poll still finds every payment, a minute later. A new store or URL gets its webhook staged and only
   switched to once the settings are saved, so a form that fails elsewhere keeps the working webhook.

## How payments work

| BTCPay invoice | pretix payment | order |
|---|---|---|
| `New` (also partially paid) | created | pending until its payment deadline |
| `Processing`: fully paid, not yet confirmed | pending | reserved while BTCPay monitors the transaction (default a day) |
| `Settled` (also `PaidOver`, `Marked`) | confirmed, also after it failed or was cancelled; money beyond the amount becomes its own confirmed payment | paid (overpaid) |
| `Expired` (also `PaidPartial`, `PaidLate`) | failed | back to its old payment deadline |
| `Invalid` | failed; a confirmed payment is only flagged | back to its old payment deadline |

- The webhook payload is only used for the invoice id: the plugin reads the invoice back from the API and checks
  store, event, order, payment, amount and currency before it acts. A replayed, reordered or forged delivery can
  therefore not change anything the invoice itself does not say.
- Every invoice runs until the order's payment deadline at the latest, and at most the configured minutes
  (default 15): the exchange rate is fixed while it runs, and after the deadline the seats may be sold again.
  The invoice asks for the configured speed policy and a payment tolerance of 0.
- Each payment has exactly one invoice, created under a row lock in `execute_payment`. A payment whose invoice ran
  out is not continued: pretix starts a new payment with a new invoice.
- A buyer who switches the payment method gets a new payment and invoice; the old invoice is left to run out (not
  invalidated in BTCPay, which could hit a payment arriving in the same second). If money still arrives on it and
  BTCPay settles it (or an admin marks it settled), that payment counts, and other open BTCPay payments of the now
  paid order are cancelled. Money is never silently dropped.
- Money beyond the invoice amount (an over payment, or a second transfer to a settled invoice) is booked as its own
  confirmed payment once every payment on the invoice is confirmed (BTCPay's `paidAmount` also counts unconfirmed
  ones), so pretix shows the order as overpaid and refunding it leaves the ticket paid.
- If pretix confirmed a payment but could not mark the order paid (a lock timeout), the next webhook or poll
  finishes it; if the seats are really gone, the order is flagged instead.
- Partial and late payments, and money on an expired or invalid invoice, are logged on the order ("Needs attention"
  in the payment details) for the team to handle in BTCPay. Refunds create a BTCPay pull payment the buyer claims with
  their own address and someone approves in BTCPay (only if refunds are switched on). One refund per payment goes
  through BTCPay: it is noted in the shared cache (atomically, so of two at once only one gets through) before the
  request, and the note stays, because pretix runs refunds inside a transaction that a crash can roll back after
  BTCPay created the claim. A refund without a clear answer is marked for a human; any further refund of the same
  payment is refused with the name of the earlier one, to be checked and done by hand in BTCPay. The ticket and each
  surplus are separate payments, so both can be refunded. The note lives only in the cache: Redis should persist.
- Webhook, poll, checkout and cancelling work on an order one at a time, under a PostgreSQL advisory lock that lasts
  until the surrounding transaction commits (cancelling runs inside pretix' own transaction) and never runs out. It
  is only ever tried, never waited for in the database, so it cannot deadlock with pretix' row and quota locks. A
  webhook that finds the order busy gets a 503 and BTCPay delivers it again; the poll takes it next time.
- The poll asks about invoices that are due, longest unchecked first, at most 300 per run, and starts no new one
  after 45 seconds (one that is running finishes; the next run continues): open ones every minute, settled or
  closed ones pretix has not caught up with every few minutes, the rest every half hour while BTCPay still watches
  them or money beyond the amount is not booked yet, and uncounted closed ones every six hours for half a year.
  Refunded and failed payments are included, and it keeps running when the payment method is disabled. A settled
  invoice pretix has not counted, or whose order it has not marked paid, stays in the poll however old it is. It
  loads the plugin's payments of that half year each run, which is fine for thousands of payments, not for millions.
- Every note for the team ("Needs attention") is logged again when the amount on the invoice changes, so money that
  arrives after a refund or on a closed invoice is never silent.
- The pending page polls a status endpoint that needs the order secret and only reads the database.

## Known limitations

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
```

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
- Unconfirmed payments keep the order reserved, and a dropped one gives the seats back.
- Invoices expire with the order at the latest, with explicit speed policy and zero payment tolerance.
- Invoices are only created in `execute_payment`; emails link to pretix' own payment page instead of creating
  invoices while rendering.
- The status endpoint needs the order secret and does not call BTCPay.
- Absolute redirect URL back to the order, links only to the configured BTCPay host.
- Refunds via pull payments that someone approves, off by default; cancel handling, German translations (formal and
  informal), tests.
