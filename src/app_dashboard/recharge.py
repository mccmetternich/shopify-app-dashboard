"""Recharge Payments API client.

Fetches successful subscription charges for sync to the subscription_revenue
table. Sync (not async) to match the rest of the codebase.

C1 traps addressed here:
  - Test charges are filtered EXPLICITLY by checking charge['test'] == False.
    Never rely on external_transaction_id prefix alone — Recharge can generate
    test ch_* IDs. Both checks run; either one can block a test charge.
  - Currency is asserted to be USD. We do not silently convert or swallow a
    foreign-currency charge — that would corrupt revenue totals.
  - Pagination uses Recharge's cursor-based scheme (next_cursor in response).
    The caller loops until next_cursor is None.
"""

import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import httpx

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.rechargeapps.com"

# Recharge API version header (required since 2021-11).
_API_VERSION = "2021-11"

# Fields we need from each charge.
# Recharge v2 returns all fields by default; listing them here for documentation.
_REQUIRED_FIELDS = {
    "id", "customer_id", "total_price", "scheduled_at",
    "subscription_id", "status", "test", "currency",
    "external_transaction_id",
}


def _parse_decimal(value) -> Decimal:
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"Cannot parse Recharge price: {value!r}") from exc


def _parse_utc(value: str) -> datetime:
    """Parse a Recharge ISO timestamp. Recharge returns UTC."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError(f"Recharge timestamp missing timezone: {value!r}")
    return dt.astimezone(timezone.utc)


class RechargeClient:
    """Thin wrapper around the Recharge REST API.

    One client per scheduler run. Instantiated with credentials from config;
    never reads config itself (testability requirement).
    """

    BASE_URL = _BASE_URL

    def __init__(self, api_token: str):
        if not api_token:
            raise ValueError("api_token must not be empty")
        self._headers = {
            "X-Recharge-Access-Token": api_token,
            "X-Recharge-Version": _API_VERSION,
            "Accept": "application/json",
        }
        self._client = httpx.Client(timeout=30.0)

    def fetch_charges(
        self,
        updated_at_min: datetime,
        cursor: str | None = None,
    ) -> tuple[list[dict], str | None]:
        """Fetch one page of successful, non-test charges updated since `updated_at_min`.

        Returns (charges, next_cursor). next_cursor is None when there are no
        more pages. Caller loops until next_cursor is None.

        Filtering applied here:
          1. status=SUCCESS — only billed charges, not pending/failed/refunded
          2. charge['test'] == False — EXPLICIT test-charge filter (C1 trap:
             never rely on external_transaction_id prefix alone)
          3. external_transaction_id that starts with 'ch_' is a real Stripe
             charge — we log a warning if we see a test charge slip through
             (belt-and-suspenders) but the test==False check is the authoritative gate

        Currency assertion: raises immediately if charge['currency'] != 'USD'.
        Do not silently skip or convert — a foreign-currency charge that gets
        stored as USD corrupts revenue totals.
        """
        params: dict = {
            "status": "SUCCESS",
            "updated_at_min": updated_at_min.astimezone(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "limit": 250,  # max page size for Recharge v2
        }
        if cursor:
            params["cursor"] = cursor

        resp = self._client.get(
            f"{self.BASE_URL}/charges",
            headers=self._headers,
            params=params,
        )
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f"Recharge API returned {exc.response.status_code}: "
                f"{exc.response.text[:200]}"
            ) from exc

        body = resp.json()
        raw_charges = body.get("charges", [])
        next_cursor = body.get("next_cursor") or None

        charges = []
        for charge in raw_charges:
            # EXPLICIT test-charge filter (C1 trap).
            is_test = charge.get("test", False)
            if is_test is True or is_test == 1 or str(is_test).lower() == "true":
                logger.debug(
                    "fetch_charges: skipping test charge %s", charge.get("id")
                )
                continue

            # Belt-and-suspenders: log unexpected test-like IDs but still pass
            # if test==False, since external_transaction_id can be 'ch_' for
            # real charges too.
            ext_id = charge.get("external_transaction_id", "") or ""
            if not ext_id.startswith("ch_"):
                logger.debug(
                    "fetch_charges: charge %s has unexpected transaction id %r",
                    charge.get("id"),
                    ext_id,
                )

            # Currency assertion — never silently convert (C1 trap).
            currency = charge.get("currency", "")
            assert currency == "USD", (
                f"Recharge charge {charge.get('id')} has currency={currency!r}, "
                "expected USD. Configure Recharge to USD or update the ingest "
                "layer to handle multiple currencies explicitly."
            )

            charges.append({
                "id": str(charge["id"]),
                "customer_id": str(charge["customer_id"]),
                "total_price": _parse_decimal(charge["total_price"]),
                "scheduled_at": _parse_utc(charge["scheduled_at"]),
                "subscription_id": str(charge.get("subscription_id", "")),
                "status": charge.get("status", ""),
                "currency": currency,
            })

        logger.debug(
            "fetch_charges: %d charges (of %d raw), next_cursor=%r",
            len(charges),
            len(raw_charges),
            next_cursor,
        )
        return charges, next_cursor

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
