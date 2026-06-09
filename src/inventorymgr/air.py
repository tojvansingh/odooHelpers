"""AIR-vs-SEA expedite analysis: given PO numbers to potentially air, figure out
how much of each line to air to avoid stockouts by target dates.

"Incoming" supply = receipts in **Ready** state only (stock.move state='assigned',
incoming) — a PO can show unreceived qty while its receipt is cancelled/done, so
PO-line remaining overcounts. Each move is dated by its own scheduled date.
"""

from __future__ import annotations

import datetime

from .sources.odoo_client import OdooClient
from .sources.odoo_source import read_products_by_ids


def last_day_of_month(year: int, month: int) -> datetime.date:
    if month == 12:
        return datetime.date(year, 12, 31)
    return datetime.date(year, month + 1, 1) - datetime.timedelta(days=1)


def add_months(year: int, month: int, n: int) -> tuple[int, int]:
    idx = year * 12 + (month - 1) + n
    return idx // 12, idx % 12 + 1


def month_range(y0: int, m0: int, y1: int, m1: int) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    y, m = y0, m0
    while (y, m) <= (y1, m1):
        out.append((y, m))
        y, m = add_months(y, m, 1)
    return out


def _po_name_by_line(client: OdooClient, line_ids) -> dict[int, str]:
    ids = list({i for i in line_ids if i})
    if not ids:
        return {}
    rows = client.search_read("purchase.order.line", [["id", "in", ids]], ["order_id"])
    return {r["id"]: r["order_id"][1] for r in rows if r.get("order_id")}


def fetch_ready_incoming(client: OdooClient, product_ids) -> list[dict]:
    """Ready (assigned) incoming moves for the products: [{pid, po, date|None, qty}]."""
    if not product_ids:
        return []
    moves = client.search_read(
        "stock.move",
        [["product_id", "in", list(product_ids)],
         ["state", "=", "assigned"],
         ["picking_id.picking_type_code", "=", "incoming"]],
        ["product_id", "product_uom_qty", "date", "purchase_line_id"],
    )
    po_by_line = _po_name_by_line(client, [m["purchase_line_id"][0] for m in moves if m.get("purchase_line_id")])
    rows = []
    for m in moves:
        plid = m["purchase_line_id"][0] if m.get("purchase_line_id") else None
        rows.append({
            "pid": m["product_id"][0],
            "po": po_by_line.get(plid),
            "date": (m["date"] or "")[:10] or None,
            "qty": m["product_uom_qty"] or 0,
        })
    return rows


def fetch_air_data(client: OdooClient, input_po_names):
    """Returns (air_qty, products, onsched, deliveries).

    air_qty: {pid: ready incoming qty on the input POs (what we can air)}
    products: {pid: Product}
    onsched: {pid: [(date|None, qty)]} ready incoming on OTHER POs
    deliveries: [{pid, display_name, po, type('air'|'on-schedule'), date, qty}] (all ready incoming)
    """
    input_po_names = set(input_po_names)
    air_moves = client.search_read(
        "stock.move",
        [["purchase_line_id.order_id.name", "in", list(input_po_names)],
         ["state", "=", "assigned"],
         ["picking_id.picking_type_code", "=", "incoming"]],
        ["product_id"],
    )
    products = read_products_by_ids(client, sorted({m["product_id"][0] for m in air_moves}))
    pids = list(products)  # active only — archived (discontinued) products are excluded

    deliveries: list[dict] = []
    air_qty: dict[int, float] = {}
    onsched: dict[int, list[tuple[str | None, float]]] = {}
    for row in fetch_ready_incoming(client, pids):
        is_air = row["po"] in input_po_names
        deliveries.append({
            "pid": row["pid"],
            "display_name": products[row["pid"]].display_name,
            "po": row["po"],
            "type": "air" if is_air else "on-schedule",
            "date": row["date"],
            "qty": row["qty"],
        })
        if is_air:
            air_qty[row["pid"]] = air_qty.get(row["pid"], 0) + row["qty"]
        else:
            onsched.setdefault(row["pid"], []).append((row["date"], row["qty"]))
    return air_qty, products, onsched, deliveries


def onsched_through(onsched_list, cutoff_iso: str) -> float:
    return sum(q for (d, q) in (onsched_list or []) if d is None or d <= cutoff_iso)
