"""Shopify Admin GraphQL API client.

Sync (not async) to match the rest of the codebase. Uses httpx in sync mode.

Rate limiting: sleeps `throttle_seconds` between every request. Shopify's
Admin API cost budget (2000 points restored at 100/s) means 50-order pages at
roughly 1 point each are well within budget at 0.3s spacing, but operators on
lower-tier plans can raise the throttle via config.

C1 traps addressed here:
  - is_new_customer is derived from customer.numberOfOrders == 1 AT INGEST TIME.
    It is never recomputed from report-time data.
  - Refunds are fetched in a separate pass (fetch_refunds). Never assume an
    order with no refund record has refunded=0 without checking.
  - All timestamps come from Shopify as UTC ISO-8601. We assert the 'Z' suffix
    or '+00:00' offset before storing. A timestamp without timezone info raises
    immediately rather than silently storing a wall-clock time.
"""

import logging
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import httpx

logger = logging.getLogger(__name__)

# Shopify Admin GraphQL endpoint template.
_GQL_URL = "https://{domain}/admin/api/2024-01/graphql.json"

# Orders page size. Shopify allows up to 250 but 50 keeps cost budget low.
_PAGE_SIZE = 50

_ORDERS_QUERY = """
query FetchOrders($after: String, $query: String) {
  orders(first: %(page_size)d, after: $after, query: $query, sortKey: CREATED_AT) {
    edges {
      cursor
      node {
        id
        createdAt
        totalPriceSet { shopMoney { amount currencyCode } }
        customer {
          id
          numberOfOrders
        }
        lineItems(first: 20) {
          nodes {
            sku
            title
            quantity
            originalUnitPriceSet { shopMoney { amount } }
          }
        }
        customAttributes { key value }
        landingSite
        referringSite
        totalRefundedSet { shopMoney { amount } }
      }
    }
    pageInfo { hasNextPage endCursor }
  }
}
""" % {"page_size": _PAGE_SIZE}

_REFUND_QUERY = """
query FetchRefund($id: ID!) {
  order(id: $id) {
    id
    totalRefundedSet { shopMoney { amount } }
  }
}
"""


def _parse_decimal(value: str | None) -> Decimal:
    """Parse a Shopify money string to Decimal. Raises on bad input."""
    if value is None:
        return Decimal("0")
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"Cannot parse money value: {value!r}") from exc


def _parse_utc(iso_str: str) -> datetime:
    """Parse a Shopify ISO-8601 timestamp and assert it is UTC.

    Shopify guarantees UTC on all Admin API timestamps, documented at
    https://shopify.dev/docs/api/admin-graphql. We assert rather than silently
    convert: if Shopify ever changes this, we want a loud failure, not silent
    timezone corruption.
    """
    dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    if dt.tzinfo is None or dt.utcoffset().total_seconds() != 0:
        raise ValueError(
            f"Expected a UTC timestamp from Shopify Admin API, got: {iso_str!r}"
        )
    return dt.astimezone(timezone.utc)


def _extract_utm(custom_attributes: list[dict]) -> dict | None:
    """Pull utm_* keys from Shopify customAttributes.

    Returns a dict with at least one key, or None if no UTM data is present.
    Stores NULL (not {}) when no UTM exists — spec requirement.
    """
    utm_keys = {"utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term"}
    utm = {
        attr["key"]: attr["value"]
        for attr in custom_attributes
        if attr.get("key", "").lower() in utm_keys and attr.get("value")
    }
    return utm if utm else None


def _normalize_order(node: dict) -> dict:
    """Turn a raw GraphQL order node into a flat dict ready for upsert."""
    customer = node.get("customer") or {}
    n_orders = customer.get("numberOfOrders") or 0

    money = node.get("totalPriceSet", {}).get("shopMoney", {})
    total = _parse_decimal(money.get("amount"))
    currency = money.get("currencyCode", "USD")

    refunded_money = node.get("totalRefundedSet", {}).get("shopMoney", {})
    refunded = _parse_decimal(refunded_money.get("amount"))

    line_items = []
    for li in (node.get("lineItems") or {}).get("nodes", []):
        unit_price_set = li.get("originalUnitPriceSet") or {}
        unit_price_money = unit_price_set.get("shopMoney") or {}
        line_items.append({
            "sku": li.get("sku") or "",
            "title": li.get("title") or "",
            "quantity": li.get("quantity") or 1,
            "unit_price": float(_parse_decimal(unit_price_money.get("amount"))),
        })

    utm = _extract_utm(node.get("customAttributes") or [])

    # is_new_customer: True when numberOfOrders == 1 AT INGEST TIME.
    # This is the customer's total order count across all time per Shopify at
    # the moment we read it. We capture it once and never recompute (C1 trap).
    is_new_customer = (n_orders == 1)

    return {
        "id": node["id"],
        "created_at": _parse_utc(node["createdAt"]),
        "total": total,
        "refunded": refunded,
        "currency": currency,
        "is_new_customer": is_new_customer,
        "line_items": line_items,
        "source_utm": utm,
        "customer_id": customer.get("id"),
        # Keep raw customer node for the customers upsert
        "_customer_raw": customer,
    }


class ShopifyAdminClient:
    """Thin synchronous wrapper around the Shopify Admin GraphQL API.

    One client per scheduler run. Instantiated with credentials from config;
    never reads config itself (testability requirement).
    """

    def __init__(
        self,
        shop_domain: str,
        access_token: str,
        throttle_seconds: float = 0.3,
    ):
        if not shop_domain:
            raise ValueError("shop_domain must not be empty")
        if not access_token:
            raise ValueError("access_token must not be empty")
        self._url = _GQL_URL.format(domain=shop_domain)
        self._headers = {
            "X-Shopify-Access-Token": access_token,
            "Content-Type": "application/json",
        }
        self._throttle = throttle_seconds
        self._client = httpx.Client(timeout=30.0)

    def _gql(self, query: str, variables: dict) -> dict:
        """Execute one GraphQL request. Raises on HTTP error or GQL errors."""
        time.sleep(self._throttle)
        resp = self._client.post(
            self._url,
            headers=self._headers,
            json={"query": query, "variables": variables},
        )
        resp.raise_for_status()
        body = resp.json()
        if "errors" in body:
            raise RuntimeError(
                f"Shopify GraphQL errors: {body['errors']}"
            )
        return body["data"]

    def fetch_orders(
        self,
        created_at_min: datetime,
        cursor: str | None = None,
    ) -> tuple[list[dict], str | None]:
        """Fetch one page of orders created on or after `created_at_min`.

        Returns (orders, next_cursor). next_cursor is None when there are no
        more pages. Caller loops until next_cursor is None.

        orders: list of normalized dicts from _normalize_order(). Each dict
        includes a 'refunded' field sourced from totalRefundedSet on the order
        node. This covers refunds that existed at query time. For orders that
        were refunded AFTER this query, call fetch_refunds() separately.

        Note on refunds: we include totalRefundedSet in the main query to avoid
        N+1 calls for the common case. fetch_refunds() is for reconciliation
        runs — refreshing refund amounts on recently-settled orders.
        """
        # Shopify query filter: created_at:>={iso}
        min_iso = created_at_min.astimezone(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        variables = {
            "query": f"created_at:>={min_iso}",
            "after": cursor,
        }
        data = self._gql(_ORDERS_QUERY, variables)
        edges = data["orders"]["edges"]
        page_info = data["orders"]["pageInfo"]

        orders = [_normalize_order(edge["node"]) for edge in edges]
        next_cursor = page_info["endCursor"] if page_info["hasNextPage"] else None

        logger.debug(
            "fetch_orders: got %d orders, hasNextPage=%s",
            len(orders),
            page_info["hasNextPage"],
        )
        return orders, next_cursor

    def fetch_refunds(self, order_ids: list[str]) -> dict[str, Decimal]:
        """Fetch current refund totals for a list of order GIDs.

        Returns {order_id: total_refunded}. Used by the reconciliation pass to
        update refunded amounts on orders that may have been refunded after the
        initial ingest.

        Calls the Admin API once per order — Shopify GraphQL does not support
        bulk refund lookups by ID list. Rate-limiting sleep applies between each
        call.

        CRITICAL: This must be called separately from fetch_orders when doing a
        refund reconciliation pass. Never skip this (C1 trap).
        """
        result: dict[str, Decimal] = {}
        for order_id in order_ids:
            data = self._gql(_REFUND_QUERY, {"id": order_id})
            order_node = data.get("order")
            if order_node is None:
                logger.warning("fetch_refunds: order %r not found", order_id)
                continue
            money = (order_node.get("totalRefundedSet") or {}).get("shopMoney") or {}
            result[order_id] = _parse_decimal(money.get("amount"))
        return result

    def close(self) -> None:
        """Release the underlying HTTP connection pool."""
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
