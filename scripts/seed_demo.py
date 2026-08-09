"""Fill a database with a synthetic app's history, so the dashboard can be run
without a Partner API token.

Every merchant here is invented. Domains are prefixed `demo-` so a screenshot or
a shared screen can never be mistaken for somebody's real install base, and the
uninstall reasons are Shopify's own pick-list strings (including the localised
ones) so the normaliser has something real to normalise.

It writes through the same functions the live pipeline uses -- upsert_raw_events,
upsert_charges, upsert_transactions, then derive_installation -- rather than
inserting derived rows directly. A seeder that wrote `subscriptions` itself could
produce a dashboard the real code path cannot, which would make it a liar.

    createdb app_dashboard_demo
    DATABASE_URL=postgresql://localhost:5432/app_dashboard_demo \
    PARTNER_API_TOKEN=unused PARTNER_ORG_ID=0 PARTNER_APP_ID=0 \
    DASHBOARD_USERS=demo:demo-only-not-a-password \
    PUBLIC_BASE_URL=http://localhost:8000 GOOGLE_ALLOWED_DOMAINS=example.com \
    ANNUAL_PLAN_AMOUNTS=190.00 NO_SCHEDULER=1 \
      uv run python scripts/seed_demo.py --yes

It TRUNCATES every table it seeds, so it refuses to run without --yes and prints
the database it is about to overwrite first.
"""

import argparse
import random
import re
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import urlparse

from app_dashboard.config import get_settings
from app_dashboard.db import connect, run_migrations
from app_dashboard.derive import derive_installation
from app_dashboard.ingest_raw import upsert_charges, upsert_raw_events, upsert_transactions
from app_dashboard.uninstall_reasons import classify

# Fixed so two runs produce the same dashboard: a screenshot in the README
# should still match the thing it documents a month later.
RNG = random.Random(20260809)

MONTHS_OF_HISTORY = 22
SHOP_COUNT = 190

# Plans. The annual one has to appear in ANNUAL_PLAN_AMOUNTS or it is counted as
# monthly, at twelve times its true MRR -- the failure this seeder is also a
# demonstration of.
MONTHLY = Decimal("19.00")
PLUS = Decimal("49.00")
ANNUAL = Decimal("190.00")

# Shopify's billing processing fee is not a flat rate: identically priced charges
# settle differently per merchant. Each shop draws one and keeps it.
FEE_RATES = [Decimal("0.02895"), Decimal("0.04895"), Decimal("0.05895")]

FIRST = [
    "North Loop", "Harbour", "Copperline", "Fieldnote", "Wolf & Kin", "Ninth Street",
    "Tallgrass", "Bright Anchor", "Cedar Fork", "Lantern", "Quiet Coast", "Ridgeway",
    "Marlow", "Saltbox", "Pinehurst", "Ember", "Foxglove", "Granite Bay", "Halyard",
    "Junco", "Kestrel", "Longwater", "Meridian", "Nightjar", "Overland", "Prairie",
    "Quarry", "Redwing", "Stonecrop", "Thistle", "Umber", "Verdant", "Wayfare",
    "Yarrow", "Zephyr", "Blue Heron", "Clearwater", "Driftwood", "Elmsworth",
]
SECOND = [
    "Supply", "Goods", "Works", "Trading Co", "Provisions", "Mercantile", "Studio",
    "Outfitters", "Collective", "Apothecary", "Bakehouse", "Coffee", "Cyclery",
    "Home", "Kitchen", "Paper", "Press", "Roasters", "Textiles", "Woodshop",
]

COUNTRIES = (
    ["US"] * 46 + ["GB"] * 12 + ["CA"] * 10 + ["AU"] * 8 + ["DE"] * 5 + ["NL"] * 3
    + ["FR"] * 3 + ["SE"] * 2 + ["JP"] * 2 + ["BR"] * 2 + ["DK"] * 2 + ["IE", "NZ", "SG"]
)
INDUSTRIES = [
    "Apparel & Accessories", "Home & Garden", "Health & Beauty", "Food & Drink",
    "Sporting Goods", "Toys & Games", "Electronics", "Arts & Crafts", "Pet Supplies",
]

# Shopify's own wording, in the languages it serves the pick-list in. Weighted so
# the chart has a shape rather than nine equal bars.
REASONS = (
    ["Not using app now"] * 14
    + ["App wird derzeit nicht genutzt", "現在アプリを使用していない", "Bruger ikke appen i øjeblikket"]
    + ["Testing multiple apps"] * 9
    + ["Testen mehrerer Apps"]
    + ["Limited or missing features"] * 8
    + ["Begrænsede eller manglende funktioner"]
    + ["Not working properly with store"] * 6
    + ["Not working or compatible with store"] * 3
    + ["Too expensive"] * 6
    + ["Store is closing or pausing"] * 4
    + ["Hard to set up or use"] * 3
    + ["Not satisfied with app features"] * 3
    + ["アプリの機能に満足できなかった"]
    + ["Not satisfied with support"] * 2
    + ["Other (please specify)"] * 4
    # An unmapped string, on purpose: the point of the Unclassified bucket is
    # that a wording Shopify has not shown us yet stays visible.
    + ["Switching to a different solution"]
)

# Free text, keyed by the bucket the reason lands in, so a verbatim never
# contradicts the reason it is filed under. A demo dataset that files "the store
# is closing" under "too expensive" teaches the reader to distrust the page.
VERBATIMS = {
    "Not using app now": [
        "Didn't have the volume to justify it yet, will be back for Q4.",
        "Seasonal store, we reinstall every October.",
        "Ran one campaign and haven't needed it since.",
    ],
    "Too expensive": [
        "Trialling three of these at once and yours was the most expensive.",
        "Fine at $19, not at $49 for the volume we do.",
        "Cheaper to build it into the theme ourselves.",
    ],
    "Limited or missing features": [
        "Needed multi-currency and it wasn't there.",
        "No way to schedule an offer to end at midnight.",
        "We needed per-collection rules, not per-product.",
    ],
    "Not working with store": [
        "Couldn't get it to show on the cart drawer with our theme.",
        "Conflicted with our bundles app, both tried to edit the cart.",
        "Broke on mobile after the last theme update.",
    ],
    "Store closing or pausing": [
        "Store is closing, nothing to do with the app.",
        "Pausing the business until spring.",
    ],
    "Hard to set up or use": [
        "Too many clicks to build one offer.",
        "Gave up on the setup, no idea what half the settings did.",
    ],
    "Not satisfied with features": [
        "It works, it just doesn't do the one thing we bought it for.",
        "The reporting is thinner than the screenshots suggested.",
    ],
    "Not satisfied with support": [
        "Support answered fast, but the feature we needed is on your roadmap and not in the app.",
        "Three days for a first reply during BFCM.",
    ],
    "Testing multiple apps": [
        "Comparison shopping, nothing against yours.",
        "Kept the one our agency already knew.",
    ],
    "Other": [
        "Changed our whole promo strategy, this no longer fits it.",
        "Migrating off Shopify.",
    ],
}

ANNOTATIONS = [
    (300, "Listing rewrite went live: new hero, three screenshots, keyword pass."),
    (232, "Free plan removed. Installs dropped, paid conversion roughly doubled."),
    (168, "Annual plan launched at $190."),
    (104, "Shopify made the uninstall reason question mandatory. Reason coverage jumps here."),
    (47, "$49 tier launched for stores over 5k orders/mo."),
    (12, "Featured in a Shopify collection for two weeks."),
]

GA4_SOURCES = [
    ("google", 0.34), ("apps.shopify.com", 0.27), ("(direct)", 0.16),
    ("admin.shopify.com", 0.11), ("youtube.com", 0.05), ("reddit.com", 0.04),
    ("newsletter", 0.03),
]
GA4_CHANNELS = [
    ("Organic Search", 0.38), ("Referral", 0.29), ("Direct", 0.17),
    ("Organic Social", 0.09), ("Organic Video", 0.07),
]


def _slug(name: str) -> str:
    keep = "".join(c.lower() if c.isalnum() else "-" for c in name)
    return re.sub(r"-+", "-", keep).strip("-")


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _event(shop, kind, when, charge=None, reason=None, description=None):
    """One raw_app_events row in the exact shape partner_api.fetch_app_events emits."""
    charge_gid = charge["id"] if charge else None
    payload = {
        "type": kind,
        "occurredAt": _iso(when),
        "shop": {"id": shop["gid"], "myshopifyDomain": shop["domain"], "name": shop["name"]},
    }
    if charge:
        payload["charge"] = charge
    if reason:
        payload["reason"] = reason
    if description:
        payload["description"] = description
    return {
        "id": f"{shop['gid']}:{kind}:{_iso(when)}:{charge_gid or ''}",
        "type": kind,
        "occurred_at": when,
        "shop_gid": shop["gid"],
        "charge_gid": charge_gid,
        "charge": charge,
        "payload": payload,
    }


def _charge(shop, index, amount):
    gid = f"gid://partners/AppSubscription/{shop['n']}{index:02d}"
    return {
        "id": gid,
        "amount": {"amount": str(amount), "currencyCode": "USD"},
        "name": {MONTHLY: "Monthly", PLUS: "Plus", ANNUAL: "Annual"}[amount],
        "test": False,
    }


def build_shops(now: datetime) -> list[dict]:
    start = now - timedelta(days=MONTHS_OF_HISTORY * 30)
    names = set()
    shops = []
    for n in range(SHOP_COUNT):
        while True:
            name = f"{RNG.choice(FIRST)} {RNG.choice(SECOND)}"
            if name not in names:
                names.add(name)
                break
        # Installs accelerate: an app that grew looks nothing like one that
        # arrived all at once, and every retention cohort depends on the shape.
        share = (RNG.random() ** 0.62)
        installed = start + timedelta(days=share * MONTHS_OF_HISTORY * 30)
        shops.append({
            "n": 1000 + n,
            "gid": f"gid://shopify/Shop/{60000000 + n * 7}",
            "name": name,
            "domain": f"demo-{_slug(name)}.myshopify.com",
            "installed": installed,
            "country": RNG.choice(COUNTRIES),
            "industry": RNG.choice(INDUSTRIES),
            "fee_rate": RNG.choice(FEE_RATES),
        })
    return shops


def build_history(shops, now: datetime, mandatory_from: date):
    """Walk each shop's life and emit its events, charges and transactions."""
    events, transactions = [], []
    txn_n = 0

    for shop in shops:
        events.append(_event(shop, "RELATIONSHIP_INSTALLED", shop["installed"]))
        age_days = (now - shop["installed"]).days

        # Does it ever pay?
        subscribes = RNG.random() < 0.47 and age_days > 2
        sub_at = None
        plan = None
        charge = None
        if subscribes:
            delay = RNG.choice([0, 0, 0, 1, 2, 3, 6, 11, 19, 34])
            sub_at = shop["installed"] + timedelta(days=delay, hours=RNG.randrange(1, 20))
            if sub_at >= now:
                subscribes = False
            else:
                plan = RNG.choices([MONTHLY, ANNUAL, PLUS], weights=[74, 18, 8])[0]
                charge = _charge(shop, 1, plan)
                events.append(_event(shop, "SUBSCRIPTION_CHARGE_ACTIVATED", sub_at, charge=charge))

        # Plan change: Shopify mints a NEW subscription and cancels the old one.
        # The originals are kept because the billing history below needs the
        # price the merchant actually paid before the change.
        first_plan, first_charge = plan, charge
        changed_at = None
        if subscribes and plan == MONTHLY and RNG.random() < 0.16:
            changed_at = sub_at + timedelta(days=RNG.randrange(45, 400))
            if changed_at < now - timedelta(days=2):
                new_plan = RNG.choices([PLUS, ANNUAL], weights=[6, 4])[0]
                new_charge = _charge(shop, 2, new_plan)
                events.append(_event(shop, "SUBSCRIPTION_CHARGE_ACTIVATED", changed_at,
                                     charge=new_charge))
                events.append(_event(shop, "SUBSCRIPTION_CHARGE_CANCELED",
                                     changed_at + timedelta(hours=3), charge=charge))
                charge, plan = new_charge, new_plan
            else:
                changed_at = None

        # Cancel without uninstalling: a real and separate population.
        churn_at = None
        if subscribes and RNG.random() < 0.19:
            floor = changed_at or sub_at
            churn_at = floor + timedelta(days=RNG.randrange(30, 430))
            if churn_at < now:
                events.append(_event(shop, "SUBSCRIPTION_CHARGE_CANCELED", churn_at,
                                     charge=charge))
            else:
                churn_at = None

        # Uninstall. Payers leave less often than the never-paid.
        #
        # The floor is the shop's last subscription event, not its install date.
        # Drawing freely from the install date produced histories Shopify cannot
        # emit -- an uninstall followed by a subscription, with no reinstall in
        # between -- which derivation faithfully replays into a live
        # subscription on an uninstalled shop. check_invariants.py caught it.
        leave_odds = 0.24 if subscribes else 0.52
        gone_at = None
        if RNG.random() < leave_odds:
            floor = max(x for x in (shop["installed"], sub_at, changed_at, churn_at)
                        if x is not None)
            span = max(1, (now - floor).days)
            gone_at = floor + timedelta(days=RNG.randrange(1, span + 1),
                                        hours=RNG.randrange(0, 24))
            if gone_at < now:
                # RELATIONSHIP_DEACTIVATED is a store Shopify closed or froze.
                # Those merchants never see the exit survey, so they must never
                # carry a reason -- every coverage figure depends on it.
                deactivated = RNG.random() < 0.09
                reason = description = None
                if not deactivated:
                    # Optional before Shopify made it mandatory, near-universal after.
                    answers = RNG.random() < (0.93 if gone_at.date() >= mandatory_from else 0.27)
                    if answers:
                        picks = [RNG.choice(REASONS)]
                        if RNG.random() < 0.14:
                            picks.append(RNG.choice(REASONS))
                        reason = ", ".join(dict.fromkeys(picks))
                        if RNG.random() < 0.28:
                            bucket, _ = classify(picks[0])
                            description = RNG.choice(VERBATIMS[bucket]) \
                                if bucket in VERBATIMS else None
                events.append(_event(
                    shop,
                    "RELATIONSHIP_DEACTIVATED" if deactivated else "RELATIONSHIP_UNINSTALLED",
                    gone_at, reason=reason, description=description,
                ))
                shop["gone_at"] = gone_at
            else:
                gone_at = None

        # Money. One transaction per billing period each subscription was live.
        # Billed as segments rather than as one loop over the final plan: a shop
        # that changed plans was charged the old price first, and a history that
        # backdates today's price onto last year is the exact mistake this
        # dashboard exists to avoid.
        if subscribes:
            ends = min(x for x in (churn_at, gone_at, now) if x is not None)
            segments = []
            if changed_at:
                segments.append((sub_at, min(changed_at, ends), first_plan, first_charge))
                if changed_at < ends:
                    segments.append((changed_at, ends, plan, charge))
            else:
                segments.append((sub_at, ends, plan, charge))

            for seg_start, seg_end, seg_plan, seg_charge in segments:
                at = seg_start
                step = timedelta(days=365 if seg_plan == ANNUAL else 30)
                while at < seg_end:
                    gross = Decimal(seg_plan)
                    net = (gross * (1 - shop["fee_rate"])).quantize(Decimal("0.01"))
                    txn_n += 1
                    transactions.append({
                        "id": f"gid://partners/AppSubscriptionSale/{9000000 + txn_n}",
                        "type": "AppSubscriptionSale",
                        "created_at": at,
                        "shop_gid": shop["gid"],
                        "charge_gid": seg_charge["id"],
                        "billing_interval": (
                            "ANNUAL" if seg_plan == ANNUAL else "EVERY_30_DAYS"
                        ),
                        "gross_amount": str(gross),
                        # Shopify's revenue share, 0% under $1M of lifetime
                        # revenue. The processing fee lives only in the gap
                        # between gross and net.
                        "shopify_fee": "0.00",
                        "net_amount": str(net),
                        "currency_code": "USD",
                    })
                    at += step

            # A refund arrives only as a transaction, never as an app event.
            if transactions and RNG.random() < 0.05:
                last = transactions[-1]
                txn_n += 1
                transactions.append({
                    "id": f"gid://partners/AppSaleAdjustment/{9000000 + txn_n}",
                    "type": "AppSaleAdjustment",
                    "created_at": last["created_at"] + timedelta(days=RNG.randrange(1, 12)),
                    "shop_gid": shop["gid"],
                    "charge_gid": charge["id"],
                    "billing_interval": None,
                    "gross_amount": f"-{last['gross_amount']}",
                    "shopify_fee": "0.00",
                    "net_amount": f"-{last['net_amount']}",
                    "currency_code": "USD",
                })

    events.sort(key=lambda e: e["occurred_at"])
    return events, transactions


def seed_usage(conn, shops, now: datetime, tracking_from: datetime):
    """Usage events, which the Partner API has none of. A shop that installed
    before tracking started has no activation event to find, which is why
    activation reads unknown rather than 0% for the early cohorts."""
    settings = get_settings()
    activation = settings.usage_activation_event
    live = settings.usage_live_event
    rows = []
    n = 0
    for shop in shops:
        if shop["installed"] < tracking_from:
            continue
        end = shop.get("gone_at") or now
        first = shop["installed"] + timedelta(hours=RNG.randrange(1, 96))
        if first >= end or RNG.random() > 0.68:
            continue
        n += 1
        rows.append((shop["gid"], f"demo-{n}-a", "settings_completed", first, "{}"))
        n += 1
        rows.append((shop["gid"], f"demo-{n}-b", activation,
                     first + timedelta(hours=RNG.randrange(1, 30)), "{}"))
        if RNG.random() < 0.74:
            at = first + timedelta(days=RNG.randrange(1, 6))
            for _ in range(RNG.randrange(2, 9)):
                if at >= end:
                    break
                n += 1
                rows.append((shop["gid"], f"demo-{n}-c", live, at, "{}"))
                at += timedelta(days=RNG.randrange(2, 30))
    with conn.cursor() as cur:
        # received_at is set explicitly, not left to its now() default. The
        # activation reports take min(received_at) as the day tracking started
        # and only count shops that installed after it, so defaulted rows put
        # that boundary at this instant and every activation figure reads 0% of
        # 0 shops. Posting as it happens is also what a real integration does.
        cur.executemany(
            """insert into usage_events
                   (shop_gid, event_id, event_type, occurred_at, properties, received_at)
               values (%s, %s, %s, %s, %s, %s) on conflict do nothing""",
            [(*row, row[3]) for row in rows],
        )
    conn.commit()
    return len(rows)


def seed_ga4(conn, now: datetime, days: int = 400):
    """App Store listing traffic. The Partner API exposes none of this at all."""
    rows = []
    for i in range(days):
        day = (now - timedelta(days=i)).date()
        weekday = day.weekday()
        # Scaled so listing installs land near the install count in the event
        # feed. GA4 and the Partner API never agree exactly, which is the point
        # of the reconciliation panel, but an order of magnitude apart would be
        # a broken demo rather than an instructive disagreement.
        base = 44 + i * -0.05 + (7 if weekday < 5 else -9) + RNG.randrange(-8, 9)
        sessions = max(6, int(base))
        users = int(sessions * RNG.uniform(0.82, 0.94))
        clicks = int(sessions * RNG.uniform(0.035, 0.075))
        installs = 1 if RNG.random() < 0.34 else 0
        rows.append((day, "total", "", sessions, users, clicks, installs, 0))
        for dim, table in (("source", GA4_SOURCES), ("channel", GA4_CHANNELS)):
            for value, share in table:
                s = int(sessions * share * RNG.uniform(0.8, 1.2))
                if s < 1:
                    continue
                rows.append((day, dim, value, s, int(s * 0.88),
                             int(s * RNG.uniform(0.03, 0.08)),
                             1 if RNG.random() < 0.05 else 0, 0))
        for value, share in (("US", 0.47), ("GB", 0.12), ("CA", 0.1), ("AU", 0.08),
                             ("DE", 0.06), ("NL", 0.04), ("IN", 0.04)):
            s = int(sessions * share * RNG.uniform(0.8, 1.2))
            if s < 1:
                continue
            rows.append((day, "country", value, s, int(s * 0.9),
                         int(s * RNG.uniform(0.03, 0.08)),
                         1 if RNG.random() < 0.05 else 0, 0))
    with conn.cursor() as cur:
        cur.executemany(
            """insert into ga4_daily
                   (date, dimension, value, sessions, users, add_app_clicks, installs, ad_clicks)
               values (%s, %s, %s, %s, %s, %s, %s, %s)
               on conflict (date, dimension, value) do nothing""",
            rows,
        )
    conn.commit()
    return len(rows)


def seed_side_tables(conn, shops, now: datetime):
    """Country and industry (the CSV importer's job in real life), a few review
    dates, and the annotations that say why the chart moved."""
    with conn.cursor() as cur:
        cur.executemany(
            """update shops set country = %s, industry = %s where shop_gid = %s""",
            [(s["country"], s["industry"], s["gid"]) for s in shops],
        )
        reviewed = [s for s in shops if RNG.random() < 0.06 and "gone_at" not in s][:11]
        cur.executemany(
            "update shops set reviewed_at = %s where shop_gid = %s",
            [((now - timedelta(days=RNG.randrange(20, 500))).date(), s["gid"])
             for s in reviewed],
        )
        cur.executemany(
            "insert into annotations (on_date, note, author) values (%s, %s, %s)",
            [((now - timedelta(days=ago)).date(), note, "demo@example.com")
             for ago, note in ANNOTATIONS],
        )
    conn.commit()
    return len(reviewed)


def wipe(conn):
    tables = [
        "raw_app_events", "app_events", "charges", "subscriptions", "shops",
        "transactions", "usage_events", "ga4_daily", "annotations",
        "tracking_events", "sync_state",
    ]
    conn.execute(f"truncate {', '.join(tables)} restart identity cascade")
    conn.commit()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yes", action="store_true",
                        help="required: this truncates every table in DATABASE_URL")
    args = parser.parse_args()

    settings = get_settings()
    target = urlparse(settings.database_url)
    where = f"{target.hostname or 'localhost'}{target.path}"
    if not args.yes:
        print(f"Refusing to run. This TRUNCATES every table in {where}.")
        print("Re-run with --yes if that is the database you meant.")
        return 2

    if not settings.annual_plan_amounts_set:
        print("ANNUAL_PLAN_AMOUNTS is empty, so the $190 annual plan in this dataset")
        print("would be counted as monthly, at 12x its real MRR. Set ANNUAL_PLAN_AMOUNTS=190.00")
        print("and run again.")
        return 2

    print(f"Seeding {where} ...")
    conn = connect()
    run_migrations(conn)
    wipe(conn)

    now = datetime.now(timezone.utc)
    mandatory_from = settings.reason_mandatory_from
    shops = build_shops(now)
    events, transactions = build_history(shops, now, mandatory_from)

    upsert_charges(conn, events)
    raw = upsert_raw_events(conn, events)
    txns = upsert_transactions(conn, transactions)

    for shop in shops:
        derive_installation(conn, shop["gid"])

    usage = seed_usage(conn, shops, now, now - timedelta(days=250))
    ga4 = seed_ga4(conn, now)
    reviewed = seed_side_tables(conn, shops, now)

    conn.execute(
        "insert into sync_state (source, cursor, last_synced_at) values (%s, null, now())"
        " on conflict (source) do update set last_synced_at = now()",
        ("partner_api",),
    )
    conn.commit()

    (installed,) = conn.execute(
        "select count(*) from shops where install_state = 'installed'"
    ).fetchone()
    (mrr,) = conn.execute(
        """select coalesce(sum(s.monthly_amount), 0) from subscriptions s
           join shops sh on sh.shop_gid = s.shop_gid
           where s.churned_at is null and sh.install_state = 'installed'"""
    ).fetchone()

    print(f"  {len(shops)} shops, {installed} currently installed")
    print(f"  {raw} raw events, {txns} transactions, {usage} usage events")
    print(f"  {ga4} GA4 rows, {reviewed} marked as having reviewed, "
          f"{len(ANNOTATIONS)} annotations")
    print(f"  active MRR ${mrr}")
    print("\nRun it:  uv run uvicorn app_dashboard.web:app --reload")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
