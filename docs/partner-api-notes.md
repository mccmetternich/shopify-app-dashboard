# Field notes on the Shopify Partner API

Not documentation. These are behaviours observed against one app's live traffic, verified against
the `2026-07` API, and every one of them cost real debugging first. Shopify can change any of it
without telling either of us, so check anything here that matters to you rather than trusting it.

If you find one of these is now wrong, that is the most useful pull request this repository can
receive.

## Money

**`shopifyFee` is not the fee.** It is Shopify's *revenue share*, which has been 0% on the first $1M
of lifetime revenue since 2025-01-01, so it reads `$0.00` on most rows. The billing processing fee
appears **only** in the gap between `grossAmount` and `netAmount`. `gross - shopifyFee = net` is
false. Every "what Shopify took" figure here is `gross - net`.

**That deduction is not a flat rate.** Identically priced charges settle at 2.895%, 4.895% and
5.895% depending on the merchant. Read it per transaction; never calculate it from a rate you
believe to be true.

**Refunds arrive only as transactions**, never as app events. A dashboard reading only the events
feed is blind to money coming back out: `APP_SALE_ADJUSTMENT` and `APP_SALE_CREDIT` have no event
counterpart at all.

## Subscriptions

**`AppSubscription` has no billing interval.** Introspection returns amount, billingOn, id, name and
test, and nothing else. The interval has to be inferred from the price, which is what
`ANNUAL_PLAN_AMOUNTS` exists for and why getting it wrong reports an annual subscriber at twelve
times their real MRR. The one place the API states the interval outright is
`AppSubscriptionSale.billingInterval` on a transaction, which the customer detail page falls back
to.

**Shopify mints a new `AppSubscription` on a plan change** and cancels the old one. In the feed that
reads `subscribed → upgraded → unsubscribed`, usually within a day, with both briefly live. Tracking
a running total instead of a per-subscription-id map is wrong in both directions: the second
activation looks like an edit, and the trailing cancel of the superseded subscription zeroes a shop
whose replacement is live.

**An uninstall does not guarantee a cancel event.** Derivation therefore churns whatever is still
live at that point. Without that, a subscription stays live forever in every figure that reads
`subscriptions` without joining `shops`.

## Uninstalls

**`RELATIONSHIP_DEACTIVATED` is not a merchant leaving.** It is a store Shopify closed or froze. It
folds into type `uninstalled` here, but those merchants were never shown the exit survey, so any
"share who gave a reason" figure must exclude them or it understates coverage against a denominator
that could never have answered.

**Uninstall reasons arrive localised** to the merchant's admin language. The same reason arrives as
"Testing multiple apps", "Testen mehrerer Apps" or "現在アプリを使用していない".
`src/app_dashboard/uninstall_reasons.py` maps the observed strings onto canonical buckets; unknown
strings fall to "Unclassified" and are logged, rather than vanishing into "Other" where nobody would
notice a wording Shopify has just changed.

**The reason is a comma-separated pick-list**, not a single value. A merchant can choose more than
one, so bucket counts legitimately total more than the merchants they represent.

### The era boundary

Shopify made the exit question mandatory partway through 2026. There was no announcement to cite.
Coverage either side is wildly different, so pooling the two eras produces the average of two
different questions and describes neither.

`REASON_MANDATORY_FROM` splits them. Read the right date off your own feed: it is the day after your
last uninstall with an empty reason.

## Shape of the API itself

**`Transaction` is a GraphQL interface, not a union**, so `id` and `createdAt` are selectable on the
node and only per-type fields need inline fragments.

**`AppEvent` has no id of its own.** The dedupe key here is composed from
`(shop_gid, type, occurred_at, charge_gid)` because there is nothing stable to use instead.

**There is no `appInstallation` field on AppEvent types.** `shop { id }` is the durable per-merchant
key; this codebase was re-keyed onto it in migration 002 after assuming otherwise.

**The events cursor is opaque**, so an overlap window is inert on that source. Only the feeds that
take a timestamp can be re-windowed.

**It 429s readily.** Paging here is throttled to 0.3s per call.

## What it does not have at all

- **App Store listing traffic.** Views, sources, Add App clicks, listing conversion: none of it.
  This reads that from GA4 instead, which is the one part of the dashboard that is not derivable
  from Shopify.
- **Merchant country or industry.** Backfilled from a CSV if you have one; see
  [deploy.md](deploy.md).
- **Reviews.** Nothing reports who reviewed your app, which is why `shops.reviewed_at` is
  hand-maintained.
- **Anything about product usage.** The API knows a shop installed, that it pays, and that it left.
  Whether anyone ever *used* the app has to be reported by the app; see
  [usage-events-integration.md](usage-events-integration.md).
