"""Redis storage layer for the tracker.

Key schema (shared with ml_predictor and api_bridge — keep in sync, see README):

    wm:articles                   SET     canonical titles being tracked
    wm:articles:pending           SET     card names requested by the API bridge, not yet validated
    wm:articles:invalid           SET     names that do not resolve to a Wikipedia article
    wm:alias                      HASH    normalized card name -> canonical title
    wm:views:daily:<title>        HASH    YYYYMMDD   -> views (Wikimedia REST API)
    wm:views:hourly:<title>       HASH    YYYYMMDDHH -> views (Wikimedia hourly dumps)
    wm:meta:<title>               HASH    bookkeeping (last_daily_fetch, hourly_since, ...)
    wm:stats:<title>              HASH    latest raw evolution, read by the API bridge
    wm:dumps:done                 ZSET    processed dump hours (member YYYYMMDDHH, score epoch)
    wm:alert:cooldown:<title>     STRING  TTL key preventing duplicate alerts
    wm:alerts                     STREAM  alert history
    wm:tracker:runs               STREAM  tracker run log

Titles are MediaWiki canonical titles with underscores, e.g. ``Saturne_(planète)``.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone

import redis

KEY_ARTICLES = "wm:articles"
KEY_PENDING = "wm:articles:pending"
KEY_INVALID = "wm:articles:invalid"
KEY_ALIAS = "wm:alias"
KEY_DUMPS_DONE = "wm:dumps:done"
KEY_ALERTS = "wm:alerts"
KEY_RUNS = "wm:tracker:runs"

STREAM_MAXLEN = 5000


def daily_key(title: str) -> str:
    return f"wm:views:daily:{title}"


def hourly_key(title: str) -> str:
    return f"wm:views:hourly:{title}"


def meta_key(title: str) -> str:
    return f"wm:meta:{title}"


def stats_key(title: str) -> str:
    return f"wm:stats:{title}"


def cooldown_key(title: str) -> str:
    return f"wm:alert:cooldown:{title}"


def normalize_title(name: str) -> str:
    """Normalize a card name the way MediaWiki does for main-namespace titles.

    ``"  saturne  (planète) "`` -> ``"Saturne_(planète)"``. The API bridge
    implements the exact same rule in JavaScript.
    """
    text = " ".join(name.replace("_", " ").split())
    if not text:
        return ""
    return (text[0].upper() + text[1:]).replace(" ", "_")


def hour_label(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%d%H")


def parse_hour_label(label: str) -> datetime:
    return datetime.strptime(label, "%Y%m%d%H").replace(tzinfo=timezone.utc)


class Storage:
    def __init__(self, client: redis.Redis):
        self.r = client

    @classmethod
    def from_url(cls, url: str) -> "Storage":
        return cls(redis.Redis.from_url(url, decode_responses=True))

    def ping(self) -> bool:
        return bool(self.r.ping())

    # --- article registry -------------------------------------------------

    def tracked_articles(self) -> set[str]:
        return set(self.r.smembers(KEY_ARTICLES))

    def tracked_count(self) -> int:
        return int(self.r.scard(KEY_ARTICLES))

    def known_aliases(self) -> dict[str, str]:
        return dict(self.r.hgetall(KEY_ALIAS))

    def register_articles(self, aliases: Mapping[str, str]) -> None:
        """Track canonical titles and remember which card names point to them."""
        if not aliases:
            return
        pipe = self.r.pipeline()
        pipe.sadd(KEY_ARTICLES, *set(aliases.values()))
        pipe.hset(KEY_ALIAS, mapping=dict(aliases))
        # A canonical title is also its own alias.
        pipe.hset(KEY_ALIAS, mapping={t: t for t in aliases.values()})
        pipe.srem(KEY_INVALID, *aliases.keys())
        pipe.execute()

    def invalid_names(self) -> set[str]:
        return set(self.r.smembers(KEY_INVALID))

    def mark_invalid(self, names: Iterable[str]) -> None:
        names = list(names)
        if names:
            self.r.sadd(KEY_INVALID, *names)

    def pop_pending(self, count: int) -> list[str]:
        return list(self.r.spop(KEY_PENDING, count) or [])

    # --- daily views -------------------------------------------------------

    def store_daily(self, title: str, views: Mapping[str, int]) -> None:
        if views:
            self.r.hset(daily_key(title), mapping={k: int(v) for k, v in views.items()})

    def daily_views(self, title: str) -> dict[str, int]:
        return {k: int(v) for k, v in self.r.hgetall(daily_key(title)).items()}

    def has_daily(self, title: str) -> bool:
        return bool(self.r.exists(daily_key(title)))

    # --- hourly views ------------------------------------------------------

    def store_hour(self, label: str, counts: Mapping[str, int], tracked: Iterable[str]) -> None:
        """Persist one dump hour for every tracked title and mark the hour processed.

        Titles absent from ``counts`` had zero views; nothing is written for them,
        readers treat a missing field of a processed hour as 0.
        """
        pipe = self.r.pipeline()
        for title, views in counts.items():
            pipe.hset(hourly_key(title), label, int(views))
        for title in tracked:
            # First hour processed while the title was tracked: marks the start
            # of reliable hourly coverage (hours are processed oldest-first).
            pipe.hsetnx(meta_key(title), "hourly_since", label)
        pipe.zadd(KEY_DUMPS_DONE, {label: parse_hour_label(label).timestamp()})
        pipe.execute()

    def mark_hour_skipped(self, label: str) -> None:
        """An hour whose dump never got published: don't retry it forever."""
        self.r.zadd(KEY_DUMPS_DONE, {label: parse_hour_label(label).timestamp()})

    def done_hours(self, since: datetime | None = None) -> list[str]:
        """Processed hour labels, newest first."""
        low = since.timestamp() if since else "-inf"
        return list(self.r.zrevrangebyscore(KEY_DUMPS_DONE, "+inf", low))

    def hourly_views(self, title: str) -> dict[str, int]:
        return {k: int(v) for k, v in self.r.hgetall(hourly_key(title)).items()}

    def trim_hourly(self, titles: Iterable[str], older_than: datetime) -> int:
        cutoff = hour_label(older_than)
        removed = 0
        for title in titles:
            old = [h for h in self.r.hkeys(hourly_key(title)) if h < cutoff]
            if old:
                removed += int(self.r.hdel(hourly_key(title), *old))
        self.r.zremrangebyscore(KEY_DUMPS_DONE, "-inf", f"({older_than.timestamp()}")
        return removed

    # --- bookkeeping -------------------------------------------------------

    def get_meta(self, title: str) -> dict[str, str]:
        return dict(self.r.hgetall(meta_key(title)))

    def set_meta(self, title: str, **fields: str | int | float) -> None:
        self.r.hset(meta_key(title), mapping={k: str(v) for k, v in fields.items()})

    def save_stats(self, title: str, stats: Mapping[str, str | int | float | None]) -> None:
        self.r.hset(stats_key(title), mapping={k: "" if v is None else str(v) for k, v in stats.items()})

    def in_cooldown(self, title: str) -> bool:
        return bool(self.r.exists(cooldown_key(title)))

    def set_cooldown(self, title: str, seconds: int) -> None:
        self.r.set(cooldown_key(title), int(time.time()), ex=seconds)

    def log_alert(self, fields: Mapping[str, str | int | float | None]) -> None:
        self.r.xadd(KEY_ALERTS, _stream_fields(fields), maxlen=STREAM_MAXLEN, approximate=True)

    def log_run(self, fields: Mapping[str, str | int | float | None]) -> None:
        self.r.xadd(KEY_RUNS, _stream_fields(fields), maxlen=STREAM_MAXLEN, approximate=True)


def _stream_fields(fields: Mapping[str, str | int | float | None]) -> dict[str, str]:
    return {k: "" if v is None else str(v) for k, v in fields.items()}
