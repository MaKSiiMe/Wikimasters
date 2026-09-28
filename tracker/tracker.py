#!/usr/bin/env python3
"""WikiMasters tracker — collects Wikipedia traffic for card articles.

Each cycle (default: hourly):
  1. registers seed articles and validates cards requested by the API bridge;
  2. refreshes daily views from the Wikimedia REST API (multi-year backfill on
     first sight, which is what the ML model trains on);
  3. ingests missing hours from the Wikimedia hourly pageview dumps;
  4. computes the raw 24h evolution of every article, stores it for the API
     bridge and pings Discord when traffic jumps by +300 % (configurable).

Between cycles, pending cards are validated every minute so a card discovered
by the userscript starts being tracked almost immediately.

Usage:
    python tracker.py              # run forever
    python tracker.py --once       # single cycle (cron / debugging)
    python tracker.py --add "Chat" --add "Mont Fuji"
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

from alerts import DiscordNotifier, Evolution, daily_evolution, hourly_evolution
from rarity import rarity_outlook
from storage import Storage, hour_label, normalize_title
from wikimedia import WikimediaClient, build_session

__version__ = "0.1.0"

log = logging.getLogger("tracker")

ARTICLES_FILE = Path(__file__).with_name("articles.txt")


def _env_bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    return int(value) if value else default


@dataclass(frozen=True)
class Config:
    redis_url: str = "redis://localhost:6379/0"
    project: str = "fr.wikipedia.org"
    contact: str = "https://github.com/MaKSiiMe/Wikimasters"
    extra_articles: tuple[str, ...] = ()
    interval_seconds: int = 3600
    pending_poll_seconds: int = 60
    backfill_days: int = 730
    daily_refresh_hours: int = 6
    hourly_enabled: bool = True
    hourly_bootstrap_hours: int = 48
    max_hours_per_run: int = 6
    hourly_retention_days: int = 14
    unpublished_grace_hours: int = 6
    max_tracked: int = 2000
    spike_threshold_pct: float = 300.0
    spike_min_views: int = 500
    alert_cooldown_hours: int = 24
    discord_webhook_url: str = ""

    @classmethod
    def from_env(cls) -> "Config":
        extra = tuple(a.strip() for a in os.getenv("TRACKER_ARTICLES", "").split(",") if a.strip())
        return cls(
            redis_url=os.getenv("REDIS_URL", cls.redis_url),
            project=os.getenv("WIKI_PROJECT", cls.project),
            contact=os.getenv("TRACKER_CONTACT", cls.contact),
            extra_articles=extra,
            interval_seconds=_env_int("TRACKER_INTERVAL_SECONDS", cls.interval_seconds),
            pending_poll_seconds=_env_int("PENDING_POLL_SECONDS", cls.pending_poll_seconds),
            backfill_days=_env_int("BACKFILL_DAYS", cls.backfill_days),
            daily_refresh_hours=_env_int("DAILY_REFRESH_HOURS", cls.daily_refresh_hours),
            hourly_enabled=_env_bool("ENABLE_HOURLY_DUMPS", cls.hourly_enabled),
            hourly_bootstrap_hours=_env_int("HOURLY_BOOTSTRAP_HOURS", cls.hourly_bootstrap_hours),
            max_hours_per_run=_env_int("MAX_HOURS_PER_RUN", cls.max_hours_per_run),
            hourly_retention_days=_env_int("HOURLY_RETENTION_DAYS", cls.hourly_retention_days),
            max_tracked=_env_int("MAX_TRACKED_ARTICLES", cls.max_tracked),
            spike_threshold_pct=float(os.getenv("SPIKE_THRESHOLD_PCT", cls.spike_threshold_pct)),
            spike_min_views=_env_int("SPIKE_MIN_VIEWS", cls.spike_min_views),
            alert_cooldown_hours=_env_int("ALERT_COOLDOWN_HOURS", cls.alert_cooldown_hours),
            discord_webhook_url=os.getenv("DISCORD_WEBHOOK_URL", ""),
        )

    @property
    def user_agent(self) -> str:
        # https://meta.wikimedia.org/wiki/User-Agent_policy
        return (f"WikiMastersTracker/{__version__} ({self.contact}) "
                f"python-requests/{requests.__version__}")


def load_seed_articles(path: Path = ARTICLES_FILE) -> list[str]:
    if not path.exists():
        return []
    lines = (line.split("#", 1)[0].strip() for line in path.read_text(encoding="utf-8").splitlines())
    return [line for line in lines if line]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Tracker:
    def __init__(self, cfg: Config, storage: Storage, wiki: WikimediaClient, notifier: DiscordNotifier):
        self.cfg = cfg
        self.storage = storage
        self.wiki = wiki
        self.notifier = notifier
        self._last_trim: datetime | None = None

    # --- 1. article registry ---------------------------------------------

    def register(self, names: list[str]) -> list[str]:
        """Resolve card names to Wikipedia articles and start tracking them.

        Returns the canonical titles that were newly added.
        """
        normalized = {normalize_title(n): n for n in names if normalize_title(n)}
        aliases = self.storage.known_aliases()
        invalid_known = self.storage.invalid_names()
        unknown = [n for n in normalized if n not in aliases and n not in invalid_known]
        if not unknown:
            return []

        resolved = self.wiki.resolve_titles(unknown)
        valid = {name: title for name, title in resolved.items() if title}
        invalid = [name for name, title in resolved.items() if not title]
        if invalid:
            log.info("Ignoring %d name(s) without Wikipedia article: %s", len(invalid), ", ".join(invalid[:10]))
            self.storage.mark_invalid(invalid)

        tracked = self.storage.tracked_articles()
        room = max(self.cfg.max_tracked - len(tracked), 0)
        new_titles = [t for t in dict.fromkeys(valid.values()) if t not in tracked]
        if len(new_titles) > room:
            log.warning("MAX_TRACKED_ARTICLES=%d reached, dropping %d article(s)",
                        self.cfg.max_tracked, len(new_titles) - room)
            keep = set(new_titles[:room]) | tracked
            valid = {n: t for n, t in valid.items() if t in keep}
            new_titles = new_titles[:room]

        self.storage.register_articles(valid)
        if new_titles:
            log.info("Now tracking %d new article(s): %s", len(new_titles), ", ".join(new_titles[:10]))
        return new_titles

    def process_pending(self) -> list[str]:
        pending = self.storage.pop_pending(50)
        if not pending:
            return []
        added = self.register(pending)
        if added:
            self.refresh_daily(added, force=True)
            self.update_stats(added)
        return added

    # --- 2. daily views (REST API) -----------------------------------------

    def refresh_daily(self, titles: list[str] | set[str], force: bool = False) -> int:
        today = utcnow().date()
        yesterday = today - timedelta(days=1)  # today's figure isn't published yet
        now_ts = time.time()
        fetched = 0
        for title in sorted(titles):
            meta = self.storage.get_meta(title)
            last = float(meta.get("last_daily_fetch", 0) or 0)
            if not force and now_ts - last < self.cfg.daily_refresh_hours * 3600:
                continue
            backfill = not self.storage.has_daily(title)
            start = today - timedelta(days=self.cfg.backfill_days if backfill else 7)
            try:
                views = self.wiki.daily_pageviews(title, start, yesterday)
            except requests.RequestException as exc:
                log.warning("Daily views failed for %s: %s", title, exc)
                continue
            self.storage.store_daily(title, views)
            self.storage.set_meta(title, last_daily_fetch=now_ts)
            fetched += 1
            if backfill:
                log.info("Backfilled %d day(s) for %s", len(views), title)
        return fetched

    # --- 3. hourly views (dumps) ---------------------------------------------

    def missing_hours(self, now: datetime) -> list[datetime]:
        """Unprocessed hours inside the bootstrap window, oldest first."""
        latest = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
        oldest = latest - timedelta(hours=self.cfg.hourly_bootstrap_hours - 1)
        done = set(self.storage.done_hours(since=oldest))
        hours = [oldest + timedelta(hours=i) for i in range(self.cfg.hourly_bootstrap_hours)]
        return [h for h in hours if hour_label(h) not in done]

    def refresh_hourly(self) -> int:
        if not self.cfg.hourly_enabled:
            return 0
        now = utcnow()
        processed = 0
        for hour in self.missing_hours(now)[: self.cfg.max_hours_per_run]:
            tracked = self.storage.tracked_articles()
            if not tracked:
                break
            label = hour_label(hour)
            started = time.monotonic()
            try:
                counts = self.wiki.hourly_dump(hour, tracked)
            except (requests.RequestException, OSError, EOFError) as exc:
                log.warning("Hourly dump %s failed: %s", label, exc)
                break
            if counts is None:
                if now - hour > timedelta(hours=self.cfg.unpublished_grace_hours):
                    log.warning("Dump %s never published, skipping it", label)
                    self.storage.mark_hour_skipped(label)
                    continue
                log.debug("Dump %s not published yet", label)
                break  # later hours aren't published either
            self.storage.store_hour(label, counts, tracked)
            processed += 1
            log.info("Ingested dump %s (%d/%d articles with views) in %.1fs",
                     label, len(counts), len(tracked), time.monotonic() - started)
        self._maybe_trim(now)
        return processed

    def _maybe_trim(self, now: datetime) -> None:
        if self._last_trim and now - self._last_trim < timedelta(days=1):
            return
        cutoff = now - timedelta(days=self.cfg.hourly_retention_days)
        removed = self.storage.trim_hourly(self.storage.tracked_articles(), cutoff)
        self._last_trim = now
        if removed:
            log.info("Trimmed %d hourly datapoint(s) older than %s", removed, cutoff.date())

    # --- 4. evolution + alerts -------------------------------------------------

    def evolution(self, title: str, done_hours: list[str], today: date) -> Evolution | None:
        if self.cfg.hourly_enabled:
            evo = hourly_evolution(title, self.storage.hourly_views(title), done_hours,
                                   self.storage.get_meta(title).get("hourly_since"))
            if evo:
                return evo
        return daily_evolution(title, self.storage.daily_views(title), today)

    def update_stats(self, titles: list[str] | set[str]) -> list[Evolution]:
        done_hours = self.storage.done_hours()
        today = utcnow().date()
        spikes = []
        for title in sorted(titles):
            stats: dict = {}
            evo = self.evolution(title, done_hours, today)
            if evo is not None:
                stats.update(evo.as_stats(self.cfg.spike_threshold_pct, self.cfg.spike_min_views))
                if evo.is_spike(self.cfg.spike_threshold_pct, self.cfg.spike_min_views):
                    spikes.append(evo)
            outlook = rarity_outlook(self.storage.daily_views(title), today)
            if outlook is not None:
                stats.update(outlook.as_stats())
            if stats:
                self.storage.save_stats(title, stats)
        for evo in spikes:
            self.alert(evo)
        return spikes

    def alert(self, evo: Evolution) -> None:
        if self.storage.in_cooldown(evo.title):
            return
        pct = evo.pct
        log.info("SPIKE %s: %s views vs %s (%s) over %s", evo.title, evo.current, evo.previous,
                 "new" if pct is None else f"{pct:+.0f}%", evo.window)
        sent = self.notifier.send(evo)
        self.storage.set_cooldown(evo.title, self.cfg.alert_cooldown_hours * 3600)
        self.storage.log_alert({
            "title": evo.title, "window": evo.window, "views_current": evo.current,
            "views_previous": evo.previous, "evolution_pct": None if pct is None else round(pct, 2),
            "as_of": evo.as_of, "discord_sent": int(sent),
        })

    # --- orchestration -------------------------------------------------------

    def seed(self) -> None:
        names = load_seed_articles() + list(self.cfg.extra_articles)
        if names:
            self.register(names)

    def run_cycle(self) -> dict:
        started = time.monotonic()
        errors = 0
        report = {"started_at": utcnow().isoformat(timespec="seconds")}
        try:
            self.seed()
            report["pending_added"] = len(self.process_pending())
            tracked = self.storage.tracked_articles()
            report["tracked"] = len(tracked)
            report["daily_fetched"] = self.refresh_daily(tracked)
            report["hours_ingested"] = self.refresh_hourly()
            report["spikes"] = len(self.update_stats(tracked))
        except Exception:  # keep the bot alive, the error lands in the run log
            errors += 1
            log.exception("Tracker cycle failed")
        report["errors"] = errors
        report["duration_s"] = round(time.monotonic() - started, 1)
        try:
            self.storage.log_run(report)
        except Exception:
            log.exception("Could not write run log")
        log.info("Cycle done: %s", report)
        return report

    def run_forever(self, stop: threading.Event) -> None:
        while not stop.is_set():
            self.run_cycle()
            deadline = time.monotonic() + self.cfg.interval_seconds
            while not stop.is_set() and time.monotonic() < deadline:
                stop.wait(min(self.cfg.pending_poll_seconds, max(deadline - time.monotonic(), 0)))
                if stop.is_set():
                    break
                try:
                    self.process_pending()
                except Exception:
                    log.exception("Pending cards processing failed")


def build_tracker(cfg: Config) -> Tracker:
    storage = Storage.from_url(cfg.redis_url)
    wiki = WikimediaClient(cfg.project, build_session(cfg.user_agent))
    notifier = DiscordNotifier(cfg.discord_webhook_url, cfg.project)
    return Tracker(cfg, storage, wiki, notifier)


def wait_for_redis(storage: Storage, stop: threading.Event, timeout: float = 120.0) -> None:
    deadline = time.monotonic() + timeout
    while not stop.is_set():
        try:
            storage.ping()
            return
        except Exception as exc:
            if time.monotonic() > deadline:
                raise SystemExit(f"Redis unreachable: {exc}") from exc
            log.info("Waiting for Redis... (%s)", exc)
            stop.wait(3)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--once", action="store_true", help="run a single cycle and exit")
    parser.add_argument("--add", action="append", default=[], metavar="NAME", help="track an extra article")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    cfg = Config.from_env()
    tracker = build_tracker(cfg)
    log.info("WikiMasters tracker %s — project=%s, hourly dumps=%s, alerts=%s",
             __version__, cfg.project, cfg.hourly_enabled, "on" if tracker.notifier.enabled else "off")

    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    wait_for_redis(tracker.storage, stop)

    if args.add:
        added = tracker.register(args.add)
        tracker.refresh_daily(added, force=True)
    if args.once:
        report = tracker.run_cycle()
        return 1 if report["errors"] else 0
    tracker.run_forever(stop)
    log.info("Tracker stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
