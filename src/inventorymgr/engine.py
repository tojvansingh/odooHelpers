"""Pure inventory-planning calculations.

Decoded from the user's Google-Sheets workbook (tab "4.2 DT Plan"). Order qty
mirrors the sheet's `=CEILING(MAX(0, -ending_inventory), MOQ)` over a ~12-month
projection that subtracts last-year same-month demand and adds existing incoming
POs in their arrival month.
"""

from __future__ import annotations

import math

from .model import PlanInput, PlanResult, Product

OOS_THRESHOLD = 20  # sheet col K: (on_hand - outgoing) < 20 counts as out of stock


def ceil_to_step(x: float, step: int | None) -> float:
    """Round x UP to the nearest multiple of step (the MOQ increment). <=0 -> 0."""
    if x <= 0:
        return 0
    s = step if step and step > 0 else 1
    return math.ceil(x / s) * s


def starting_available(p: Product) -> float:
    return (p.on_hand or 0) - (p.outgoing or 0)


def project(start: float, forecasts: list[float], incoming_by_index: dict[int, float]) -> list[float]:
    """Roll inventory forward: each month subtract demand, add any incoming that lands that month."""
    inv = start
    series: list[float] = []
    for i, demand in enumerate(forecasts):
        inv = inv - (demand or 0) + (incoming_by_index.get(i, 0) or 0)
        series.append(inv)
    return series


def compute_flags(p: Product, forecasts: list[float]) -> list[str]:
    flags: list[str] = []
    if (p.collection or "").strip().lower() == "custom":
        flags.append("custom")
    avail = starting_available(p)
    if avail < OOS_THRESHOLD:
        flags.append("oos")
    if (p.on_hand or 0) <= (p.outgoing or 0) and ((p.incoming or 0) + avail) <= 0:
        flags.append("oos_no_incoming")
    if sum((f or 0) for f in forecasts) == 0:
        flags.append("no_history")
    return flags


def _at(lst: list[float], i: int) -> float:
    return lst[i] if i < len(lst) else 0


def make_plan(pi: PlanInput, moq_step: int | None) -> PlanResult:
    n = len(pi.forecasts)
    # Customer-aware demand: take the GREATER of forecast and returning-customer bookings
    # (they describe the same demand), then add genuinely-new-customer bookings on top.
    demand = [max(pi.forecasts[i] or 0, _at(pi.booked_returning, i)) + _at(pi.booked_new, i) for i in range(n)]
    start = pi.starting_inventory if pi.starting_inventory is not None else starting_available(pi.product)
    series = project(start, demand, pi.incoming_by_index)
    ending = series[-1] if series else start
    baseline = ceil_to_step(-ending, moq_step)
    flags = compute_flags(pi.product, pi.forecasts)  # no_history is about sales history, not demand
    # Custom items are flagged "do not auto-reorder" — the human sets these manually.
    recommended = 0 if "custom" in flags else baseline
    incoming = [pi.incoming_by_index.get(i, 0) for i in range(n)]
    return PlanResult(
        product=pi.product,
        starting_available=start,
        inventory_series=series,
        ending_inventory=ending,
        baseline_qty=baseline,
        recommended_qty=recommended,
        flags=flags,
        forecasts=list(pi.forecasts),
        incoming=incoming,
        demand=demand,
        booked_returning=[_at(pi.booked_returning, i) for i in range(n)],
        booked_new=[_at(pi.booked_new, i) for i in range(n)],
    )
