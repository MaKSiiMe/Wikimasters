"""Feature engineering shared by train.py (GPU box) and inference_api.py (server).

Only depends on numpy so the inference image stays tiny. Any change here
changes the model contract: bump FEATURE_VERSION and retrain.

Input  : daily views of one article (Redis hash ``wm:views:daily:<title>``).
Window : the last WINDOW_DAYS days, as (WINDOW_DAYS, N_FEATURES) float32.
Targets:
  - trend : log((mean views over the next HORIZON_DAYS + 1) / (mean over the
            last BASELINE_DAYS + 1)), clipped to [-3, 3]. > 0 means rising.
  - spike : 1 if a day of the horizon reaches SPIKE_RATIO x the baseline
            (+300 %, same rule as the tracker alerts), else 0.
The model learns to recognise the early "frémissements" (quiet ramps,
weekly-pattern breaks) that precede those spikes.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import date, datetime, timedelta

import numpy as np

FEATURE_VERSION = 1
WINDOW_DAYS = 28
HORIZON_DAYS = 7
BASELINE_DAYS = 7
SPIKE_RATIO = 4.0  # +300 %
SPIKE_MIN_VIEWS = 100
FEATURE_NAMES = ("log_z", "log_diff", "log_vs_baseline", "dow_sin", "dow_cos")
N_FEATURES = len(FEATURE_NAMES)
TREND_CLIP = 3.0


def normalize_title(name: str) -> str:
    """Same rule as tracker/storage.py and api_bridge: 'saturne (planète)' -> 'Saturne_(planète)'."""
    text = " ".join(name.replace("_", " ").split())
    if not text:
        return ""
    return (text[0].upper() + text[1:]).replace(" ", "_")


def daily_array(daily: Mapping[str, int | str]) -> tuple[np.ndarray, date]:
    """Contiguous daily series from a {YYYYMMDD: views} mapping.

    Days missing between the first and last known day are zero-view days
    (the Wikimedia API omits them). Returns (views, date of the last value).
    """
    if not daily:
        raise ValueError("empty series")
    parsed = {datetime.strptime(k, "%Y%m%d").date(): int(v) for k, v in daily.items()}
    start, end = min(parsed), max(parsed)
    views = np.zeros((end - start).days + 1, dtype=np.float64)
    for day, value in parsed.items():
        views[(day - start).days] = max(value, 0)
    return views, end


def last_window(views: np.ndarray) -> np.ndarray:
    """Last WINDOW_DAYS values; short histories are left-padded with their first value."""
    tail = np.asarray(views, dtype=np.float64)[-WINDOW_DAYS:]
    if tail.size == 0:
        raise ValueError("empty series")
    if tail.size < WINDOW_DAYS:
        tail = np.pad(tail, (WINDOW_DAYS - tail.size, 0), mode="edge")
    return tail


def window_features(views: np.ndarray, end_date: date) -> np.ndarray:
    """(WINDOW_DAYS, N_FEATURES) float32 features for a window ending on ``end_date``.

    Level-invariant: an article at 50 views/day and one at 50 000 views/day
    with the same shape get the same z-scores; absolute scale only survives
    through the baseline-relative feature.
    """
    views = np.asarray(views, dtype=np.float64)
    if views.shape != (WINDOW_DAYS,):
        raise ValueError(f"expected {WINDOW_DAYS} days, got {views.shape}")
    lv = np.log1p(np.maximum(views, 0.0))
    z = (lv - lv.mean()) / max(lv.std(), 0.1)
    diff = np.clip(np.diff(lv, prepend=lv[0]), -TREND_CLIP, TREND_CLIP)
    rel = np.clip(lv - lv[-BASELINE_DAYS:].mean(), -5.0, 5.0)
    weekday = (end_date.weekday() - np.arange(WINDOW_DAYS - 1, -1, -1)) % 7
    angle = 2.0 * math.pi * weekday / 7.0
    return np.stack([z, diff, rel, np.sin(angle), np.cos(angle)], axis=1).astype(np.float32)


def targets(past: np.ndarray, future: np.ndarray) -> tuple[float, float]:
    """(trend, spike) labels for a window ``past`` followed by ``future``."""
    baseline = float(np.mean(past[-BASELINE_DAYS:]))
    trend = math.log((float(np.mean(future)) + 1.0) / (baseline + 1.0))
    peak = float(np.max(future))
    spike = peak >= SPIKE_RATIO * max(baseline, 1.0) and peak >= SPIKE_MIN_VIEWS
    return float(np.clip(trend, -TREND_CLIP, TREND_CLIP)), float(spike)


def build_windows(views: np.ndarray, end_date: date, stride: int = 1
                  ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Sliding training windows over one series.

    Returns X (N, WINDOW_DAYS, N_FEATURES), y_trend (N,), y_spike (N,) and
    t (N,) the index of each window's last day (used for temporal splits).
    """
    views = np.asarray(views, dtype=np.float64)
    first_day = end_date - timedelta(days=len(views) - 1)
    xs, trends, spikes, idx = [], [], [], []
    for t in range(WINDOW_DAYS - 1, len(views) - HORIZON_DAYS, stride):
        past = views[t - WINDOW_DAYS + 1: t + 1]
        future = views[t + 1: t + 1 + HORIZON_DAYS]
        xs.append(window_features(past, first_day + timedelta(days=t)))
        trend, spike = targets(past, future)
        trends.append(trend)
        spikes.append(spike)
        idx.append(t)
    if not xs:
        empty = np.zeros((0,), dtype=np.float32)
        return np.zeros((0, WINDOW_DAYS, N_FEATURES), dtype=np.float32), empty, empty, empty.astype(np.int64)
    return (np.stack(xs), np.asarray(trends, dtype=np.float32),
            np.asarray(spikes, dtype=np.float32), np.asarray(idx, dtype=np.int64))


def heuristic_trend(views: np.ndarray) -> float:
    """Model-free fallback: momentum of the last 3 days vs the 28-day mean (log ratio)."""
    window = last_window(views)
    short = float(np.mean(window[-3:]))
    long = float(np.mean(window))
    return float(np.clip(math.log((short + 1.0) / (long + 1.0)), -TREND_CLIP, TREND_CLIP))


def trend_score(trend: float) -> int:
    """Map a log-ratio trend to the -100..100 "Trend Score" shown in the HUD."""
    return int(round(100.0 * math.tanh(trend)))
