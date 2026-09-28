"""Traffic evolution, spike detection and Discord notifications."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote

import requests

log = logging.getLogger(__name__)

HOURS_PER_WINDOW = 24


@dataclass(frozen=True)
class Evolution:
    """Views in the latest 24h window vs the 24h window before it."""

    title: str
    window: str  # "24h" (hourly dumps) or "1d" (daily REST data)
    current: int
    previous: int
    as_of: str  # last hour (YYYYMMDDHH) or last day (YYYYMMDD) included

    @property
    def pct(self) -> float | None:
        if self.previous <= 0:
            return None
        return (self.current - self.previous) / self.previous * 100.0

    def is_spike(self, threshold_pct: float, min_views: int) -> bool:
        if self.current < min_views:
            return False
        pct = self.pct
        # previous == 0 with a big current window: an article coming out of nowhere.
        return pct is None or pct >= threshold_pct

    def as_stats(self, threshold_pct: float, min_views: int) -> dict[str, str | int | float | None]:
        pct = self.pct
        return {
            "title": self.title,
            "window": self.window,
            "views_current": self.current,
            "views_previous": self.previous,
            "evolution_pct": None if pct is None else round(pct, 2),
            "is_spike": int(self.is_spike(threshold_pct, min_views)),
            "as_of": self.as_of,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }


def hourly_evolution(title: str, hourly: dict[str, int], done_hours: list[str],
                     hourly_since: str | None) -> Evolution | None:
    """24h vs previous 24h from processed dump hours (``done_hours`` newest first).

    Returns None unless the 48 latest processed hours were all processed while
    the title was tracked (otherwise missing fields would read as zero views).
    """
    window = done_hours[: 2 * HOURS_PER_WINDOW]
    if len(window) < 2 * HOURS_PER_WINDOW or not hourly_since or window[-1] < hourly_since:
        return None
    current = sum(hourly.get(h, 0) for h in window[:HOURS_PER_WINDOW])
    previous = sum(hourly.get(h, 0) for h in window[HOURS_PER_WINDOW:])
    return Evolution(title, "24h", current, previous, window[0])


def daily_evolution(title: str, daily: dict[str, int], today: date,
                    max_staleness_days: int = 3) -> Evolution | None:
    """Latest complete day vs the day before, from REST API data."""
    if not daily:
        return None
    latest = max(daily)
    latest_day = datetime.strptime(latest, "%Y%m%d").date()
    if (today - latest_day).days > max_staleness_days:
        return None
    before = (latest_day - timedelta(days=1)).strftime("%Y%m%d")
    if before < min(daily):
        return None  # no history before the latest day yet
    # The REST API omits zero-view days, hence the default.
    return Evolution(title, "1d", daily[latest], daily.get(before, 0), latest)


class DiscordNotifier:
    def __init__(self, webhook_url: str | None, project: str, session: requests.Session | None = None,
                 timeout: float = 10.0):
        self.webhook_url = (webhook_url or "").strip()
        self.project = project
        self.session = session or requests.Session()
        self.timeout = timeout

    @property
    def enabled(self) -> bool:
        return bool(self.webhook_url)

    def build_payload(self, evo: Evolution) -> dict:
        pct = evo.pct
        pct_text = "nouvelle activité" if pct is None else f"{pct:+.0f} %"
        human = evo.title.replace("_", " ")
        window = "24 h" if evo.window == "24h" else "1 jour"
        return {
            "username": "WikiMasters Tracker",
            "embeds": [{
                "title": f"🚀 Pic de trafic : {human}",
                "url": f"https://{self.project}/wiki/{quote(evo.title)}",
                "description": f"**{pct_text}** de vues sur {window} — la carte pourrait prendre de la valeur.",
                "color": 0x22C55E,
                "fields": [
                    {"name": f"Vues ({window} actuelles)", "value": _fmt(evo.current), "inline": True},
                    {"name": f"Vues ({window} précédentes)", "value": _fmt(evo.previous), "inline": True},
                ],
                "footer": {"text": f"Fenêtre close à {evo.as_of} UTC"},
                "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }],
        }

    def send(self, evo: Evolution) -> bool:
        if not self.enabled:
            return False
        payload = self.build_payload(evo)
        for _ in range(3):
            try:
                resp = self.session.post(self.webhook_url, json=payload, timeout=self.timeout)
            except requests.RequestException as exc:
                log.warning("Discord webhook unreachable: %s", exc)
                return False
            if resp.status_code == 429:  # Discord rate limit
                delay = float(resp.json().get("retry_after", 1.0)) if resp.content else 1.0
                time.sleep(min(delay, 30.0))
                continue
            if resp.ok:
                return True
            log.warning("Discord webhook rejected alert (%s): %s", resp.status_code, resp.text[:200])
            return False
        return False


def _fmt(n: int) -> str:
    return f"{n:,}".replace(",", " ")
