# Shopify App Dashboard

Self-hosted analytics for your own Shopify app. It polls the Shopify Partner API and derives
installs, uninstalls with reasons, MRR and what moved it, collected revenue, cohort retention,
churn, and activation.

[![CI](https://github.com/kgelster/shopify-app-dashboard/actions/workflows/ci.yml/badge.svg)](https://github.com/kgelster/shopify-app-dashboard/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.13](https://img.shields.io/badge/python-3.13-blue.svg)](https://www.python.org/downloads/)

> ## Unofficial. Not a Shopify product.
>
> This is an independent project by an app developer, for app developers. It is **not affiliated
> with, endorsed by, sponsored by, or connected to Shopify Inc.** Nobody at Shopify has reviewed it.
> "Shopify" appears in the name only to say which API it reads, in the sense the law calls
> nominative use. Shopify, Shopify Partners and the Shopify App Store are trademarks of Shopify Inc.

![The Overview page](docs/screenshots/overview.png)

## It is in use on a real app

This is the dashboard behind [Promo Party](https://apps.shopify.com/promo-party-fgwp), a live app in
the Shopify App Store, and it is the one its developers actually open. One Fly machine, one
Postgres, polling the Partner API every 15 minutes, serving a real hostname since August 2026, when
[Mantle](https://docs.heymantle.com/wind-down) shut down and the replacement had to exist.

**The screenshots are not that app.** They come from `scripts/seed_demo.py`, which invents an app
called Demo App and about two hundred fictional merchants. No real merchant, domain, contact or
revenue figure appears anywhere in this repository, in the screenshots, or in the git history.

## What it gives you

- **Overview** with prior-period comparison on every tile, an MRR movements waterfall
  (new / expansion / contraction / churn), and an ops health strip.
- **Customers**, filterable and paginated, plus a per-merchant detail page with the full lifecycle
  timeline, payment history, and product usage.
- **Churn** with Shopify's uninstall reasons normalised across languages, and free-text verbatims.
- **Retention** by install and subscription cohort.
- **Funnel**, including activation if your app posts usage events.
- **Actions**: three call sheets (merchants worth asking for a review, monthly subscribers worth
  pitching the annual plan, recent installs that have not subscribed).
- **Traffic** from GA4: listing sessions, Add App clicks, installs, by channel, source and country.
- **A `.md` twin of every page** with a Copy button, and `/export.json` for the whole thing at once.
- **Slack** stale-sync alerts and a weekly digest.

## Install

Requires Python 3.13, Postgres, and a Shopify Partner API token.

### By hand

```bash
uv sync
cp .env.example .env        # fill in real values, never commit this file
createdb app_dashboard
uv run python -m app_dashboard.migrate
uv run uvicorn app_dashboard.web:app --reload
```

Read [docs/configuration.md](docs/configuration.md) before the first run. Three settings decide
whether the numbers are right, and `ANNUAL_PLAN_AMOUNTS` in particular will silently report annual
subscribers at twelve times their real MRR if you leave it unset.

The first sync replays your app's full history from the events feed, so there is no historical
import to arrange.

### With Claude Code

<details>
<summary>A setup prompt that asks instead of guessing (click to expand)</summary>

Paste this into [Claude Code](https://claude.com/claude-code) from the directory you want it in. It
asks you for the things it cannot know instead of inventing them, which for this app is the
difference between a correct dashboard and a confidently wrong one:

```text
Set up https://github.com/kgelster/shopify-app-dashboard for my Shopify app.

Clone it, then read README.md and docs/configuration.md first.

Work through this in order. Stop and ask me rather than guessing at any
value:

1. `uv sync`, then create the Postgres database and tell me the
   DATABASE_URL you used.
2. Ask me for my Partner org id and app id, then walk me through creating a
   Partner API token at
   partners.shopify.com/<org-id>/settings/partner_api_clients
   (that slug, not api_clients) and wait for me to paste it back.
3. Ask me every price I charge, including annual ones with cents, then set
   ANNUAL_PLAN_AMOUNTS to just the annual ones. Explain what happens if it
   is wrong before you write it.
4. Ask me what my app's core "the merchant actually used it" action is,
   and set USAGE_EVENT_TYPES, USAGE_ACTIVATION_EVENT and USAGE_LIVE_EVENT
   from that answer.
5. Write .env from .env.example, generate a SESSION_SECRET, and confirm
   .env is gitignored. Never commit it, never print the token back to me.
6. Run the migrations, start the app, and tell me when the first sync is
   done. It replays my app's full history, so it takes a few minutes.
7. Run scripts/check_invariants.py and show me the output. If anything
   fails, stop and explain which invariant and what it means before
   touching anything.

Then tell me which numbers on the Overview page you would not trust yet,
and why.
```

</details>

### Without a Partner token

`scripts/seed_demo.py` builds the synthetic dataset in the screenshots on this page, so you can
see the whole thing before deciding whether to wire up your own app. It TRUNCATES every table it
seeds, so point it at a throwaway database:

```bash
createdb app_dashboard_demo
DATABASE_URL=postgresql://localhost:5432/app_dashboard_demo \
PARTNER_API_TOKEN=unused PARTNER_ORG_ID=0 PARTNER_APP_ID=0 \
DASHBOARD_USERS=demo:demo-only-not-a-password \
PUBLIC_BASE_URL=http://localhost:8000 GOOGLE_ALLOWED_DOMAINS=example.com \
ANNUAL_PLAN_AMOUNTS=190.00 NO_SCHEDULER=1 \
  uv run python scripts/seed_demo.py --yes
```

## What it looks like

All synthetic, from `scripts/seed_demo.py`.

### Churn

Shopify serves the uninstall pick-list in the merchant's own admin language, so the same reason
arrives as "Testing multiple apps", "Testen mehrerer Apps" and "現在アプリを使用していない".
Grouping on the raw string gives you a long tail of one-off bars that says nothing. These are
bucketed, and the two things that would otherwise quietly corrupt the percentage are called out on
the page: stores Shopify closed (never shown the survey, so excluded) and the date the question
became mandatory (before and after are different questions, so they are not pooled).

![The Churn page](docs/screenshots/churn.png)

### Retention

Install cohorts and subscription cohorts, separately. A merchant who keeps paying and a merchant who
keeps the app installed are different retention stories, and an app with a free tier can look
healthy on one while dying on the other.

![Retention cohorts](docs/screenshots/retention.png)

### One merchant

The whole lifecycle in one place. This one shows the trap that costs the most money: Shopify does
not edit a subscription when a merchant changes plan, it activates a **new** `AppSubscription` and
cancels the old one, which is why the timeline reads "Upgraded" and "Subscription ended" on the same
day. It also shows an annual plan normalised to `$15.83 a month in MRR`, and what Shopify actually
kept out of each charge, measured per transaction rather than assumed to be 2.9%.

![A single merchant's detail page](docs/screenshots/customer.png)

### Funnel and activation

The Partner API knows lifecycle only: installed, paid, left. Whether anyone ever *used* the app is
invisible from that side, so activation is reported by the app itself through `POST /ingest/usage`.
Until events arrive it reads unknown, never 0%.

![The Funnel page](docs/screenshots/funnel.png)

### Actions

Three call sheets, recomputed on every page load. Nothing here writes anywhere.

![The Actions page](docs/screenshots/actions.png)

### Traffic

The one thing the Partner API exposes nothing about: App Store listing traffic. This reads it from
GA4 and reconciles listing installs against the installs in the events feed, which never quite
agree.

![The Traffic page](docs/screenshots/traffic.png)

### Customers, and the sign-in page

![The Customers list](docs/screenshots/customers.png)

Sign-in is Google OAuth restricted to an allowlist you configure, with HTTP Basic as the fallback
for whoever set it up. The allowlist is re-checked on every request, so removing an address locks
out the cookie it already issued.

![The sign-in page](docs/screenshots/signin.png)

## Documentation

| | |
|---|---|
| [docs/configuration.md](docs/configuration.md) | Every setting that can make the numbers wrong. Read before the first run. |
| [docs/partner-api-notes.md](docs/partner-api-notes.md) | What the Partner API actually does. Field notes, each one paid for. |
| [docs/architecture.md](docs/architecture.md) | The pipeline, which table is the truth for which number, and the traps. Read before changing `derive.py` or `stats.py`. |
| [docs/deploy.md](docs/deploy.md) | Secrets, deploy, verification, forcing a replay, backfills. |
| [docs/exports.md](docs/exports.md) | The `.md` twin of every page, and `/export.json`. |
| [docs/usage-events-integration.md](docs/usage-events-integration.md) | The `POST /ingest/usage` contract. Hand this to whoever writes your app. |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Running the tests, and what is likely to be merged. |

## Scope

This is what we run for our own app. It was built because Mantle shut down and the replacement had
to exist; publishing it costs nothing and it may save someone else the same build. It is
deliberately one Partner org, one app, one Postgres, one machine, with opinions where a product
would have settings. It works; it is not a product.

**No support is promised, and that is not modesty.** Issues and pull requests are welcome and may
sit for a while. Nobody is on call. There is no roadmap and no deprecation policy. If you need
something to depend on, fork it: MIT, and the fork is yours.

What would actually help, in rough order: a new uninstall-reason string Shopify has started serving
(`src/app_dashboard/uninstall_reasons.py`, and unmapped ones are logged rather than hidden, so
`grep` your logs), a Partner API behaviour that contradicts
[docs/partner-api-notes.md](docs/partner-api-notes.md), and a bug with a failing test.

## License and trademarks

MIT. See [LICENSE](LICENSE).

Shopify, Shopify Partners, and the Shopify App Store are trademarks of Shopify Inc. This project is
not affiliated with, endorsed by, or sponsored by Shopify Inc., and the MIT licence covers this code
only, never anyone's trademarks. If you fork this and put a name on it, that name is your problem to
get right.
