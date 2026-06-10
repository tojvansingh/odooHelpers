"""Split a PO's incoming receipt into an AIR receipt and a SEA receipt with separate
scheduled dates, so air-freighted and sea-freighted quantities can be tracked apart.

LOCAL ONLY — hard-refuses any non-localhost Odoo.

Air quantities come either from --air "SKU:qty,SKU:qty" or from an AIR sheet column.

Run:
  cd odooHelpers && PYTHONPATH=src uv run python scripts/split_receipt.py \
      --po P60165 --air "001D:100,003D:200" --air-date 2026-08-15 --sea-date 2026-11-15 [--dry-run]
  # or from the AIR sheet:
      --po P60165 --air-sheet <KEY> --air-col "AIR Aug-31" --air-date ... --sea-date ...
"""

from __future__ import annotations

import argparse
import re

from inventorymgr.sources.gsheets import GSheets
from inventorymgr.sources.odoo_client import OdooClient


def air_from_sheet(key: str, col: str) -> dict[str, float]:
    ws = GSheets().open_by_key(key).worksheet("AIR vs SEA")
    vals = ws.get_all_values()
    hdr = vals[0]
    ai, di = hdr.index(col), hdr.index("Display Name")
    out: dict[str, float] = {}
    for row in vals[1:]:
        if not row or len(row) <= max(ai, di):
            continue
        try:
            q = float(row[ai] or 0)
        except ValueError:
            q = 0
        m = re.search(r"\[([^\]]+)\]", row[di] or "")
        if q > 0 and m:
            out[m.group(1)] = q
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--po", required=True)
    ap.add_argument("--air", help='"SKU:qty,SKU:qty"')
    ap.add_argument("--air-sheet", help="AIR sheet key (reads the AIR vs SEA tab)")
    ap.add_argument("--air-col", default="AIR Aug-31")
    ap.add_argument("--air-date", required=True, help="YYYY-MM-DD")
    ap.add_argument("--sea-date", required=True, help="YYYY-MM-DD")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    c = OdooClient(profile="local")
    if not any(h in c.s.url for h in ("localhost", "127.0.0.1")):
        raise SystemExit(f"Refusing to run: not local Odoo ({c.s.url})")

    if args.air:
        air_by_sku = {p.split(":")[0].strip(): float(p.split(":")[1]) for p in args.air.split(",")}
    elif args.air_sheet:
        air_by_sku = air_from_sheet(args.air_sheet, args.air_col)
    else:
        raise SystemExit("Provide --air or --air-sheet")

    po = c.search_read("purchase.order", [["name", "=", args.po]],
                       ["picking_ids", "group_id", "partner_id"])
    if not po:
        raise SystemExit(f"PO {args.po} not found")
    po = po[0]
    picks = c.search_read(
        "stock.picking",
        [["id", "in", po["picking_ids"]], ["state", "not in", ["done", "cancel"]],
         ["picking_type_code", "=", "incoming"]],
        ["name", "picking_type_id", "location_id", "location_dest_id", "scheduled_date"],
    )
    if not picks:
        raise SystemExit(f"No open incoming receipt on {args.po}")
    pick = picks[0]
    moves = c.search_read("stock.move", [["picking_id", "=", pick["id"]], ["state", "not in", ["done", "cancel"]]],
                          ["product_id", "product_uom_qty"])
    sku_of = {p["id"]: p["default_code"] for p in c.search_read(
        "product.product", [["id", "in", [m["product_id"][0] for m in moves]]], ["default_code"])}

    splits = []  # (move_id, name, air, sea)
    for m in moves:
        a = air_by_sku.get(sku_of.get(m["product_id"][0]), 0)
        if a <= 0:
            continue
        a = min(a, m["product_uom_qty"])
        splits.append((m["id"], m["product_id"][1], a, m["product_uom_qty"] - a))

    print(f"{args.po} receipt {pick['name']}: splitting {len(splits)} of {len(moves)} lines")
    for _, name, a, sea in splits:
        print(f"  {name[:34]:34} AIR {a:.0f} / SEA {sea:.0f}")
    if args.dry_run or not splits:
        return

    air_pick = c.execute_kw("stock.picking", "create", [{
        "picking_type_id": pick["picking_type_id"][0],
        "location_id": pick["location_id"][0],
        "location_dest_id": pick["location_dest_id"][0],
        "partner_id": po["partner_id"][0] if po.get("partner_id") else False,
        "origin": f"{args.po} (AIR)",
        "group_id": po["group_id"][0] if po.get("group_id") else False,
        "scheduled_date": f"{args.air_date} 00:00:00",
    }])
    for mid, _, a, sea in splits:
        if sea <= 0:
            c.execute_kw("stock.move", "write", [[mid], {"picking_id": air_pick, "date": f"{args.air_date} 00:00:00"}])
        else:
            c.execute_kw("stock.move", "copy", [mid, {
                "picking_id": air_pick, "product_uom_qty": a, "quantity": 0,
                "date": f"{args.air_date} 00:00:00",
            }])
            c.execute_kw("stock.move", "write", [[mid], {"product_uom_qty": sea, "quantity": 0, "date": f"{args.sea_date} 00:00:00"}])

    c.execute_kw("stock.picking", "write", [[pick["id"]], {"scheduled_date": f"{args.sea_date} 00:00:00"}])
    c.execute_kw("stock.picking", "action_confirm", [[air_pick]])
    c.execute_kw("stock.picking", "action_assign", [[pick["id"], air_pick]])
    name = c.search_read("stock.picking", [["id", "=", air_pick]], ["name"])[0]["name"]
    print(f"\nAIR receipt {name} (id {air_pick}) @ {args.air_date}; SEA receipt {pick['name']} @ {args.sea_date}")


if __name__ == "__main__":
    main()
