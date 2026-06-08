"""Generate a plan for one Class from live Odoo and print it (eyeball / sanity check).

Run: cd inventorymgr && PYTHONPATH=src uv run python scripts/plan_class.py --class "Dish Towels"
"""

from __future__ import annotations

import argparse
import datetime

from inventorymgr.assemble import assemble_plan
from inventorymgr.config import load_class_params
from inventorymgr.sources.odoo_client import OdooClient
from inventorymgr.sources.odoo_source import (
    read_monthly_sales,
    read_open_po_remaining,
    read_products_by_class,
)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--class", dest="class_name", default="Dish Towels")
    ap.add_argument("--limit", type=int, default=0, help="cap products (debug)")
    ap.add_argument("--all", action="store_true", help="print every row, not just orders/flags")
    ap.add_argument("--prod", action="store_true", help="use the production Odoo profile")
    args = ap.parse_args()

    client = OdooClient(profile="prod" if args.prod else "local")
    params = load_class_params().get(args.class_name)
    if params is None:
        raise SystemExit(f"No params for class {args.class_name!r} in data/class_params.csv")

    products = read_products_by_class(client, args.class_name)
    if args.limit:
        products = dict(list(products.items())[: args.limit])
    pids = list(products)
    sales = read_monthly_sales(client, pids)
    remaining = read_open_po_remaining(client, pids)

    today = datetime.date.today()
    months, results = assemble_plan(products, sales, remaining, params, today.year, today.month)

    print(
        f"Class={args.class_name!r}  products={len(products)}  "
        f"horizon={len(months)}mo ({months[0][0]}-{months[0][1]:02d}..{months[-1][0]}-{months[-1][1]:02d})  "
        f"lead={params.lead_days} transit={params.transit_days} MOQ={params.moq_step}"
    )
    print(f"{'PRODUCT':40} {'OnHand':>7} {'Out':>5} {'Inc':>6} {'End':>7} {'Order':>6}  flags")
    total = 0.0
    for _pid, res in sorted(results, key=lambda kr: kr[1].recommended_qty, reverse=True):
        total += res.recommended_qty
        if args.all or res.recommended_qty > 0 or res.flags:
            p = res.product
            print(
                f"{(p.display_name or p.name)[:40]:40} {p.on_hand:7.0f} {p.outgoing:5.0f} "
                f"{p.incoming:6.0f} {res.ending_inventory:7.0f} {res.recommended_qty:6.0f}  "
                f"{','.join(res.flags)}"
            )
    print(f"\nTOTAL recommended order qty: {total:.0f}")


if __name__ == "__main__":
    main()
