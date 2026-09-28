from datetime import date, timedelta

from rarity import rarity_for, rarity_outlook


def daily_series(end: date, values):
    """values oldest first, the last one on `end`."""
    n = len(values)
    return {(end - timedelta(days=n - 1 - i)).strftime("%Y%m%d"): v for i, v in enumerate(values)}


def test_rarity_for_thresholds():
    assert [rarity_for(v) for v in (0, 49, 50, 249, 250, 999, 1000, 4999, 5000, 19999, 20000)] == \
        ["C", "C", "PC", "PC", "R", "R", "SR", "SR", "UR", "UR", "L"]


def test_outlook_current_tier_and_distance():
    end = date(2026, 9, 27)
    out = rarity_outlook(daily_series(end, [160] * 30), today=date(2026, 9, 28))
    assert out.views_30d == 4800 and out.rarity_30d == "SR"
    assert out.next_rarity == "UR" and out.views_to_next == 200
    assert out.eta_days is None  # flat traffic never reaches 5000
    assert out.downgrade_eta_days is None


def test_outlook_projects_rising_article():
    end = date(2026, 9, 27)
    # 23 quiet days then a week at 300/day: the rolling sum keeps climbing
    out = rarity_outlook(daily_series(end, [100] * 23 + [300] * 7), today=end)
    assert out.views_30d == 4400 and out.rarity_30d == "SR"
    # each day adds 300 and drops a 100-day: +200/day -> 5000 after 3 days
    assert out.eta_days == 3


def test_outlook_downgrade_after_spike():
    end = date(2026, 9, 27)
    # a spike 25 days ago keeps the article UR; it leaves the window in 5 days
    values = [50] * 4 + [5000] + [50] * 25
    out = rarity_outlook(daily_series(end, values), today=end)
    assert out.rarity_30d == "UR"
    assert out.downgrade_eta_days == 5


def test_outlook_calendar_month_and_staleness():
    daily = {"20260805": 3000, "20260910": 100}  # 08-05 is outside the window 08-12..09-10
    out = rarity_outlook(daily, today=date(2026, 9, 11))
    assert out.views_prev_month == 3000 and out.rarity_prev_month == "SR"
    assert out.views_30d == 100 and out.rarity_30d == "PC"
    assert rarity_outlook(daily, today=date(2026, 10, 1)) is None
