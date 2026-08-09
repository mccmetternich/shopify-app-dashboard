# Configuration

Everything is environment variables. [`.env.example`](../.env.example) is the complete list with
notes on each; this page covers only the ones that are easy to get wrong.

Settings are validated at import, which for this app means at process start, so a bad value is a
process that refuses to boot rather than a dashboard that quietly reports the wrong number for a
week.

## Required, with no default

`DATABASE_URL`, `PARTNER_API_TOKEN`, `PARTNER_ORG_ID`, `PARTNER_APP_ID`, `DASHBOARD_USERS`,
`PUBLIC_BASE_URL`, `GOOGLE_ALLOWED_DOMAINS`.

The last two have no default deliberately. A default there would point every deployment at whoever
published it and admit their staff, which is a standing back door rather than a convenience.

Create the Partner API token at `partners.shopify.com/<your-org-id>/settings/partner_api_clients`.
The slug is `partner_api_clients`, not `api_clients`, and your org id is the number in that URL.

## The three that decide whether the numbers are right

### `ANNUAL_PLAN_AMOUNTS`

`AppSubscription` carries no billing-interval field, so annual plans are recognised **by price**.
List every annual price you charge, with cents, comma separated: `190.00,490.00`.

An annual price missing from that list is treated as monthly and counted at **twelve times** its
true MRR, on every page, with nothing anywhere to say so. Empty, which is the default, means every
plan is monthly; the app logs a warning at startup when it is.

Changing it later does not repair charges already stored. Reset the sync cursor and replay; see
[deploy.md](deploy.md).

The way to confirm a price is annual is its charges: `billingOn` lands ~370 days after activation
rather than ~30.

### `USAGE_EVENT_TYPES`

The event names `POST /ingest/usage` accepts. Anything outside the list is rejected rather than
stored, which is deliberate: the endpoint is the one route an external caller reaches.
`USAGE_ACTIVATION_EVENT` and `USAGE_LIVE_EVENT` must both appear in it, and the app refuses to start
if they do not.

The defaults use "offer" nouns because that is what the app this was built for does. Rename them to
whatever yours does. See [usage-events-integration.md](usage-events-integration.md).

### `TRUSTED_CLIENT_IP_HEADER`

The header your proxy puts the real client address in. Rate limiting keys on it, so leaving it wrong
collapses every caller into one bucket and limits nothing.

Prefer a single-value header your proxy **overwrites**: `Fly-Client-IP`, `CF-Connecting-IP`,
`X-Real-IP`. `X-Forwarded-For` works, but proxies *append* to it, so only its rightmost entry is
trustworthy and that is the one read. Everything to its left was written by the client and can say
anything.

Empty means trust the socket peer, which is correct only with nothing in front of the app.

## Ones that move more than they look like they move

`POLL_INTERVAL_MINUTES` is not only the sync cadence. The ops-strip red threshold and the stale-sync
Slack alert are multiples of it, 3 and 4 polls respectively, so raising it moves both.

`SESSION_SECRET` signs the session cookie, so rotating it signs everyone out. The app refuses to
serve a non-local deployment while it is still the published default, and requires at least 32
characters.

`REASON_MANDATORY_FROM`, `ANNOTATIONS_EARLIEST` and `GA4_EARLIEST_DATA` are all dates derived from
one dataset. Read your own off your own feed rather than inheriting these.

## Optional integrations

Google OAuth (`GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`) for sign-in, a GA4 property
(`GA4_PROPERTY_ID` / `GA4_CREDENTIALS_JSON`) for App Store listing traffic, `SLACK_WEBHOOK_URL` for
stale-sync alerts and the weekly digest, and `USAGE_INGEST_TOKEN` for `POST /ingest/usage`. Each is
inert when unset: the Traffic page does not render without a GA4 property, and the ingest endpoint
refuses everything without a token.
