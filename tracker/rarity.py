"""WikiMasters rarity tiers, as documented in the game ("vues mensuelles").

The game recomputes a card's rarity from the article's monthly views, but it
is not documented whether "monthly" means a rolling 30-day window or the
previous calendar month (and the game's figures may lag behind Wikimedia).
Both hypotheses are computed so the HUD observations can settle it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

# (code, minimum monthly views), ascending.
TIERS: tuple[tuple[str, int], ...] = (
    ("C", 0), ("PC", 50), ("R", 250), ("SR", 1000), ("UR", 5000), ("L", 20000),
)
CODES = tuple(code for code, _ in TIERS)
WINDOW_DAYS = 30
RATE_DAYS = 7  # recent daily rate used to project the rolling window


def rarity_for(monthly_views: float) -> str:
    code = TIERS[0][0]
    for tier, minimum in TIERS:
        if monthly_views >= minimum:
            code = tier
    return code


def rank(code: str) -> int:
    return CODES.index(code)


@dataclass(frozen=True)
class RarityOutlook:
    as_of: str  # last day included (YYYYMMDD)
    views_30d: int
    rarity_30d: str
    views_prev_month: int  # previous calendar month
    rarity_prev_month: str
    next_rarity: str | None
    views_to_next: int | None
    eta_days: int | None  # days until the rolling window reaches next_rarity
    downgrade_eta_days: int | None  # days until it falls below the current tier

    def as_stats(self) -> dict[str, str | int | None]:
        return {
            "rarity_as_of": self.as_of,
            "views_30d": self.views_30d,
            "rarity_30d": self.rarity_30d,
            "views_prev_month": self.views_prev_month,
            "rarity_prev_month": self.rarity_prev_month,
            "next_rarity": self.next_rarity,
            "views_to_next": self.views_to_next,
            "eta_days": self.eta_days,
            "downgrade_eta_days": self.downgrade_eta_days,
        }


def rarity_outlook(daily: dict[str, int], today: date, horizon_days: int = 30,
                   max_staleness_days: int = 3) -> RarityOutlook | None:
    """Current tier, distance to the next one and projected crossing dates.

    Projection: each future day adds the recent average (last RATE_DAYS days)
    while the day leaving the rolling window is known exactly from history.
    """
    if not daily:
        return None
    as_of = _parse(max(daily))
    if (today - as_of).days > max_staleness_days:
        return None

    def views(day: date) -> int:
        return daily.get(day.strftime("%Y%m%d"), 0)

    window = [as_of - timedelta(days=i) for i in range(WINDOW_DAYS)]
    total = sum(views(d) for d in window)
    current = rarity_for(total)
    rate = sum(views(as_of - timedelta(days=i)) for i in range(RATE_DAYS)) / RATE_DAYS

    idx = rank(current)
    next_code = CODES[idx + 1] if idx + 1 < len(CODES) else None
    next_min = TIERS[idx + 1][1] if next_code else None
    current_min = TIERS[idx][1]

    eta = downgrade = None
    rolling = float(total)
    for k in range(1, horizon_days + 1):
        rolling += rate - views(as_of + timedelta(days=k - WINDOW_DAYS))
        if eta is None and next_min is not None and rolling >= next_min:
            eta = k
        if downgrade is None and current_min > 0 and rolling < current_min:
            downgrade = k
        if (eta or next_min is None) and (downgrade or current_min == 0):
            break

    month_start = as_of.replace(day=1)
    prev_end = month_start - timedelta(days=1)
    prev_start = prev_end.replace(day=1)
    prev_total = sum(views(prev_start + timedelta(days=i)) for i in range((prev_end - prev_start).days + 1))

    return RarityOutlook(
        as_of=as_of.strftime("%Y%m%d"),
        views_30d=total,
        rarity_30d=current,
        views_prev_month=prev_total,
        rarity_prev_month=rarity_for(prev_total),
        next_rarity=next_code,
        views_to_next=None if next_min is None else next_min - total,
        eta_days=eta,
        downgrade_eta_days=downgrade,
    )


def _parse(label: str) -> date:
    return date(int(label[:4]), int(label[4:6]), int(label[6:8]))
