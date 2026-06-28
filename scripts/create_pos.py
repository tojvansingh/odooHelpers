"""Create DRAFT purchase orders in LOCAL Odoo from a plan sheet's "Order Qty (final)".

LOCAL ONLY — hard-refuses any non-localhost Odoo.

Vendor resolution: every line goes to --vendor, EXCEPT SKUs listed in
data/vendor_exceptions.csv (sku,vendor) which override. (If --vendor is omitted,
falls back to each product's primary product.supplierinfo vendor.)

--tab is repeatable: rows from every named tab are merged before grouping, so several
classes for the same --vendor collapse into a single PO (Odoo POs have one vendor each,
so differing vendors still split into one PO per vendor). Same SKU on two tabs -> summed.

Run:
  cd odooHelpers && PYTHONPATH=src uv run python scripts/create_pos.py --sheet <KEY> \
      --tab "Dish Towels" --tab "Pillows" --tab "Glassware" \
      --vendor "Orchid Overseas" --date-planned 2026-11-15 [--dry-run]
"""

from __future__ import annotations

import argparse
import csv
import datetime
import pathlib
import re
from collections import defaultdict

from inventorymgr.sources.gsheets import GSheets
from inventorymgr.sources.odoo_client import OdooClient

EXCEPTIONS_CSV = pathlib.Path(__file__).resolve().parents[1] / "data" / "vendor_exceptions.csv"


def load_exceptions() -> dict[str, str]:
    out: dict[str, str] = {}
    if EXCEPTIONS_CSV.exists():
        for row in csv.DictReader(EXCEPTIONS_CSV.open()):
            sku, vendor = (row.get("sku") or "").strip(), (row.get("vendor") or "").strip()
            if sku and vendor:
                out[sku] = vendor
    return out


def resolve_vendor(c: OdooClient, name: str, cache: dict) -> tuple | None:
    if name not in cache:
        hits = c.search_read("res.partner", [["name", "ilike", name]], ["name", "supplier_rank"])
        hits.sort(key=lambda p: -(p.get("supplier_rank") or 0))
        cache[name] = (hits[0]["id"], hits[0]["name"]) if hits else None
    return cache[name]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sheet", required=True)
    ap.add_argument("--tab", dest="tabs", action="append", required=True,
                    help="class tab to read (repeatable; rows from all tabs are merged into the PO(s))")
    ap.add_argument("--vendor", help="default vendor for all lines (else supplierinfo primary)")
    ap.add_argument("--qty-col", default="Order Qty (final)")
    ap.add_argument("--date-planned", default=None)
    ap.add_argument("--prod", action="store_true", help="create in PRODUCTION (default: local)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    c = OdooClient(profile="prod" if args.prod else "local")
    is_local = any(h in c.s.url for h in ("localhost", "127.0.0.1"))
    if not args.prod and not is_local:
        raise SystemExit(f"Refusing: local profile is not localhost ({c.s.url})")
    if args.prod:
        print(f"*** PRODUCTION write target: {c.s.url} ***")

    sh = GSheets().open_by_key(args.sheet)
    wanted: dict[str, float] = {}  # SKU -> qty, summed across all tabs (same SKU on 2 tabs -> combined)
    for tab in args.tabs:
        vals = sh.worksheet(tab).get_all_values()
        hdr = vals[0]
        qi, di = hdr.index(args.qty_col), hdr.index("Display Name")
        n = 0
        for row in vals[1:]:
            if not row or len(row) <= max(qi, di):
                continue
            try:
                qty = float(row[qi] or 0)
            except ValueError:
                qty = 0
            m = re.search(r"\[([^\]]+)\]", row[di] or "")
            if qty > 0 and m:
                wanted[m.group(1)] = wanted.get(m.group(1), 0) + qty
                n += 1
        print(f"  tab {tab!r}: {n} order rows")
    if not wanted:
        raise SystemExit("No positive Order Qty (final) rows found.")

    prods = c.search_read(
        "product.product", [["default_code", "in", list(wanted)]],
        ["default_code", "name", "uom_po_id", "standard_price", "product_tmpl_id"],
    )
    by_sku = {p["default_code"]: p for p in prods}
    missing = [s for s in wanted if s not in by_sku]
    tmpl_ids = list({p["product_tmpl_id"][0] for p in prods if p.get("product_tmpl_id")})

    sinfo = c.search_read("product.supplierinfo", [["product_tmpl_id", "in", tmpl_ids]],
                          ["product_tmpl_id", "partner_id", "price", "sequence"])
    primary: dict[int, tuple] = {}
    price_by: dict[tuple, float] = {}
    for s in sorted(sinfo, key=lambda x: (x.get("sequence") or 0)):
        if not s.get("partner_id"):
            continue
        t = s["product_tmpl_id"][0]
        primary.setdefault(t, (s["partner_id"][0], s["partner_id"][1], s.get("price") or 0))
        price_by.setdefault((t, s["partner_id"][0]), s.get("price") or 0)

    exceptions = load_exceptions()
    vcache: dict = {}
    groups: dict[tuple, list] = defaultdict(list)
    novendor = []
    for sku, qty in wanted.items():
        p = by_sku.get(sku)
        if not p:
            continue
        t = p["product_tmpl_id"][0] if p.get("product_tmpl_id") else None
        vname = exceptions.get(sku) or args.vendor
        if vname:
            v = resolve_vendor(c, vname, vcache)
            if not v:
                novendor.append(f"{sku}(vendor '{vname}' not found)")
                continue
            partner_id, pname = v
            price = price_by.get((t, partner_id)) or p.get("standard_price") or 0
        elif t in primary:
            partner_id, pname, price = primary[t]
        else:
            novendor.append(sku)
            continue
        uom = p["uom_po_id"][0] if p.get("uom_po_id") else None
        groups[(partner_id, pname)].append((p["id"], p["name"], qty, uom, price))

    date_planned = args.date_planned or (datetime.date.today() + datetime.timedelta(days=90)).isoformat()
    print(f"qty>0: {len(wanted)} | matched: {len(by_sku)} | missing: {len(missing)} | "
          f"exceptions applied: {sum(1 for s in wanted if s in exceptions)} | unresolved: {len(novendor)}")
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
        print("  unresolved:", novendor[:10])


if __name__ == "__main__":
    main()
