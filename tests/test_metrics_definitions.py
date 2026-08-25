"""Drift guard: every slug in the METRICS registry must have an entry in
METRICS_DEFINITIONS.md.

The doc is the contract; the test is the lock. A metric added to metrics.py
without a corresponding doc entry fails loudly here so the doc cannot rot silently.
"""

import re
from pathlib import Path

import pytest

from app_dashboard.metrics import METRICS

DEFINITIONS_PATH = Path(__file__).parent.parent / "METRICS_DEFINITIONS.md"


def _documented_slugs() -> set[str]:
    """Return every slug found as a level-2 heading in the definitions file.

    Headings have the form:  ## slug — Human Name
    """
    text = DEFINITIONS_PATH.read_text()
    # Match ## followed by the slug (word chars + underscores), then either
    # a space+dash or end of line.
    return set(re.findall(r"^## ([a-z_]+)", text, re.MULTILINE))


def test_definitions_file_exists():
    assert DEFINITIONS_PATH.exists(), (
        f"METRICS_DEFINITIONS.md not found at {DEFINITIONS_PATH}. "
        "Create it — every metric in the registry needs a doc entry."
    )


@pytest.mark.parametrize("slug", sorted(METRICS))
def test_metric_is_documented(slug):
    """Every registered metric slug must appear in METRICS_DEFINITIONS.md."""
    documented = _documented_slugs()
    assert slug in documented, (
        f"Metric '{slug}' is in the METRICS registry but has no entry in "
        f"METRICS_DEFINITIONS.md. Add a '## {slug} — <name>' section."
    )


def test_no_undocumented_slugs():
    """Aggregate check: zero registry slugs missing from the doc."""
    documented = _documented_slugs()
    missing = sorted(set(METRICS) - documented)
    assert not missing, (
        f"These metrics are registered but not documented: {missing}. "
        "Add entries to METRICS_DEFINITIONS.md."
    )
