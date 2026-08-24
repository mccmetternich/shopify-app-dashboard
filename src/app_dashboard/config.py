import logging
from datetime import date
from functools import lru_cache

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# Credentials that appear in this repository's own documentation. Refused
# outright: Basic auth bypasses the SSO allowlist, so one of these is a full
# account on a public-facing deployment.
PUBLISHED_CREDENTIALS = {"admin:change-me", "user:pass", "u:p", "admin:admin"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- required: nothing here has a safe default -------------------------
    database_url: str
    # "user:pass,user2:pass2" -- one credential pair per dashboard user
    dashboard_users: str
    # Redirect URIs must match Google's registration byte for byte, so this is
    # configured rather than derived from the request: behind a TLS-terminating
    # proxy the request scheme can read as http and silently break the callback.
    # Required, and deliberately without a default: a default here would point
    # every deployment at whoever published it.
    public_base_url: str
    # Comma-separated domains and individual addresses. Enforced by us, not by
    # Google: the OAuth client is External, so Google authenticates any account
    # and this is the gate. Required for the same reason as public_base_url --
    # inheriting somebody else's allowlist is a standing back door.
    google_allowed_domains: str

    # --- identity ----------------------------------------------------------
    # The brand being measured. The scoreboard calls itself
    # "<app_name> Scoreboard".
    app_name: str = "Densologie"
    # Used in export filenames. Falls back to a slug of app_name.
    app_slug: str = "densologie"
    # Public store listing URL. Hidden in UI when unset.
    app_listing_url: str = ""

    # --- optional integrations ---------------------------------------------
    slack_webhook_url: str | None = None
    google_client_id: str | None = None
    google_client_secret: str | None = None
    # Signs the session cookie. Rotating it logs everyone out. create_app
    # refuses to serve a non-local deployment while this is the published
    # default, so leaving it alone is a startup failure, not a silent weakness.
    session_secret: str = "dev-only-not-a-secret"
    # Shared secret for POST /ingest/usage, the one route an external caller
    # reaches. Unset means the endpoint refuses everything.
    usage_ingest_token: str | None = None

    # --- what the brand sells (Densologie SKUs) ---------------------------
    # Price tiers for display and seed purposes. Not used for billing logic.
    # Serum $149 | Capsules $99 | Bundle $228 | Stack $594
    product_tiers: str = "149.00,99.00,228.00,594.00"

    # --- Shopify Admin API -------------------------------------------------
    # Both must be set together or both left empty. Validated below.
    shopify_admin_token: str = ""  # validated at startup if non-empty
    shopify_shop_domain: str = ""  # e.g. "densologie.myshopify.com"

    # --- Meta Marketing API ------------------------------------------------
    # Both must be set together or both left empty. Validated below.
    meta_access_token: str = ""
    meta_account_id: str = ""

    # --- Recharge ----------------------------------------------------------
    recharge_api_token: str = ""

    # --- Store config -------------------------------------------------------
    store_timezone: str = "America/Los_Angeles"
    serum_sku: str = "HAIR-SERUM-50ML"  # for inventory tile (Phase C)

    # --- Ingest polling intervals ------------------------------------------
    shopify_poll_interval_minutes: int = 15
    meta_poll_interval_minutes: int = 15
    recharge_poll_interval_minutes: int = 15

    # --- ingest / event types ----------------------------------------------
    # Accepted event names on POST /ingest/usage. Anything outside the list is
    # rejected rather than stored. See docs/usage-events-integration.md.
    usage_event_types: str = "purchase,subscription_start,subscription_cancel,survey_response"
    # Which event type means "first purchase". Used by activation reports.
    usage_activation_event: str = "purchase"
    # Which event type means "subscription is live and billing". Used by live-
    # subscriber counts.
    usage_live_event: str = "subscription_start"

    # --- operational -------------------------------------------------------
    # Weekly Slack digest, as a cron day-of-week and hour in this timezone.
    digest_day_of_week: str = "mon"
    digest_hour: int = 9
    # Empty would fall through to the scheduler's system-local timezone, so the
    # digest would fire at an hour nobody chose. Normalised in the validator.
    digest_timezone: str = "UTC"
    # The header your proxy puts the real client address in. Rate limiting keys
    # on it. PREFER A SINGLE-VALUE HEADER your proxy overwrites: Fly-Client-IP,
    # CF-Connecting-IP, X-Real-IP. X-Forwarded-For works but is a list that
    # proxies append to, so only the rightmost entry is trustworthy, which is
    # what client_key reads. Empty means trust the socket peer, which is right
    # only with no proxy in front.
    trusted_client_ip_header: str = ""
    # No annotation may be dated before this. Set it to roughly when your brand
    # launched; a chart marker dated 1970 is a typo, not history.
    annotations_earliest: date = date(2020, 1, 1)

    # --- validation ---------------------------------------------------------

    @field_validator("dashboard_users")
    @classmethod
    def _every_pair_has_a_colon(cls, raw: str) -> str:
        """Catch a password containing a comma.

        The format is "user:pass,user2:pass2", so a comma inside a password
        silently truncates it: "admin:pa,ssword" parses as {"admin": "pa"} and
        logging in with "pa" succeeds. An operator who generated a random
        password would get a two-character one and never know. A fragment with
        no colon in it is that accident, every time.
        """
        for part in (p.strip() for p in raw.split(",")):
            if part and ":" not in part:
                raise ValueError(
                    f"DASHBOARD_USERS has a fragment with no colon: {part!r}. "
                    "Entries are user:pass separated by commas, so a password "
                    "containing a comma is silently truncated. Generate one "
                    "without: python -c \"import secrets; "
                    "print(secrets.token_urlsafe(24))\""
                )
        # Basic auth bypasses the Google domain allowlist by design, so a
        # published placeholder here is a full account. This repository is
        # public, which makes any example credential the first thing anyone
        # tries against a deployment.
        if any(p.strip() in PUBLISHED_CREDENTIALS for p in raw.split(",")):
            raise ValueError(
                "DASHBOARD_USERS is still an example credential from this "
                "repository. It grants full access and bypasses "
                "GOOGLE_ALLOWED_DOMAINS. Generate one: python -c \"import "
                "secrets; print(secrets.token_urlsafe(24))\""
            )
        return raw

    @model_validator(mode="after")
    def _credential_pairs_complete(self) -> "Settings":
        """Fail loudly if only half a credential pair is set.

        A token without a domain (or vice versa) is always a misconfiguration —
        the client constructor would blow up at first use, which is after the
        scheduler has already started and the dashboard is serving. Catching it
        here means the process refuses to start, which is the right behaviour.
        """
        if bool(self.shopify_admin_token) != bool(self.shopify_shop_domain):
            raise ValueError(
                "SHOPIFY_ADMIN_TOKEN and SHOPIFY_SHOP_DOMAIN must both be set "
                "or both be empty. One without the other is always a "
                "misconfiguration."
            )
        if bool(self.meta_access_token) != bool(self.meta_account_id):
            raise ValueError(
                "META_ACCESS_TOKEN and META_ACCOUNT_ID must both be set "
                "or both be empty. One without the other is always a "
                "misconfiguration."
            )
        return self

    @model_validator(mode="after")
    def _usage_events_agree(self) -> "Settings":
        """The activation and live events must be names the endpoint accepts."""
        known = self.usage_event_types_set
        for label, value in (("USAGE_ACTIVATION_EVENT", self.usage_activation_event),
                             ("USAGE_LIVE_EVENT", self.usage_live_event)):
            if known and value not in known:
                raise ValueError(
                    f"{label} is {value!r}, which is not in USAGE_EVENT_TYPES "
                    f"({', '.join(sorted(known))}). Events with that name would "
                    "be rejected on ingest, and the reports built on them would "
                    "read 0% rather than saying they have no data."
                )
        return self

    # --- derived ------------------------------------------------------------

    @property
    def dashboard_users_map(self) -> dict[str, str]:
        pairs = (p.split(":", 1) for p in self.dashboard_users.split(",") if ":" in p)
        return {u.strip(): pw for u, pw in pairs}

    @field_validator("digest_timezone")
    @classmethod
    def _timezone_is_named(cls, raw: str) -> str:
        return raw.strip() or "UTC"

    @property
    def dashboard_name(self) -> str:
        return f"{self.app_name} Scoreboard"

    @property
    def slug(self) -> str:
        if self.app_slug:
            return self.app_slug
        cleaned = "".join(c if c.isalnum() else "-" for c in self.app_name.lower())
        return "-".join(p for p in cleaned.split("-") if p) or "densologie"

    @property
    def usage_event_types_set(self) -> frozenset[str]:
        return frozenset(p.strip() for p in self.usage_event_types.split(",") if p.strip())


@lru_cache
def get_settings() -> Settings:
    return Settings()
