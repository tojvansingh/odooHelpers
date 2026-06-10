from inventorymgr.assemble import (
    booked_by_month,
    build_incoming_index,
    horizon_length,
    horizon_months,
    month_key,
)
from inventorymgr.model import ClassParams


def test_booked_by_month_returning_vs_new():
    months = horizon_months(2026, 6, 4)  # 2026-06 .. 2026-09
    buyers = {"2025-06": {100}, "2025-07": {100}}  # last-year buyers per month (commercial ids)
    bookings = [
        ("2026-06-10", 50, 100),  # returning (bought 2025-06)
        ("2026-06-15", 20, 200),  # new
        ("2026-07-01", 30, 100),  # returning (2025-07)
        ("2026-12-01", 99, 100),  # beyond horizon -> ignored
        (None, 10, 100),          # undated -> month 0; returning
    ]
    ret, new = booked_by_month(bookings, buyers, months)
    assert ret == [60, 30, 0, 0]
    assert new == [20, 0, 0, 0]


def test_month_key():
    assert month_key(2026, 6) == "2026-06"


def test_horizon_wraps_year():
    assert horizon_months(2026, 11, 4) == [(2026, 11), (2026, 12), (2027, 1), (2027, 2)]


def test_horizon_length_dish_towels():
    # lead 150 + transit 0 -> ~5 months to arrival + 6-month buffer = 11
    assert horizon_length(ClassParams("Dish Towels", lead_days=150, transit_days=0, moq_step=50)) == 11
    # add 60-day transit -> 7 + 6 = 13
    assert horizon_length(ClassParams("Dish Towels", lead_days=150, transit_days=60, moq_step=50)) == 13


def test_incoming_buckets_by_arrival_month():
    months = horizon_months(2026, 6, 11)  # 2026-06 .. 2027-04
    remaining = [("2026-06", 100), ("2026-08", 50), ("2025-01", 30), ("2030-01", 999)]
    sched = build_incoming_index(remaining, months)
    assert sched[0] == 130   # June arrival + pre-horizon arrival both fold into index 0
    assert sched[2] == 50    # August = index 2
    assert 999 not in sched.values()  # beyond the horizon end is ignored
