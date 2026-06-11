"""Compute warehouse/manufacturing dashboard metrics from Odoo.

Pure date/bucket helpers sit at the top (unit-tested); the fetch_* functions hit
Odoo via the thin XML-RPC client and return plain dicts ready for rendering.

Definitions baked in here:
- Day buckets use the connected user's Odoo timezone (matches the UI).
- Production output = qty_produced on done MOs, bucketed by date_finished.
- Backlog = remaining qty (product_qty - qty_produced) on open MOs, bucketed by
  scheduled start date vs today.
- Shipped/unshipped $ = sale-line subtotal (tax-excluded, after discount)
  prorated by the move's share of the line's total moved qty — kit- and
  backorder-safe, since kit components all point at the same sale line.
- An open delivery move is "waiting on manufacturing" when it spawned an MO that
  isn't done (MTO), or its product has an open MO, or its product has a BOM
  (manufactured item with no MO started yet). Anything else short on stock is
  "waiting on inventory".
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from zoneinfo import ZoneInfo

from .sources.odoo_client import OdooClient

UTC = dt.timezone.utc

# Move/picking states that mean "not yet satisfied / not yet ready".
UNSATISFIED_MOVE_STATES = ("confirmed", "waiting", "partially_available")
OPEN_PICKING_STATES = ("confirmed", "waiting", "assigned")

DUE_BUCKETS = ("Past due", "Next 2 weeks", "2-4 weeks", ">4 weeks")


# ---------- pure date helpers ----------

def local_date(odoo_dt: str, tz: ZoneInfo) -> dt.date:
    """Odoo datetimes are UTC 'YYYY-MM-DD HH:MM:SS' strings; bucket in local tz."""
    naive = dt.datetime.strptime(odoo_dt, "%Y-%m-%d %H:%M:%S")
    return naive.replace(tzinfo=UTC).astimezone(tz).date()


def utc_str(local_day: dt.date, tz: ZoneInfo) -> str:
    """Local midnight of `local_day` as an Odoo-style UTC datetime string."""
    midnight = dt.datetime.combine(local_day, dt.time.min, tzinfo=tz)
    return midnight.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


def last_weekdays(today: dt.date, n: int = 5) -> list[dt.date]:
    """The last n Mon-Fri dates ending at today (or the last weekday before it)."""
    out: list[dt.date] = []
    d = today
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= dt.timedelta(days=1)
    return list(reversed(out))


def weekdays_elapsed_in_month(today: dt.date) -> int:
    days = (dt.date(today.year, today.month, i) for i in range(1, today.day + 1))
    return sum(1 for d in days if d.weekday() < 5) or 1


def week_start(today: dt.date) -> dt.date:
    return today - dt.timedelta(days=today.weekday())


def due_bucket(day: dt.date, today: dt.date) -> str:
    delta = (day - today).days
    if delta < 0:
        return DUE_BUCKETS[0]
    if delta < 14:
        return DUE_BUCKETS[1]
    if delta < 28:
        return DUE_BUCKETS[2]
    return DUE_BUCKETS[3]


def _m2o_id(value) -> int | None:
    return value[0] if value else None


def _m2o_name(value) -> str:
    return value[1] if value else ""


# ---------- Odoo fetchers ----------

def outgoing_picking_type_ids(client: OdooClient, exclude_warehouse_substr: str = "FBA") -> list[int]:
    """Active customer-delivery operation types, excluding Amazon-fulfilled warehouses."""
    types = client.search_read("stock.picking.type", [["code", "=", "outgoing"]], ["warehouse_id"])
    return [t["id"] for t in types if exclude_warehouse_substr not in _m2o_name(t.get("warehouse_id"))]


def _classes_for(client: OdooClient, product_ids) -> dict[int, str]:
    if not product_ids:
        return {}
    recs = client.search_read(
        "product.product", [["id", "in", list(product_ids)]], ["class_id"],
        context={"active_test": False},
    )
    return {r["id"]: _m2o_name(r.get("class_id")) or "(no class)" for r in recs}


def fetch_production_output(
    client: OdooClient, tz: ZoneInfo, since_day: dt.date
) -> dict[str, dict[dt.date, float]]:
    """{class: {local finish date: units finished}} for in-house done MOs since since_day."""
    rows = client.search_read(
        "mrp.production",
        [["state", "=", "done"], ["date_finished", ">=", utc_str(since_day, tz)],
         ["picking_type_id.name", "not ilike", "subcontract"]],
        ["product_id", "qty_produced", "date_finished"],
    )
    classes = _classes_for(client, {_m2o_id(r["product_id"]) for r in rows})
    out: dict[str, dict[dt.date, float]] = defaultdict(lambda: defaultdict(float))
    for r in rows:
        day = local_date(r["date_finished"], tz)
        out[classes[_m2o_id(r["product_id"])]][day] += r.get("qty_produced") or 0
    return out


def fetch_production_backlog(
    client: OdooClient, tz: ZoneInfo, today: dt.date
) -> tuple[dict[str, dict[str, float]], dict[str, dict[str, float]]]:
    """(in_house, subcontracted) — each {class: {due bucket: units still to make}}
    over open (incl. draft) MOs, split by the MO's operation type."""
    rows = client.search_read(
        "mrp.production",
        [["state", "not in", ["done", "cancel"]]],
        ["product_id", "product_qty", "qty_produced", "date_start", "picking_type_id"],
    )
    classes = _classes_for(client, {_m2o_id(r["product_id"]) for r in rows})
    in_house: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    subcontracted: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for r in rows:
        remaining = max((r.get("product_qty") or 0) - (r.get("qty_produced") or 0), 0)
        if not remaining:
            continue
        start = local_date(r["date_start"], tz) if r.get("date_start") else today
        dest = subcontracted if "subcontract" in _m2o_name(r.get("picking_type_id")).lower() else in_house
        dest[classes[_m2o_id(r["product_id"])]][due_bucket(start, today)] += remaining
    return in_house, subcontracted


def _move_value_fn(client: OdooClient, moves: list[dict]):
    """Return value(move, qty) -> $ : the move's sale line subtotal prorated by qty.

    Denominator is the line's total demand across all its non-cancelled moves, so
    kit components (which share one sale line) and backorder splits sum to at most
    the line subtotal instead of multiple times it.
    """
    line_ids = {_m2o_id(m["sale_line_id"]) for m in moves if m.get("sale_line_id")}
    if not line_ids:
        return lambda move, qty: 0.0
    lines = client.search_read("sale.order.line", [["id", "in", list(line_ids)]], ["price_subtotal"])
    subtotal = {l["id"]: l.get("price_subtotal") or 0 for l in lines}
    all_moves = client.search_read(
        "stock.move",
        [["sale_line_id", "in", list(line_ids)], ["state", "!=", "cancel"]],
        ["sale_line_id", "product_uom_qty"],
    )
    line_demand: dict[int, float] = defaultdict(float)
    for m in all_moves:
        line_demand[_m2o_id(m["sale_line_id"])] += m.get("product_uom_qty") or 0

    def value(move: dict, qty: float) -> float:
        lid = _m2o_id(move.get("sale_line_id"))
        if not lid or not line_demand.get(lid):
            return 0.0
        return subtotal.get(lid, 0.0) * qty / line_demand[lid]

    return value


def fetch_shipments(
    client: OdooClient, tz: ZoneInfo, ptype_ids: list[int],
    since_day: dt.date, retail_sources: set[str],
) -> dict[str, dict[dt.date, tuple[set[int], float]]]:
    """{'Retail'|'Wholesale': {local ship date: (sale order ids, $ shipped)}}."""
    picks = client.search_read(
        "stock.picking",
        [["picking_type_id", "in", ptype_ids], ["state", "=", "done"],
         ["sale_id", "!=", False], ["date_done", ">=", utc_str(since_day, tz)]],
        ["sale_id", "date_done"],
    )
    pick_day = {p["id"]: local_date(p["date_done"], tz) for p in picks}
    pick_order = {p["id"]: _m2o_id(p["sale_id"]) for p in picks}
    orders = client.search_read(
        "sale.order", [["id", "in", list(set(pick_order.values()))]], ["order_source"],
    )
    channel_of = {
        o["id"]: "Retail" if _m2o_name(o.get("order_source")) in retail_sources else "Wholesale"
        for o in orders
    }
    moves = client.search_read(
        "stock.move",
        [["picking_id", "in", list(pick_day)], ["state", "=", "done"]],
        ["picking_id", "quantity", "sale_line_id"],
    )
    value_of = _move_value_fn(client, moves)
    out: dict[str, dict[dt.date, tuple[set[int], float]]] = {
        "Retail": defaultdict(lambda: (set(), 0.0)),
        "Wholesale": defaultdict(lambda: (set(), 0.0)),
    }
    for pid, day in pick_day.items():
        oid = pick_order[pid]
        chan = channel_of.get(oid, "Wholesale")
        ids, val = out[chan][day]
        ids.add(oid)
        out[chan][day] = (ids, val)
    for m in moves:
        pid = _m2o_id(m["picking_id"])
        chan = channel_of.get(pick_order[pid], "Wholesale")
        ids, val = out[chan][pick_day[pid]]
        out[chan][pick_day[pid]] = (ids, val + value_of(m, m.get("quantity") or 0))
    return out


def fetch_open_exceptions(
    client: OdooClient, tz: ZoneInfo, ptype_ids: list[int], today: dt.date
) -> dict[str, dict[str, float]]:
    """Open customer deliveries due today or earlier, rolled up per sale order.

    Returns {bucket: {'orders': n, 'total': $ order totals, 'unshipped': $ not yet shipped}}
    for buckets: due_mfg (#5), late (#6), late_mfg (#7a), late_inv (#7b), late_ready.
    """
    tomorrow_utc = utc_str(today + dt.timedelta(days=1), tz)
    today_utc = utc_str(today, tz)
    picks = client.search_read(
        "stock.picking",
        [["picking_type_id", "in", ptype_ids], ["state", "in", list(OPEN_PICKING_STATES)],
         ["sale_id", "!=", False], ["scheduled_date", "<", tomorrow_utc]],
        ["sale_id", "scheduled_date"],
    )
    if not picks:
        return {k: {"orders": 0, "total": 0.0, "unshipped": 0.0}
                for k in ("due_mfg", "late", "late_mfg", "late_inv", "late_ready")}
    pick_order = {p["id"]: _m2o_id(p["sale_id"]) for p in picks}
    moves = client.search_read(
        "stock.move",
        [["picking_id", "in", list(pick_order)], ["state", "not in", ["done", "cancel"]]],
        ["picking_id", "product_id", "product_uom_qty", "state",
         "sale_line_id", "created_production_id"],
    )
    value_of = _move_value_fn(client, moves)

    # Manufacturing signals: a direct MTO link, an open MO for the product, or a BOM.
    mo_ids = {_m2o_id(m["created_production_id"]) for m in moves if m.get("created_production_id")}
    mo_state = {r["id"]: r["state"] for r in client.search_read(
        "mrp.production", [["id", "in", list(mo_ids)]], ["state"])} if mo_ids else {}
    open_mo_pids = {
        _m2o_id(g["product_id"]) for g in client.read_group(
            "mrp.production", [["state", "not in", ["done", "cancel"]]],
            ["product_qty:sum"], ["product_id"], lazy=False)
    }
    unsat_pids = {_m2o_id(m["product_id"]) for m in moves if m["state"] in UNSATISFIED_MOVE_STATES}
    prods = client.search_read(
        "product.product", [["id", "in", list(unsat_pids)]], ["product_tmpl_id"],
        context={"active_test": False},
    ) if unsat_pids else []
    tmpl_of = {p["id"]: _m2o_id(p["product_tmpl_id"]) for p in prods}
    bom_tmpls = {
        _m2o_id(b["product_tmpl_id"]) for b in client.search_read(
            "mrp.bom", [["product_tmpl_id", "in", list(set(tmpl_of.values()))]], ["product_tmpl_id"])
    } if tmpl_of else set()

    def blocked_by_mfg(move: dict) -> bool:
        mo = _m2o_id(move.get("created_production_id"))
        if mo and mo_state.get(mo) not in ("done", "cancel", None):
            return True
        pid = _m2o_id(move["product_id"])
        return pid in open_mo_pids or tmpl_of.get(pid) in bom_tmpls

    # Per-order rollup across that order's open pickings.
    orders: dict[int, dict] = defaultdict(
        lambda: {"late": False, "mfg": False, "inv": False, "unshipped": 0.0})
    for p in picks:
        o = orders[pick_order[p["id"]]]
        o["late"] = o["late"] or (p["scheduled_date"] < today_utc)
    for m in moves:
        o = orders[pick_order[_m2o_id(m["picking_id"])]]
        o["unshipped"] += value_of(m, m.get("product_uom_qty") or 0)
        if m["state"] in UNSATISFIED_MOVE_STATES:
            o["mfg" if blocked_by_mfg(m) else "inv"] = True
    totals = {r["id"]: r.get("amount_total") or 0 for r in client.search_read(
        "sale.order", [["id", "in", list(orders)]], ["amount_total"])}

    def rollup(order_ids) -> dict[str, float]:
        return {
            "orders": len(order_ids),
            "total": sum(totals.get(i, 0) for i in order_ids),
            "unshipped": sum(orders[i]["unshipped"] for i in order_ids),
        }

    late = [i for i, o in orders.items() if o["late"]]
    return {
        "due_mfg": rollup([i for i, o in orders.items() if o["mfg"]]),
        "late": rollup(late),
        "late_mfg": rollup([i for i in late if orders[i]["mfg"]]),
        "late_inv": rollup([i for i in late if not orders[i]["mfg"] and orders[i]["inv"]]),
        "late_ready": rollup([i for i in late if not orders[i]["mfg"] and not orders[i]["inv"]]),
    }
