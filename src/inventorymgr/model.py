from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ClassParams:
    """Per-product-Class planning parameters (lead time, transit, MOQ rounding)."""

    class_name: str
    lead_days: int | None = None
    transit_days: int | None = None
    moq_step: int | None = None


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
    """Everything the engine needs to plan one product."""

    product: Product
    forecasts: list[float]  # demand per horizon month (last-year same-calendar-month units)
    incoming_by_index: dict[int, float] = field(default_factory=dict)  # horizon step -> qty arriving


@dataclass
class PlanResult:
    product: Product
    starting_available: float
    inventory_series: list[float]  # projected month-end inventory across the horizon
    ending_inventory: float
    baseline_qty: float  # mechanical order qty from the formula
    recommended_qty: float  # after auto-rules (Custom -> 0)
    flags: list[str]
    forecasts: list[float] = field(default_factory=list)  # demand per horizon month
    incoming: list[float] = field(default_factory=list)  # PO arrivals per horizon month
