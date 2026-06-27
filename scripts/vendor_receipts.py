"""Report a vendor's OPEN incoming receipts expected to arrive by a date.

Per receipt line: PO#, Receipt, Scheduled Date, Item, Qty on PO, Received, Remaining.
Writes/refreshes a Google Sheet ("Expected Receipts - <vendor>") and prints a summary.

Run:
  cd odooHelpers && PYTHONPATH=src uv run python scripts/vendor_receipts.py \
      --vendor "Orchid" --by 2026-09-30 [--prod]
"""

from __future__ import annotations

import argparse

from inventorymgr.sources.gsheets import GSheets
from inventorymgr.sources.odoo_client import OdooClient

HEADERS = ["PO#", "Receipt", "Scheduled Date", "Item", "Qty on PO", "Received", "Remaining"]


def main() -> None:
    ap = argparse.ArgumentParser(description="Report a vendor's open receipts expected by a date")
    ap.add_argument("--vendor", required=True, help="vendor name (matched ilike)")
    ap.add_argument("--by", required=True, help="expected-by date YYYY-MM-DD")
    ap.add_argument("--prod", action="store_true", help="target PRODUCTION (default: local)")
    args = ap.parse_args()

    c = OdooClient(profile="prod" if args.prod else "local")
    if args.prod:
        print(f"reading PRODUCTION {c.s.url}")

    moves = c.search_read(
        "stock.move",
        [["purchase_line_id.order_id.partner_id.name", "ilike", args.vendor],
         ["picking_id.picking_type_code", "=", "incoming"],
         ["picking_id.state", "not in", ["done", "cancel"]],
         ["picking_id.scheduled_date", "<=", f"{args.by} 23:59:59"],
         ["state", "not in", ["done", "cancel"]]],
        ["product_id", "purchase_line_id", "picking_id"],
    )
    if not moves:
        print(f"No open receipts for {args.vendor!r} due by {args.by}")
        return

    lines = {x["id"]: x for x in c.search_read(
        "purchase.order.line", [["id", "in", list({m["purchase_line_id"][0] for m in moves if m.get("purchase_line_id")})]],
        ["order_id", "product_qty", "qty_received"])}
    picks = {x["id"]: x for x in c.search_read(
        "stock.picking", [["id", "in", list({m["picking_id"][0] for m in moves})]],
        ["name", "scheduled_date"])}

    rows = []
    for m in moves:
        ln = lines.get(m["purchase_line_id"][0]) if m.get("purchase_line_id") else None
        if not ln:
            continue
        pk = picks.get(m["picking_id"][0], {})
        ordered, recv = ln["product_qty"] or 0, ln["qty_received"] or 0
        rows.append([
            ln["order_id"][1] if ln.get("order_id") else "",
            pk.get("name", ""),
            (pk.get("scheduled_date") or "")[:10],
            m["product_id"][1],
            ordered, recv, ordered - recv,
        ])
    rows.sort(key=lambda r: (r[2], r[0], r[3]))
    grid = [HEADERS] + rows

    g = GSheets()
    sh = g.open_or_create(f"Expected Receipts - {args.vendor}")
    ws = sh.sheet1
    ws.clear()
    ws.update_title("Receipts")
    ws.resize(rows=max(len(grid), 2), cols=len(HEADERS))
    ws.update(values=grid, range_name="A1", value_input_option="USER_ENTERED")
    ws.freeze(rows=1)
    ws.format("1:1", {"textFormat": {"bold": True}})
    for w in sh.worksheets():
        if w.id != ws.id:
            sh.del_worksheet(w)

    print(f"{args.vendor}: {len(rows)} lines across {len(picks)} receipts due by {args.by} | "
          f"total remaining {sum(r[6] for r in rows):.0f}")
    print("Sheet:", sh.url)


if __name__ == "__main__":
    main()
