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


def assemble_plan(
    products: dict[int, Product],
    sales: dict[int, dict[str, float]],
    remaining: dict[int, list[tuple[str | None, float]]],
    params: ClassParams,
    start_year: int,
    start_month: int,
) -> tuple[list[tuple[int, int]], list[tuple[int, PlanResult]]]:
    n = horizon_length(params)
    months = horizon_months(start_year, start_month, n)
    results: list[tuple[int, PlanResult]] = []
    for pid, product in products.items():
        product_sales = sales.get(pid, {})
        # Forecast each horizon month from last year's same calendar month.
        forecasts = [product_sales.get(month_key(y - 1, m), 0) for (y, m) in months]
        incoming = build_incoming_index(remaining.get(pid), months)
        results.append((pid, make_plan(PlanInput(product, forecasts, incoming), params.moq_step)))
    return months, results
