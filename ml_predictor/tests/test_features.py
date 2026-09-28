from datetime import date

import numpy as np
import pytest

import features as F


def test_daily_array_fills_missing_days_with_zero():
    views, end = F.daily_array({"20260901": 10, "20260904": "40"})
    assert end == date(2026, 9, 4)
    assert views.tolist() == [10, 0, 0, 40]
    with pytest.raises(ValueError):
        F.daily_array({})


def test_last_window_pads_short_history_with_first_value():
    window = F.last_window(np.array([5.0, 6.0, 7.0]))
    assert window.shape == (F.WINDOW_DAYS,)
    assert window[0] == 5.0 and window[-3:].tolist() == [5.0, 6.0, 7.0]
    assert F.last_window(np.arange(100.0))[-1] == 99.0


def test_window_features_shape_and_scale_invariance():
    rng = np.random.default_rng(0)
    base = rng.uniform(50, 60, F.WINDOW_DAYS)
    a = F.window_features(base, date(2026, 9, 27))
    b = F.window_features(base * 1000, date(2026, 9, 27))
    assert a.shape == (F.WINDOW_DAYS, F.N_FEATURES) and a.dtype == np.float32
    # z-scores (col 0) are nearly identical whatever the traffic level
    assert np.allclose(a[:, 0], b[:, 0], atol=0.15)
    # day-of-week encoding: last row is the end date's weekday (Sunday = 6)
    assert np.isclose(np.arctan2(a[-1, 3], a[-1, 4]) % (2 * np.pi), 2 * np.pi * 6 / 7, atol=1e-5)
    with pytest.raises(ValueError):
        F.window_features(base[:-1], date(2026, 9, 27))


def test_targets_trend_and_spike():
    past = np.full(F.WINDOW_DAYS, 100.0)
    trend, spike = F.targets(past, np.full(F.HORIZON_DAYS, 100.0))
    assert trend == pytest.approx(0.0) and spike == 0.0
    future = np.array([100, 150, 500, 120, 100, 100, 100], dtype=float)
    trend, spike = F.targets(past, future)
    assert trend > 0 and spike == 1.0
    # a x4 jump from 5 to 20 views is noise, not a spike
    assert F.targets(np.full(F.WINDOW_DAYS, 5.0), np.full(F.HORIZON_DAYS, 20.0))[1] == 0.0


def test_build_windows_count_and_indices():
    views = np.arange(100, dtype=float)
    x, y_t, y_s, t = F.build_windows(views, date(2026, 9, 27))
    expected = 100 - F.WINDOW_DAYS - F.HORIZON_DAYS + 1
    assert x.shape == (expected, F.WINDOW_DAYS, F.N_FEATURES)
    assert len(y_t) == len(y_s) == len(t) == expected
    assert t[0] == F.WINDOW_DAYS - 1 and t[-1] == 100 - F.HORIZON_DAYS - 1
    assert F.build_windows(np.ones(10), date(2026, 9, 27))[0].shape[0] == 0


def test_trend_score_and_heuristic():
    assert F.trend_score(0.0) == 0
    assert F.trend_score(10.0) == 100 and F.trend_score(-10.0) == -100
    rising = np.concatenate([np.full(25, 100.0), [300.0, 400.0, 500.0]])
    assert F.heuristic_trend(rising) > 0
    assert F.heuristic_trend(rising[::-1]) < 0


def test_normalize_title_matches_tracker_rule():
    assert F.normalize_title(" saturne  (planète)") == "Saturne_(planète)"
