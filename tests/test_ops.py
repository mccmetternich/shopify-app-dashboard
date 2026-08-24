"""Phase A: ops tests updated for Densologie Scoreboard pipeline constants."""

from types import SimpleNamespace

from app_dashboard.ops import build_stale_message, check_stale_sync, sync_health
from app_dashboard.pipeline import SOURCE


def _settings(webhook="http://hook"):
    return SimpleNamespace(slack_webhook_url=webhook,
                           public_base_url="https://dash.example.com",
                           app_name="Densologie",
                           dashboard_name="Densologie Scoreboard")


def _capture():
    sent = []

    def post(url, json):
        sent.append(json)
        return SimpleNamespace(status_code=200)

    return sent, post


def _synced(db, ago_sql):
    db.execute(
        f"insert into sync_state (source, cursor, last_synced_at) "
        f"values (%s, null, now() - interval %s) "
        "on conflict (source) do update set last_synced_at = excluded.last_synced_at",
        (SOURCE, ago_sql),
    )
    db.commit()


def test_health_is_fresh_within_threshold(db):
    _synced(db, "60 minutes")
    health = sync_health(db)
    assert health["stale"] is False
    assert health["age_minutes"] == 60


def test_health_goes_stale_past_threshold(db):
    _synced(db, "3 hours")
    assert sync_health(db)["stale"] is True


def test_a_sync_that_never_ran_is_stale(db):
    health = sync_health(db)
    assert health["stale"] is True
    assert health["last_synced_at"] is None and health["age_minutes"] is None


def test_fresh_sync_posts_nothing(db):
    _synced(db, "30 minutes")
    sent, post = _capture()
    assert check_stale_sync(db, _settings(), http_post=post) is False
    assert sent == []


def test_stale_sync_posts_alert(db):
    _synced(db, "4 hours")
    sent, post = _capture()
    assert check_stale_sync(db, _settings(), http_post=post) is True
    assert len(sent) == 1


def test_no_webhook_configured_is_a_noop(db):
    _synced(db, "4 hours")
    sent, post = _capture()
    assert check_stale_sync(db, _settings(webhook=None), http_post=post) is False
    assert sent == []


def test_stale_message_carries_the_age():
    text = str(build_stale_message(180, "https://dash.example.com"))
    assert "180 minutes ago" in text
