from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ClassParams:
    """Per-Class (optionally per-Collection) planning parameters (lead, transit, MOQ)."""

    class_name: str
    lead_days: int | None = None
    transit_days: int | None = None
    moq_step: int | None = None
    collection: str = ""  # "" = default for the class; else a collection-specific override


@dataclass
class Product:
    name: str
    display_name: str  # "[SKU] NAME"
    class_name: str
    collection: str
    on_hand: float
    incoming: float
    outgoing: float
    average_cost: float = 0.0


@dataclass
class PlanInput:
    """Everything the engine needs to plan one product.

    Demand per month = max(forecast, booked_returning) + booked_new (customer-aware,
    avoids double-counting Outgoing). booked_* default to zero (naive forecast-only).
    starting_inventory defaults to On Hand - Outgoing when None (the legacy model);
    the live plan passes On Hand and lets booked demand carry the committed orders.
    """

    product: Product
    forecasts: list[float]  # last-year same-calendar-month units
    incoming_by_index: dict[int, float] = field(default_factory=dict)  # horizon step -> qty arriving
    booked_returning: list[float] = field(default_factory=list)  # open orders shipping each month, returning customers
    booked_new: list[float] = field(default_factory=list)  # ... new customers (incremental demand)
    starting_inventory: float | None = None


@dataclass
class PlanResult:
    product: Product
    starting_available: float
    inventory_series: list[float]  # projected month-end inventory across the horizon
    ending_inventory: float
    baseline_qty: float  # mechanical order qty from the formula
    recommended_qty: float  # after auto-rules (Custom -> 0)
    flags: list[str]
    forecasts: list[float] = field(default_factory=list)  # last-year demand per horizon month
    incoming: list[float] = field(default_factory=list)  # PO arrivals per horizon month
    demand: list[float] = field(default_factory=list)  # consumed demand per month (max(forecast,ret)+new)
    booked_returning: list[float] = field(default_factory=list)
    booked_new: list[float] = field(default_factory=list)
