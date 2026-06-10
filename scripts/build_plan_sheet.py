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
    last_day_of_month,
)
from inventorymgr.assemble import assemble_plan, horizon_length, horizon_months
from inventorymgr.config import load_class_params
from inventorymgr.sheets_output import build_review_spreadsheet
from inventorymgr.sources.gsheets import GSheets
from inventorymgr.sources.odoo_client import OdooClient
from inventorymgr.sources.odoo_source import (
    read_monthly_sales,
    read_open_po_remaining,
    read_product_ids_for_vendor,
    read_products_by_class,
)


def months_between(a: datetime.date, b: datetime.date) -> int:
    return (b.year - a.year) * 12 + (b.month - a.month)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--class", dest="classes", action="append", help="Class name (repeatable)")
    ap.add_argument("--collection", help="filter products to this collection")
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
    for cls in classes:
        params = params_all.get(cls)
        if params is None:
            raise SystemExit(f"No params for class {cls!r} in data/class_params.csv")
        products = read_products_by_class(client, cls)
        if args.collection:
            products = {pid: p for pid, p in products.items()
                        if (p.collection or "").strip().lower() == args.collection.strip().lower()}
        if args.vendor:
            keep = read_product_ids_for_vendor(client, args.vendor)
            products = {pid: p for pid, p in products.items() if pid in keep}
        pids = list(products)
        if not pids:
            raise SystemExit(f"No products for {cls} (collection={args.collection}, vendor={args.vendor})")

        sales = read_monthly_sales(client, pids)
        remaining = read_open_po_remaining(client, pids)
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
        blocks[cls] = (months, results, params.moq_step)
        print(f"{cls}: {len(products)} products, {n}-mo horizon")

    title = args.title or "Inventory Plan — " + "+".join(classes) + (f" ({args.collection})" if args.collection else "")
    sh = build_review_spreadsheet(GSheets(), blocks, title)
    print("\nSheet:", title)
    print("URL:", sh.url)


if __name__ == "__main__":
    main()
