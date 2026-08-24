"""Meta Marketing API — Insights client.

Fetches daily ad spend rolled up by campaign. Sync (not async) to match the
rest of the codebase. Uses httpx in sync mode.

Design decisions:
  - Never silently returns $0 on error. Any HTTP error or token expiry raises
    immediately. A caller that catches exceptions and stores 0 would silently
    undercount spend and skew ROAS — same failure mode as the C1 traps.
  - Normalises spend to Decimal before returning, never float. Floats accumulate
    rounding error across 90-day sums.
  - Uses time_increment=1 so the caller gets one row per day per campaign, not
    a single aggregate. The ingest layer stores one DB row per (date, campaign_id).
"""

import logging
from datetime import date
from decimal import Decimal, InvalidOperation

import httpx

logger = logging.getLogger(__name__)

_BASE_URL = "https://graph.facebook.com/v20.0"

# Fields we request from the Insights edge.
_FIELDS = "campaign_id,campaign_name,spend,date_start,date_stop"


def _parse_decimal(value) -> Decimal:
    """Parse a Meta spend string/number to Decimal. Raises on bad input."""
    if value is None:
        raise ValueError("Meta returned null spend — token may be expired")
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"Cannot parse Meta spend value: {value!r}") from exc


class MetaInsightsClient:
    """Thin wrapper around the Meta Graph API /insights edge.

    One client per scheduler run. Instantiated with credentials from config;
    never reads config itself (testability requirement).
    """

    BASE_URL = _BASE_URL

    def __init__(self, account_id: str, access_token: str):
        if not account_id:
            raise ValueError("account_id must not be empty")
        if not access_token:
            raise ValueError("access_token must not be empty")
        self._account_id = account_id
        self._access_token = access_token
        self._client = httpx.Client(timeout=30.0)

    def _insights_url(self) -> str:
        return f"{self.BASE_URL}/act_{self._account_id}/insights"

    def fetch_daily_spend(
        self,
        date_start: date,
        date_end: date,
    ) -> list[dict]:
        """Fetch daily campaign-level spend for [date_start, date_end] inclusive.

        Returns a list of dicts:
            {
                "date": date,           # date of the row (date_start of the 1-day window)
                "campaign_id": str,
                "campaign_name": str,
                "spend": Decimal,       # always Decimal, never float or str
            }

        Raises RuntimeError on HTTP error or Meta API error (never silently
        returns $0). An expired token causes Meta to return an error JSON body,
        which we surface immediately.

        Handles pagination via the 'paging.next' cursor. Meta returns a page of
        results even with time_increment=1; large date ranges need multiple pages.
        """
        params = {
            "fields": _FIELDS,
            "time_increment": "1",
            "level": "campaign",
            "time_range": f'{{"since":"{date_start.isoformat()}","until":"{date_end.isoformat()}"}}',
            "access_token": self._access_token,
            "limit": 500,  # max per page
        }

        results: list[dict] = []
        url: str | None = self._insights_url()

        while url is not None:
            if url == self._insights_url():
                # First request — use params
                resp = self._client.get(url, params=params)
            else:
                # Subsequent pages — URL already contains all params
                resp = self._client.get(url)

            # Never silently swallow errors — raise on any HTTP failure.
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                # Try to surface the Meta error message for debuggability.
                try:
                    err_body = exc.response.json()
                    meta_msg = err_body.get("error", {}).get("message", "")
                except Exception:
                    meta_msg = ""
                raise RuntimeError(
                    f"Meta Insights API returned {exc.response.status_code}: "
                    f"{meta_msg or exc.response.text[:200]}"
                ) from exc

            body = resp.json()

            # Meta returns {"error": {...}} for invalid tokens even on 200.
            if "error" in body:
                err = body["error"]
                raise RuntimeError(
                    f"Meta Insights API error {err.get('code')}: "
                    f"{err.get('message', 'unknown error')}"
                )

            for row in body.get("data", []):
                results.append({
                    "date": date.fromisoformat(row["date_start"]),
                    "campaign_id": row["campaign_id"],
                    "campaign_name": row.get("campaign_name", ""),
                    "spend": _parse_decimal(row.get("spend", "0")),
                })

            # Follow pagination cursor if present.
            paging = body.get("paging", {})
            next_url = paging.get("next")
            url = next_url if next_url else None

        logger.debug(
            "fetch_daily_spend: %d rows for %s → %s",
            len(results),
            date_start.isoformat(),
            date_end.isoformat(),
        )
        return results

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
