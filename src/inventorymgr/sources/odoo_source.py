"""Read planning inputs from Odoo via XML-RPC (the production data source).

Mirrors the user's three manual exports:
  - product stock by Class      -> product.product (class_id filter + stock fields)
  - monthly "Qty Ordered"       -> sale.report read_group by product + month
  - open POs (incoming + dates) -> purchase.order.line (remaining qty + date_planned)
"""

from __future__ import annotations

import datetime

from ..model import Product
from .odoo_client import OdooClient

STOCK_FIELDS = [
    "name", "display_name", "default_code", "class_id", "collection_id",
    "standard_price", "qty_available", "free_qty", "incoming_qty", "outgoing_qty",
]


def _m2o_name(value) -> str:
    return value[1] if value else ""


def _to_product(r: dict) -> Product:
    return Product(
        name=r.get("name") or "",
        display_name=r.get("display_name") or "",
        class_name=_m2o_name(r.get("class_id")),
        collection=_m2o_name(r.get("collection_id")),
        on_hand=r.get("qty_available") or 0,
        incoming=r.get("incoming_qty") or 0,
        outgoing=r.get("outgoing_qty") or 0,
        average_cost=r.get("standard_price") or 0,
    )


def read_products_by_class(client: OdooClient, class_name: str, active_only: bool = True) -> dict[int, Product]:
    domain = [["class_id.name", "=", class_name]]
    if active_only:
        domain.append(["active", "=", True])
    recs = client.search_read("product.product", domain, STOCK_FIELDS)
    return {r["id"]: _to_product(r) for r in recs}


def read_products_by_ids(client: OdooClient, ids) -> dict[int, Product]:
    # Active products only: archived (discontinued) items are excluded from projections.
    if not ids:
        return {}
    recs = client.search_read("product.product", [["id", "in", list(ids)]], STOCK_FIELDS)
    return {r["id"]: _to_product(r) for r in recs}


def _month_key_from_group(row: dict) -> str:
    rng = (row.get("__range") or {}).get("date:month")
    if rng and rng.get("from"):
        return rng["from"][:7]  # 'YYYY-MM'
    # fallback: parse the localized label like "June 2025"
    return datetime.datetime.strptime(row["date:month"], "%B %Y").strftime("%Y-%m")


def read_monthly_sales(
    client: OdooClient, product_ids, states=("sale", "done")
) -> dict[int, dict[str, float]]:
    """Return {product_id: {'YYYY-MM': qty_ordered}} from sale.report."""
    if not product_ids:
        return {}
    domain = [["product_id", "in", list(product_ids)], ["state", "in", list(states)]]
    rows = client.read_group(
        "sale.report", domain, ["product_uom_qty:sum"], ["product_id", "date:month"], lazy=False
    )
    out: dict[int, dict[str, float]] = {}
    for r in rows:
        pid = r["product_id"][0]
        out.setdefault(pid, {})[_month_key_from_group(r)] = r.get("product_uom_qty") or 0
    return out


def read_open_po_remaining(
    client: OdooClient, product_ids, states=("purchase", "done")
) -> dict[int, list[tuple[str | None, float]]]:
    """Return {product_id: [(arrival 'YYYY-MM', remaining_qty), ...]} for not-fully-received PO lines."""
    if not product_ids:
        return {}
    domain = [["product_id", "in", list(product_ids)], ["state", "in", list(states)]]
    rows = client.search_read(
        "purchase.order.line", domain,
        ["product_id", "product_qty", "qty_received", "date_planned"],
    )
    out: dict[int, list[tuple[str | None, float]]] = {}
    for r in rows:
        remaining = (r.get("product_qty") or 0) - (r.get("qty_received") or 0)
        if remaining <= 0:
            continue
        pid = r["product_id"][0]
        dp = r.get("date_planned")
        out.setdefault(pid, []).append((dp[:7] if dp else None, remaining))
    return out
