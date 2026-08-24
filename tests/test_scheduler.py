"""Phase A: scheduler tests updated — Partner API sync jobs removed."""

from types import SimpleNamespace

import app_dashboard.scheduler as sched


def test_weekly_digest_is_registered_at_the_configured_local_time(monkeypatch):
    """Only the wiring: send_weekly_digest itself is tested in test_digest."""

    started = {}

    class FakeScheduler:
        def __init__(self):
            self.jobs = []

        def add_job(self, func, trigger, **kw):
            self.jobs.append((trigger, kw))

        def start(self):
            started["yes"] = True

    fake = FakeScheduler()
    monkeypatch.setattr(sched, "BackgroundScheduler", lambda: fake)
    sched.start_scheduler(lambda: None, SimpleNamespace(
        digest_day_of_week="tue", digest_hour=7, digest_timezone="Europe/Berlin",
        # Phase B ingest settings — tokens empty so jobs are NO-OPs
        shopify_admin_token="", shopify_shop_domain="",
        meta_access_token="", meta_account_id="",
        recharge_api_token="",
        shopify_poll_interval_minutes=15,
        meta_poll_interval_minutes=15,
        recharge_poll_interval_minutes=15,
    ))

    digest = [kw for trigger, kw in fake.jobs if trigger == "cron"]
    assert len(digest) == 1
    assert digest[0]["day_of_week"] == "tue"
    assert digest[0]["hour"] == 7 and digest[0]["minute"] == 0
    assert digest[0]["timezone"] == "Europe/Berlin"
    assert started["yes"] is True
