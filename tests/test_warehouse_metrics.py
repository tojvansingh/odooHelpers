import datetime as dt
from zoneinfo import ZoneInfo

from inventorymgr import warehouse_metrics as wm

PT = ZoneInfo("America/Los_Angeles")
WED = dt.date(2026, 6, 10)  # a Wednesday


def test_last_weekdays_midweek_spans_weekend():
    assert wm.last_weekdays(WED, 5) == [
        dt.date(2026, 6, 4),  # Thu
        dt.date(2026, 6, 5),  # Fri
        dt.date(2026, 6, 8),  # Mon
        dt.date(2026, 6, 9),  # Tue
        WED,
    ]


def test_last_weekdays_on_weekend_ends_friday():
    sunday = dt.date(2026, 6, 7)
    assert wm.last_weekdays(sunday, 5)[-1] == dt.date(2026, 6, 5)
    assert all(d.weekday() < 5 for d in wm.last_weekdays(sunday, 5))


def test_weekdays_elapsed_in_month():
    assert wm.weekdays_elapsed_in_month(WED) == 8  # Jun 1-10 2026: Mon-Fri, Mon-Wed
    assert wm.weekdays_elapsed_in_month(dt.date(2026, 6, 1)) == 1
    # Aug 1 2026 is a Saturday: no weekdays elapsed -> divisor clamps to 1
    assert wm.weekdays_elapsed_in_month(dt.date(2026, 8, 1)) == 1


def test_week_start_is_monday():
    assert wm.week_start(WED) == dt.date(2026, 6, 8)
    assert wm.week_start(dt.date(2026, 6, 8)) == dt.date(2026, 6, 8)


def test_due_bucket_boundaries():
    assert wm.due_bucket(WED - dt.timedelta(days=1), WED) == "Past due"
    assert wm.due_bucket(WED, WED) == "Next 2 weeks"
    assert wm.due_bucket(WED + dt.timedelta(days=13), WED) == "Next 2 weeks"
    assert wm.due_bucket(WED + dt.timedelta(days=14), WED) == "2-4 weeks"
    assert wm.due_bucket(WED + dt.timedelta(days=27), WED) == "2-4 weeks"
    assert wm.due_bucket(WED + dt.timedelta(days=28), WED) == ">4 weeks"


def test_local_date_converts_from_utc():
    # 02:30 UTC on Jun 11 is still Jun 10 in Pacific time
    assert wm.local_date("2026-06-11 02:30:00", PT) == WED
    assert wm.local_date("2026-06-10 12:00:00", PT) == WED


def test_utc_str_is_local_midnight():
    assert wm.utc_str(WED, PT) == "2026-06-10 07:00:00"  # PDT = UTC-7
