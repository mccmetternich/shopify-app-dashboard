"""Pipeline constants used by ops.py and scheduler.py.

Phase A stub — the full ingest layer (orders, ad spend, subscriptions) is
built in Phase B. For now this file exists only to satisfy the imports that
remain in ops.py and scheduler.py.
"""

# Source keys written to sync_state by each ingest job.
# These must match the _SYNC_SOURCE constants in each ingest_*.py module.
SOURCE_SHOPIFY   = "shopify_orders"
SOURCE_META      = "meta_ad_spend"
SOURCE_RECHARGE  = "recharge_charges"

# Ordered list used by ops.sync_health() and data_quality_stats().
SYNC_SOURCES = [SOURCE_SHOPIFY, SOURCE_META, SOURCE_RECHARGE]

# Legacy constant — kept for any code that imported SOURCE directly.
# Nothing writes to this key; use SYNC_SOURCES for health checks.
SOURCE = "densologie_ingest"
TRANSACTIONS_SOURCE = "densologie_transactions"
