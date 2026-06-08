"""AIR-vs-SEA expedite analysis: given a set of PO numbers to potentially air,
figure out how much of each line to air to avoid stockouts by target dates.

Arrival timing of *other* (on-schedule) open POs is read from the Delivery
(stock.picking) scheduled_date, which is treated as accurate.
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


def fetch_po_arrivals(client: OdooClient, po_names) -> dict[str, str]:
    """{po_name: earliest open incoming Delivery scheduled_date 'YYYY-MM-DD'}."""
    pos = client.search_read("purchase.order", [["name", "in", list(po_names)]], ["name", "picking_ids"])
    pick_to_po: dict[int, str] = {}
    for p in pos:
        for pk in p.get("picking_ids") or []:
            pick_to_po[pk] = p["name"]
    arrivals: dict[str, str] = {}
    if pick_to_po:
        picks = client.search_read(
            "stock.picking", [["id", "in", list(pick_to_po)]],
            ["id", "scheduled_date", "state", "picking_type_code"],
        )
        for pk in picks:
            if pk.get("state") in ("done", "cancel"):
                continue
            if (pk.get("picking_type_code") or "incoming") != "incoming":
                continue
            sd = pk.get("scheduled_date")
            if not sd:
                continue
            po, day = pick_to_po[pk["id"]], sd[:10]
            if po not in arrivals or day < arrivals[po]:
                arrivals[po] = day
    return arrivals


def fetch_air_data(client: OdooClient, input_po_names):
    """Returns (air_qty, products, onsched, input_arrivals).

    air_qty: {pid: remaining qty on the input POs (what we could air)}
    products: {pid: Product}
    onsched: {pid: [(arrival 'YYYY-MM-DD'|None, qty)]} for all OTHER open POs
    input_arrivals: {input_po_name: Delivery scheduled date}
    """
    input_po_names = set(input_po_names)
    in_lines = client.search_read(
        "purchase.order.line", [["order_id.name", "in", list(input_po_names)]],
        ["order_id", "product_id", "product_qty", "qty_received"],
    )
    air_qty: dict[int, float] = {}
    for line in in_lines:
        rem = (line["product_qty"] or 0) - (line["qty_received"] or 0)
        if rem > 0:
            air_qty[line["product_id"][0]] = air_qty.get(line["product_id"][0], 0) + rem

    pids = list(air_qty)
    products = read_products_by_ids(client, pids)

    open_lines = client.search_read(
        "purchase.order.line",
        [["product_id", "in", pids], ["state", "in", ["purchase", "done"]]],
        ["order_id", "product_id", "product_qty", "qty_received", "date_planned"],
    )
    all_po_names = {x["order_id"][1] for x in open_lines} | input_po_names
    arrivals = fetch_po_arrivals(client, all_po_names)

    onsched: dict[int, list[tuple[str | None, float]]] = {}
    for line in open_lines:
        po = line["order_id"][1]
        if po in input_po_names:
            continue
        rem = (line["product_qty"] or 0) - (line["qty_received"] or 0)
        if rem <= 0:
            continue
        dp = line.get("date_planned")
        arrival = arrivals.get(po) or (dp[:10] if dp else None)
        onsched.setdefault(line["product_id"][0], []).append((arrival, rem))

    input_arrivals = {po: arrivals.get(po) for po in input_po_names}
    return air_qty, products, onsched, input_arrivals


def onsched_through(onsched_list, cutoff_iso: str) -> float:
    """Sum of on-schedule incoming arriving on/before cutoff (undated counts as arriving)."""
    return sum(q for (arrival, q) in (onsched_list or []) if arrival is None or arrival <= cutoff_iso)
