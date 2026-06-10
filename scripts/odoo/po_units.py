"""Total units on order in one or more POs (ordered / received / remaining).

Run: cd inventorymgr && uv run python scripts/odoo/po_units.py --po P60165 [--po ...] [--prod]
"""

from __future__ import annotations

import argparse
import pathlib
import sys
from collections import defaultdict

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "src"))
from inventorymgr.sources.odoo_client import OdooClient  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="Total units on order in a PO")
    ap.add_argument("--po", dest="pos", action="append", required=True, help="PO number (repeatable)")
    ap.add_argument("--prod", action="store_true", help="use the production Odoo profile")
    args = ap.parse_args()

    c = OdooClient(profile="prod" if args.prod else "local")
    lines = c.search_read(
        "purchase.order.line", [["order_id.name", "in", args.pos]],
        ["order_id", "product_qty", "qty_received"],
    )
    agg = defaultdict(lambda: [0.0, 0.0])  # po name -> [ordered, received]
    for ln in lines:
        a = agg[ln["order_id"][1]]
        a[0] += ln["product_qty"] or 0
        a[1] += ln["qty_received"] or 0

    tot_o = tot_r = 0.0
    for name in args.pos:
        ordered, received = agg.get(name, [0.0, 0.0])
        tot_o += ordered
        tot_r += received
        print(f"{name}: ordered={ordered:.0f}  received={received:.0f}  remaining={ordered - received:.0f}")
    if len(args.pos) > 1:
        print(f"TOTAL: ordered={tot_o:.0f}  received={tot_r:.0f}  remaining={tot_o - tot_r:.0f}")


if __name__ == "__main__":
    main()
