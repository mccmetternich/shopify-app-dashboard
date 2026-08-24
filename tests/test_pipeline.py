"""Phase A stub — pipeline tests.

The full ingest pipeline (orders, ad_spend, subscriptions) is built in Phase B.
These tests verify the Phase A pipeline module exports the expected constants.
"""

from app_dashboard.pipeline import SOURCE, TRANSACTIONS_SOURCE


def test_pipeline_source_constants_are_strings():
    assert isinstance(SOURCE, str)
    assert isinstance(TRANSACTIONS_SOURCE, str)
    assert SOURCE == "densologie_ingest"
    assert TRANSACTIONS_SOURCE == "densologie_transactions"
