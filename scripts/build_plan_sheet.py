"""Pull from Odoo, assemble a customer-aware plan, and write a review Google Sheet.

Demand each month = max(last-year forecast, booked from returning customers) + booked
from new customers; projection starts from On Hand. One reused sheet per run.

Run:
  cd odooHelpers && PYTHONPATH=src uv run python scripts/build_plan_sheet.py --prod \
      --class "Dish Towels" [--collection Geography] [--vendor JKM] [--arrive 2026-11-15]
"""

from __future__ import annotations

import argparse
import datetime

from inventorymgr.air import (
    commercial_partner_map,
    fetch_booked_outgoing,
    fetch_lastyear_buyers_by_month,
    fetch_ready_incoming,
    last_day_of_month,
)
from inventorymgr.assemble import assemble_plan, horizon_length, horizon_months
from inventorymgr.config import load_class_params, resolve_class_params
from inventorymgr.sheets_output import build_review_spreadsheet
from inventorymgr.sources.gsheets import GSheets
from inventorymgr.sources.odoo_client import OdooClient
from inventorymgr.sources.odoo_source import (
    read_monthly_sales,
    read_product_ids_for_vendor,
    read_products_by_class,
)


def months_between(a: datetime.date, b: datetime.date) -> int:
    return (b.year - a.year) * 12 + (b.month - a.month)


def parse_class_spec(spec: str, default_collection: str | None) -> tuple[str, str]:
    """'Pillows:Geography' -> ('Pillows','Geography'); 'Pillows:<>Geography' -> ('Pillows','<>Geography');
    'Pillows' -> ('Pillows', default_collection)."""
    if ":" in spec:
        cls, coll = spec.split(":", 1)
        return cls.strip(), coll.strip()
    return spec.strip(), (default_collection or "").strip()


def collection_matches(product_collection: str, coll: str) -> bool:
    pc = (product_collection or "").strip().lower()
    if coll.startswith("<>"):
        return pc != coll[2:].strip().lower()
    return pc == coll.strip().lower()


def class_label(cls: str, coll: str) -> str:
    if coll.startswith("<>"):
        return f"{cls} (not {coll[2:].strip()})"
    return f"{cls} ({coll})" if coll else cls


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--class", dest="classes", action="append",
                    help="Class name, optionally with a per-class collection as 'Class:Collection' "
                         "(e.g. 'Pillows:Geography' or 'Pillows:<>Geography' to exclude); repeatable")
    ap.add_argument("--collection", help="default collection filter for classes lacking their own "
                                         "(prefix <> to exclude, e.g. '<>Geography')")
    ap.add_argument("--vendor", help="filter products to those supplied by this vendor")
    ap.add_argument("--arrive", help="override order arrival date YYYY-MM-DD (sizes horizon = months-to-arrival + 6)")
    ap.add_argument("--title", help="spreadsheet title (reused/overwritten)")
    ap.add_argument("--prod", action="store_true", help="use the production Odoo profile")
    args = ap.parse_args()
    classes = args.classes or ["Dish Towels"]

    client = OdooClient(profile="prod" if args.prod else "local")
    params_all = load_class_params()
    today = datetime.date.today()
    horizon_override = None
    if args.arrive:
        horizon_override = months_between(today, datetime.date.fromisoformat(args.arrive)) + 6

    blocks: dict = {}
    for spec in classes:
        cls, coll = parse_class_spec(spec, args.collection)
        params = resolve_class_params(params_all, cls, "" if coll.startswith("<>") else coll)
        if params is None:
            raise SystemExit(f"No params for class {cls!r} in data/class_params.csv")
        products = read_products_by_class(client, cls)
        if coll:
            products = {pid: p for pid, p in products.items() if collection_matches(p.collection, coll)}
        if args.vendor:
            keep = read_product_ids_for_vendor(client, args.vendor)
            products = {pid: p for pid, p in products.items() if pid in keep}
        pids = list(products)
        if not pids:
            raise SystemExit(f"No products for {cls} (collection={coll!r}, vendor={args.vendor})")

        sales = read_monthly_sales(client, pids)
        # Incoming = Ready receipts only (stock.move state=assigned, incoming), dated by the
        # move's own scheduled date — excludes cancelled/done receipts and stale PO-line dates.
        remaining: dict[int, list] = {}
        for m in fetch_ready_incoming(client, pids):
            remaining.setdefault(m["pid"], []).append((m["date"][:7] if m["date"] else None, m["qty"]))
        n = horizon_override or horizon_length(params)
        months = horizon_months(today.year, today.month, n)
        src_lo, src_hi = (months[0][0] - 1, months[0][1]), (months[-1][0] - 1, months[-1][1])
        buyers = fetch_lastyear_buyers_by_month(
            client, pids, f"{src_lo[0]:04d}-{src_lo[1]:02d}-01", last_day_of_month(*src_hi).isoformat()
        )
        bookings_raw = fetch_booked_outgoing(client, pids)
        cmap = commercial_partner_map(client, {b["partner_id"] for b in bookings_raw if b["partner_id"]})
        bookings_by_pid: dict[int, list] = {}
        for b in bookings_raw:
            cp = cmap.get(b["partner_id"]) if b["partner_id"] else None
            bookings_by_pid.setdefault(b["pid"], []).append((b["date"], b["qty"], cp))

        months, results = assemble_plan(
            products, sales, remaining, params, today.year, today.month,
            bookings=bookings_by_pid, buyers_by_month=buyers, horizon_override=horizon_override,
        )
        label = class_label(cls, coll)
        blocks[label] = (months, results, params.moq_step)
        print(f"{label}: {len(products)} products, {n}-mo horizon")

    title = args.title or "Inventory Plan — " + " + ".join(blocks)
    sh = build_review_spreadsheet(GSheets(), blocks, title)
    print("\nSheet:", title)
    print("URL:", sh.url)


if __name__ == "__main__":
    main()
