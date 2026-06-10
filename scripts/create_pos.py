"""Create DRAFT purchase orders in LOCAL Odoo from a plan sheet's "Order Qty (final)".

LOCAL ONLY — hard-refuses to run against anything but localhost. Products are matched
from the sheet by SKU (the [code] in Display Name); each is grouped to its primary
vendor (lowest-sequence product.supplierinfo) and one draft PO is created per vendor.

Run:
  cd odooHelpers && PYTHONPATH=src uv run python scripts/create_pos.py \
      --sheet <KEY> --tab "Dish Towels" [--date-planned 2026-11-15] [--dry-run]
"""

from __future__ import annotations

import argparse
import datetime
import re
from collections import defaultdict

from inventorymgr.sources.gsheets import GSheets
from inventorymgr.sources.odoo_client import OdooClient


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sheet", required=True, help="Google Sheet key")
    ap.add_argument("--tab", required=True, help="worksheet/tab name")
    ap.add_argument("--qty-col", default="Order Qty (final)")
    ap.add_argument("--date-planned", default=None, help="expected arrival YYYY-MM-DD")
    ap.add_argument("--dry-run", action="store_true", help="show what would be created, write nothing")
    args = ap.parse_args()

    c = OdooClient(profile="local")  # writes go to LOCAL only
    if not any(h in c.s.url for h in ("localhost", "127.0.0.1")):
        raise SystemExit(f"Refusing to run: not local Odoo ({c.s.url})")

    ws = GSheets().open_by_key(args.sheet).worksheet(args.tab)
    vals = ws.get_all_values()
    hdr = vals[0]
    qi, di = hdr.index(args.qty_col), hdr.index("Display Name")
    wanted: dict[str, float] = {}
    for row in vals[1:]:
        if not row or len(row) <= max(qi, di):
            continue
        try:
            qty = float(row[qi] or 0)
        except ValueError:
            qty = 0
        m = re.search(r"\[([^\]]+)\]", row[di] or "")
        if qty > 0 and m:
            wanted[m.group(1)] = qty
    if not wanted:
        raise SystemExit("No positive Order Qty (final) rows found.")

    prods = c.search_read(
        "product.product", [["default_code", "in", list(wanted)]],
        ["default_code", "name", "uom_po_id", "standard_price", "product_tmpl_id"],
    )
    by_sku = {p["default_code"]: p for p in prods}
    missing = [s for s in wanted if s not in by_sku]
    tmpl_ids = list({p["product_tmpl_id"][0] for p in prods if p.get("product_tmpl_id")})

    sinfo = c.search_read(
        "product.supplierinfo", [["product_tmpl_id", "in", tmpl_ids]],
        ["product_tmpl_id", "partner_id", "price", "sequence"],
    )
    primary: dict[int, tuple] = {}  # tmpl_id -> (partner_id, name, price)
    for s in sorted(sinfo, key=lambda x: (x.get("sequence") or 0)):
        t = s["product_tmpl_id"][0]
        if t not in primary and s.get("partner_id"):
            primary[t] = (s["partner_id"][0], s["partner_id"][1], s.get("price") or 0)

    groups: dict[tuple, list] = defaultdict(list)
    novendor = []
    for sku, qty in wanted.items():
        p = by_sku.get(sku)
        if not p:
            continue
        t = p["product_tmpl_id"][0] if p.get("product_tmpl_id") else None
        info = primary.get(t)
        if not info:
            novendor.append(sku)
            continue
        partner_id, pname, price = info
        uom = p["uom_po_id"][0] if p.get("uom_po_id") else None
        groups[(partner_id, pname)].append(
            (p["id"], p["name"], qty, uom, price or p.get("standard_price") or 0)
        )

    date_planned = args.date_planned or (datetime.date.today() + datetime.timedelta(days=90)).isoformat()
    print(f"qty>0 rows: {len(wanted)} | matched: {len(by_sku)} | missing SKUs: {len(missing)} | no-vendor: {len(novendor)}")
    for (partner_id, pname), lines in groups.items():
        print(f"  {pname}: {len(lines)} lines, total qty {sum(x[2] for x in lines):.0f}")
        if args.dry_run:
            continue
        order_lines = [
            (0, 0, {"product_id": pid, "name": name, "product_qty": qty,
                    "product_uom": uom, "price_unit": price, "date_planned": f"{date_planned} 00:00:00"})
            for (pid, name, qty, uom, price) in lines
        ]
        po_id = c.execute_kw("purchase.order", "create", [{"partner_id": partner_id, "order_line": order_lines}])
        name = c.search_read("purchase.order", [["id", "=", po_id]], ["name"])[0]["name"]
        print(f"    -> created DRAFT PO {name} (id {po_id})")
    if missing:
        print("  missing SKUs:", missing[:10])
    if novendor:
        print("  no-vendor SKUs:", novendor[:10])


if __name__ == "__main__":
    main()
