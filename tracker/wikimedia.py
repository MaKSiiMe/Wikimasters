"""Thin clients for the Wikimedia data sources used by the tracker.

- REST API ``/metrics/pageviews/per-article/`` : daily views (the endpoint only
  accepts ``daily`` and ``monthly`` granularities) and multi-year backfill.
- MediaWiki Action API : title normalization / redirect resolution, so a card
  name maps to the article that actually receives the traffic.
- Hourly pageview dumps (dumps.wikimedia.org/other/pageviews) : the only public
  source of per-article *hourly* volumes. Each file (~60 MB gzip) is streamed
  and filtered on the fly, never stored on disk.
"""

from __future__ import annotations

import gzip
import logging
import threading
import time
from collections.abc import Iterable, Iterator
from datetime import date, datetime
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger(__name__)

REST_BASE = "https://wikimedia.org/api/rest_v1"
DUMPS_BASE = "https://dumps.wikimedia.org/other/pageviews"
RESOLVE_BATCH = 50  # MediaWiki limit for `titles=` on anonymous requests


def build_session(user_agent: str) -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": user_agent, "Accept": "application/json"})
    retry = Retry(
        total=4,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_maxsize=4)
    session.mount("https://", adapter)
    return session


class WikimediaClient:
    def __init__(self, project: str, session: requests.Session, timeout: float = 30.0,
                 min_interval: float = 0.1):
        self.project = project
        self.session = session
        self.timeout = timeout
        self.min_interval = min_interval  # politeness delay between API calls
        self._last_call = 0.0
        self._lock = threading.Lock()

    @property
    def dump_prefixes(self) -> tuple[bytes, bytes]:
        """Domain codes of this project in the dumps: desktop (`fr`) and mobile web (`fr.m`)."""
        code = self.project.split(".")[0]
        return (f"{code} ".encode(), f"{code}.m ".encode())

    def _throttle(self) -> None:
        with self._lock:
            wait = self._last_call + self.min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()

    # --- REST API ------------------------------------------------------------

    def daily_pageviews(self, title: str, start: date, end: date) -> dict[str, int]:
        """Daily user views of ``title`` between ``start`` and ``end`` (inclusive)."""
        url = (
            f"{REST_BASE}/metrics/pageviews/per-article/{self.project}/all-access/user/"
            f"{quote(title, safe='')}/daily/{start:%Y%m%d}00/{end:%Y%m%d}00"
        )
        self._throttle()
        resp = self.session.get(url, timeout=self.timeout)
        if resp.status_code == 404:  # no data in range (new or very quiet article)
            return {}
        resp.raise_for_status()
        return {item["timestamp"][:8]: int(item["views"]) for item in resp.json().get("items", [])}

    # --- Action API ------------------------------------------------------------

    def resolve_titles(self, names: Iterable[str]) -> dict[str, str | None]:
        """Map each name to its canonical title (underscored), or None if missing."""
        names = [n for n in dict.fromkeys(names) if n]
        result: dict[str, str | None] = {}
        for i in range(0, len(names), RESOLVE_BATCH):
            batch = names[i:i + RESOLVE_BATCH]
            self._throttle()
            resp = self.session.get(
                f"https://{self.project}/w/api.php",
                params={
                    "action": "query",
                    "format": "json",
                    "formatversion": "2",
                    "redirects": "1",
                    "titles": "|".join(batch),
                },
                timeout=self.timeout,
            )
            resp.raise_for_status()
            result.update(_map_resolved(batch, resp.json().get("query", {})))
        return result

    # --- Hourly dumps ------------------------------------------------------------

    def dump_url(self, hour: datetime) -> str:
        return f"{DUMPS_BASE}/{hour:%Y}/{hour:%Y-%m}/pageviews-{hour:%Y%m%d-%H}0000.gz"

    def hourly_dump(self, hour: datetime, titles: set[str]) -> dict[str, int] | None:
        """Views per tracked title for one hour, or None if the dump isn't published yet."""
        url = self.dump_url(hour)
        with self.session.get(url, stream=True, timeout=self.timeout,
                              headers={"Accept": "application/octet-stream"}) as resp:
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            resp.raw.decode_content = False
            with gzip.GzipFile(fileobj=resp.raw) as gz:
                return parse_dump_lines(gz, self.dump_prefixes, titles)


def parse_dump_lines(lines: Iterable[bytes], prefixes: tuple[bytes, ...],
                     titles: set[str]) -> dict[str, int]:
    """Sum views of ``titles`` in dump lines ``<domain> <title> <views> <bytes>``."""
    wanted = {t.encode(): t for t in titles}
    counts: dict[str, int] = {}
    for line in _iter_matching(lines, prefixes):
        parts = line.split(b" ")
        if len(parts) < 3:
            continue
        title = wanted.get(parts[1])
        if title is None:
            continue
        try:
            counts[title] = counts.get(title, 0) + int(parts[2])
        except ValueError:
            continue
    return counts


def _iter_matching(lines: Iterable[bytes], prefixes: tuple[bytes, ...]) -> Iterator[bytes]:
    # startswith() on a tuple is by far the cheapest filter: ~95 % of the
    # ~8M lines per file belong to other projects.
    for line in lines:
        if line.startswith(prefixes):
            yield line


def _map_resolved(names: list[str], query: dict) -> dict[str, str | None]:
    normalized = {n["from"]: n["to"] for n in query.get("normalized", [])}
    redirects = {r["from"]: r["to"] for r in query.get("redirects", [])}
    existing = {
        p["title"] for p in query.get("pages", [])
        if not p.get("missing") and not p.get("invalid") and p.get("ns", 0) == 0
    }
    out: dict[str, str | None] = {}
    for name in names:
        title = normalized.get(name, name)
        for _ in range(3):  # MediaWiki resolves one hop, be tolerant anyway
            if title not in redirects:
                break
            title = redirects[title]
        out[name] = title.replace(" ", "_") if title in existing else None
    return out
