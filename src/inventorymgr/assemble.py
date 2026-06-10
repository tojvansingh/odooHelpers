"""Assemble engine inputs from source data and run the plan for a whole Class."""

from __future__ import annotations

from .engine import make_plan
from .model import ClassParams, PlanInput, PlanResult, Product

BASE_BUFFER_MONTHS = 6  # target: 6 months of stock on hand at the moment the new order arrives


def month_key(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def horizon_months(year: int, month: int, n: int) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for _ in range(n):
        out.append((year, month))
        month += 1
        if month > 12:
            month, year = 1, year + 1
    return out


def horizon_length(params: ClassParams, buffer_months: int = BASE_BUFFER_MONTHS) -> int:
    """Months to cover = (lead + transit) to the order's arrival, plus the on-hand buffer."""
    days_to_arrival = (params.lead_days or 0) + (params.transit_days or 0)
    months_to_arrival = round(days_to_arrival / 30)
    return max(1, months_to_arrival + buffer_months)


def build_incoming_index(
    remaining: list[tuple[str | None, float]] | None, months: list[tuple[int, int]]
) -> dict[int, float]:
    """Bucket open-PO remaining qty into horizon-month indices by expected arrival.

    Arrivals before the horizon start fold into index 0; arrivals past the end are ignored.
    """
    index_of = {month_key(y, m): i for i, (y, m) in enumerate(months)}
    first, last = month_key(*months[0]), month_key(*months[-1])
    sched: dict[int, float] = {}
    for arrival, qty in remaining or []:
        if arrival is None or arrival < first:
            i = 0
        elif arrival > last:
            continue
        else:
            i = index_of[arrival]
        sched[i] = sched.get(i, 0) + qty
    return sched


def last_year_key(year: int, month: int) -> str:
    return month_key(year - 1, month)


def classify_booking_month(ship_date: str | None, months: list[tuple[int, int]]) -> int | None:
    """Horizon index a booking lands in by ship date (overdue/before -> 0, beyond end -> None)."""
    first, last = month_key(*months[0]), month_key(*months[-1])
    if not ship_date or ship_date[:7] < first:
        return 0
    if ship_date[:7] > last:
        return None
    return {month_key(y, m): i for i, (y, m) in enumerate(months)}[ship_date[:7]]


def booking_type(ship_date, commercial_pid, buyers_by_month, months) -> str | None:
    """'returning' if the customer bought this product in the same month last year, else 'new'.
    None if it ships beyond the horizon."""
    idx = classify_booking_month(ship_date, months)
    if idx is None:
        return None
    y, m = months[idx]
    returning = commercial_pid is not None and commercial_pid in buyers_by_month.get(last_year_key(y, m), set())
    return "returning" if returning else "new"


def booked_by_month(bookings, buyers_by_month, months) -> tuple[list[float], list[float]]:
    """(booked_returning, booked_new) per horizon month, from open outgoing bookings."""
    n = len(months)
    ret, new = [0.0] * n, [0.0] * n
    for ship_date, qty, commercial_pid in bookings or []:
        idx = classify_booking_month(ship_date, months)
        if idx is None:
            continue
        t = booking_type(ship_date, commercial_pid, buyers_by_month, months)
        (ret if t == "returning" else new)[idx] += qty or 0
    return ret, new


def assemble_plan(
    products: dict[int, Product],
    sales: dict[int, dict[str, float]],
    remaining: dict[int, list[tuple[str | None, float]]],
    params: ClassParams,
    start_year: int,
    start_month: int,
    bookings: dict[int, list] | None = None,
    buyers_by_month: dict[int, dict[str, set]] | None = None,
    horizon_override: int | None = None,
) -> tuple[list[tuple[int, int]], list[tuple[int, PlanResult]]]:
    bookings = bookings or {}
    buyers_by_month = buyers_by_month or {}
    n = horizon_override or horizon_length(params)
    months = horizon_months(start_year, start_month, n)
    results: list[tuple[int, PlanResult]] = []
    for pid, product in products.items():
        product_sales = sales.get(pid, {})
        # Forecast each horizon month from last year's same calendar month.
        forecasts = [product_sales.get(month_key(y - 1, m), 0) for (y, m) in months]
        incoming = build_incoming_index(remaining.get(pid), months)
        bret, bnew = booked_by_month(bookings.get(pid), buyers_by_month.get(pid, {}), months)
        # Customer-aware: start from On Hand; booked demand carries the committed orders.
        pi = PlanInput(
            product, forecasts, incoming,
            booked_returning=bret, booked_new=bnew, starting_inventory=product.on_hand,
        )
        results.append((pid, make_plan(pi, params.moq_step)))
    return months, results
