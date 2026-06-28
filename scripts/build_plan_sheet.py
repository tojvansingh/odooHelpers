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


def trailing_windows(sales: dict, pids, today: datetime.date) -> dict:
    """Per product, sum units sold over the last 3 and last 6 COMPLETED months (this year)
    and over the same calendar months one year earlier.

    Returns {pid: (sales_3m, sales_3m_last_yr, sales_6m, sales_6m_last_yr)}.
    The current (partial) month is excluded so the comparison is like-for-like.
    """
    def months_back(k: int) -> list[tuple[int, int]]:
        out, yy, mm = [], today.year, today.month
        for _ in range(k):                       # walk back from the previous (last full) month
            mm -= 1
            if mm == 0:
                mm, yy = 12, yy - 1
            out.append((yy, mm))
        return out

    m3, m6 = months_back(3), months_back(6)

    def s(pid: int, months, yr_off: int) -> float:
        d = sales.get(pid, {})
        return sum((d.get(f"{y - yr_off:04d}-{mo:02d}", 0) or 0) for (y, mo) in months)

    return {pid: (s(pid, m3, 0), s(pid, m3, 1), s(pid, m6, 0), s(pid, m6, 1)) for pid in pids}


def parse_class_spec(spec: str, default_collection: str | None) -> tuple[str, str]:
    """'Pillows:Geography' -> ('Pillows','Geography'); 'Pillows:<>Geography' -> ('Pillows','<>Geography');
    'Pillows' -> ('Pillows', default_collection)."""
    if ":" in spec:
        cls, coll = spec.split(":", 1)
        return cls.strip(), coll.strip()
    return spec.strip(), (default_collection or "").strip()


def parse_collection(coll: str) -> tuple[bool, list[str]]:
    """'Geography,Astrology' -> (False, ['Geography','Astrology']);
    '<>Geography,Astrology' -> (True, ['Geography','Astrology']) (exclude all of them)."""
    coll = (coll or "").strip()
    negate = coll.startswith("<>")
    if negate:
        coll = coll[2:]
    return negate, [c.strip() for c in coll.split(",") if c.strip()]


def collection_matches(product_collection: str, negate: bool, names: list[str]) -> bool:
    if not names:
        return True
    hit = (product_collection or "").strip().lower() in {n.lower() for n in names}
    return (not hit) if negate else hit


def class_label(cls: str, negate: bool, names: list[str]) -> str:
    if not names:
        return cls
    joined = ", ".join(names)
    return f"{cls} (not {joined})" if negate else f"{cls} ({joined})"


HELP_DESCRIPTION = """\
Build a customer-aware inventory order plan from Odoo and write it to a review Google Sheet.

For each Class (and optional Collection) it pulls on-hand, open sales orders, ready
receipts and last-year sales from Odoo, projects inventory forward month by month, and
recommends an order quantity (rounded up to the MOQ). One tab per Class; the named sheet
is reused/overwritten on each run, so re-run any time to refresh the numbers.
"""

HELP_EPILOG = """\
examples:
  # one class, all collections, against production
  ... build_plan_sheet.py --prod --class "Dish Towels"

  # per-class collections: a list for one class, an exclusion for another
  ... build_plan_sheet.py --prod --class "Pillows:Geography,Astrology" --class "Dish Towels:<>Holiday"

  # size the horizon to a target arrival date and filter to one vendor
  ... build_plan_sheet.py --prod --class "Pillows" --vendor JKM --arrive 2026-11-15

using the generated sheet (one tab per class):
  Planning columns (A-L):
    On Hand / Outgoing / Incoming  current stock, open SO demand, receipts within the horizon
    Proj End                       projected inventory at the end of the horizon (red if < 0)
    MOQ                            minimum order / reorder multiple for the class+collection
    Recommended                    shortfall rounded up to the MOQ (0 for 'custom' items)
    Order Qty (final)              EDIT THIS — the qty that create_pos.py will order
    Flags                          custom / oos / oos_no_incoming / no_history
  Trend columns (M-R):
    Sales 3M / 6M                  units sold over the last 3 / 6 COMPLETED months (this year)
    3M LY / 6M LY                  units over the same calendar months one year earlier
    3M Δ% / 6M Δ%                   % change vs last year; green if up >=30%, red if down >=30%
                                   (blank when there was no prior-year baseline). Use these to
                                   sanity-check the forecast and nudge Order Qty (final).
  Monthly blocks (after R): Inv (live projection formula), Sales (last-yr forecast),
    Booked-ret, Booked-new, Inc — one column per horizon month.

workflow: run this -> review/edit 'Order Qty (final)' in the sheet ->
  create_pos.py --sheet <KEY> --tab "<Class>" ... to draft the POs in Odoo.
"""


def main() -> None:
    ap = argparse.ArgumentParser(
        description=HELP_DESCRIPTION, epilog=HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--class", dest="classes", action="append",
                    help="Class name, optionally with a per-class collection as 'Class:Collection'. "
                         "Collection may be a comma-separated list ('Pillows:Geography,Astrology') and/or "
                         "negated with a leading <> ('Pillows:<>Geography,Astrology' excludes both); repeatable")
    ap.add_argument("--collection", help="default collection filter for classes lacking their own "
                                         "(comma-separated list; prefix <> to exclude, e.g. '<>Geography,Astrology')")
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
        negate, coll_names = parse_collection(coll)
        # one positive collection -> its param override; else (multiple or negated) class default
        params_coll = coll_names[0] if (not negate and len(coll_names) == 1) else ""
        params = resolve_class_params(params_all, cls, params_coll)
        if params is None:
            raise SystemExit(f"No params for class {cls!r} in data/class_params.csv")
        products = read_products_by_class(client, cls)
        if coll_names:
            products = {pid: p for pid, p in products.items()
                        if collection_matches(p.collection, negate, coll_names)}
        if args.vendor:
            keep = read_product_ids_for_vendor(client, args.vendor)
            products = {pid: p for pid, p in products.items() if pid in keep}
        pids = list(products)
        if not pids:
            raise SystemExit(f"No products for {cls} (collection={coll!r}, vendor={args.vendor})")

        sales = read_monthly_sales(client, pids)
        windows = trailing_windows(sales, pids, today)  # 3M/6M sales vs same period last year
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
        label = class_label(cls, negate, coll_names)
        blocks[label] = (months, results, params.moq_step, windows)
        print(f"{label}: {len(products)} products, {n}-mo horizon")

    title = args.title or "Inventory Plan — " + " + ".join(blocks)
    sh = build_review_spreadsheet(GSheets(), blocks, title)
    print("\nSheet:", title)
    print("URL:", sh.url)


if __name__ == "__main__":
    main()
