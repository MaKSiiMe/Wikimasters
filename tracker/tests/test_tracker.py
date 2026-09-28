from datetime import datetime, timedelta, timezone

import fakeredis
import pytest

import tracker as tracker_mod
from storage import Storage, hour_label, normalize_title
from tracker import Config, Tracker

NOW = datetime(2026, 9, 28, 12, 30, tzinfo=timezone.utc)


class FakeWiki:
    def __init__(self):
        self.pages = {"Chat": "Chat", "Canis_lupus": "Loup_gris", "Soleil": "Soleil"}
        self.daily = {}
        self.hours = {}  # label -> {title: views}; missing label = not published
        self.daily_calls = []

    def resolve_titles(self, names):
        return {n: self.pages.get(n) for n in names}

    def daily_pageviews(self, title, start, end):
        self.daily_calls.append((title, start, end))
        return self.daily.get(title, {})

    def hourly_dump(self, hour, titles):
        counts = self.hours.get(hour_label(hour))
        return None if counts is None else {t: v for t, v in counts.items() if t in titles}


class FakeNotifier:
    enabled = True

    def __init__(self):
        self.sent = []

    def send(self, evo):
        self.sent.append(evo)
        return True


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(tracker_mod, "utcnow", lambda: NOW)
    monkeypatch.setattr(tracker_mod, "load_seed_articles", lambda: [])
    storage = Storage(fakeredis.FakeRedis(decode_responses=True))
    wiki, notifier = FakeWiki(), FakeNotifier()
    cfg = Config(hourly_bootstrap_hours=48, max_hours_per_run=100, spike_min_views=100)
    return Tracker(cfg, storage, wiki, notifier), storage, wiki, notifier


def test_normalize_title():
    assert normalize_title("  saturne   (planète) ") == "Saturne_(planète)"
    assert normalize_title("Mont_Fuji") == "Mont_Fuji"
    assert normalize_title("   ") == ""


def test_register_resolves_redirects_and_marks_invalid(env):
    t, storage, wiki, _ = env
    added = t.register(["chat", "Canis lupus", "Pas une carte"])
    assert sorted(added) == ["Chat", "Loup_gris"]
    assert storage.tracked_articles() == {"Chat", "Loup_gris"}
    assert storage.known_aliases()["Canis_lupus"] == "Loup_gris"
    assert storage.invalid_names() == {"Pas_une_carte"}
    # already known names are not resolved again
    wiki.pages.clear()
    assert t.register(["Chat", "Pas une carte"]) == []


def test_register_respects_max_tracked(env):
    t, storage, _, _ = env
    t.cfg = Config(max_tracked=1)
    t.register(["Chat", "Soleil"])
    assert storage.tracked_count() == 1


def test_process_pending_backfills_new_articles(env):
    t, storage, wiki, _ = env
    wiki.daily["Chat"] = {"20260926": 100, "20260927": 120}
    storage.r.sadd("wm:articles:pending", "Chat")
    assert t.process_pending() == ["Chat"]
    title, start, end = wiki.daily_calls[0]
    assert (NOW.date() - start).days == t.cfg.backfill_days
    assert end == NOW.date() - timedelta(days=1)
    assert storage.daily_views("Chat") == {"20260926": 100, "20260927": 120}
    assert storage.r.hget("wm:stats:Chat", "evolution_pct") == "20.0"


def test_refresh_daily_skips_recent_fetches(env):
    t, storage, wiki, _ = env
    storage.register_articles({"Chat": "Chat"})
    assert t.refresh_daily({"Chat"}) == 1
    assert t.refresh_daily({"Chat"}) == 0  # fetched less than 6h ago
    assert t.refresh_daily({"Chat"}, force=True) == 1


def test_hourly_ingestion_evolution_and_alert(env):
    t, storage, wiki, notifier = env
    storage.register_articles({"Chat": "Chat", "Soleil": "Soleil"})
    latest = NOW.replace(minute=0) - timedelta(hours=1)
    for i in range(48):
        label = hour_label(latest - timedelta(hours=i))
        wiki.hours[label] = {"Chat": 50 if i < 24 else 10, "Soleil": 5}

    assert t.refresh_hourly() == 48
    assert storage.get_meta("Chat")["hourly_since"] == hour_label(latest - timedelta(hours=47))

    spikes = t.update_stats(storage.tracked_articles())
    assert [s.title for s in spikes] == ["Chat"]
    assert spikes[0].window == "24h" and spikes[0].pct == 400.0
    assert storage.r.hget("wm:stats:Soleil", "evolution_pct") == "0.0"
    assert len(notifier.sent) == 1 and storage.r.xlen("wm:alerts") == 1

    t.update_stats(storage.tracked_articles())  # cooldown: no duplicate ping
    assert len(notifier.sent) == 1


def test_hourly_stops_at_unpublished_and_skips_lost_hours(env):
    t, storage, wiki, _ = env
    storage.register_articles({"Chat": "Chat"})
    latest = NOW.replace(minute=0) - timedelta(hours=1)
    # every hour published except the very latest one and a lost one 30h ago
    for i in range(1, 48):
        if i != 30:
            wiki.hours[hour_label(latest - timedelta(hours=i))] = {"Chat": 1}
    t.refresh_hourly()
    done = set(storage.done_hours())
    assert hour_label(latest - timedelta(hours=30)) in done  # skipped, not retried forever
    assert hour_label(latest) not in done  # still expected, retried next cycle
    assert len(done) == 47


def test_run_cycle_logs_report(env):
    t, storage, wiki, _ = env
    t.cfg = Config(hourly_enabled=False, extra_articles=("Chat",))
    wiki.daily["Chat"] = {"20260926": 100, "20260927": 600}
    report = t.run_cycle()
    assert report["errors"] == 0 and report["tracked"] == 1 and report["spikes"] == 1
    assert storage.r.xlen("wm:tracker:runs") == 1
