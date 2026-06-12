"""Set the unit price on every line of a PO. Gated: --prod for production (default local).

Run: cd odooHelpers && uv run python scripts/odoo/po_set_price.py --po P60261 --price 2.95 --prod [--dry-run]
"""

from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "src"))
from inventorymgr.sources.odoo_client import OdooClient  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--po", required=True)
    ap.add_argument("--price", type=float, required=True)
    ap.add_argument("--prod", action="store_true", help="target PRODUCTION (default: local)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    c = OdooClient(profile="prod" if args.prod else "local")
    if args.prod:
        print(f"*** PRODUCTION write target: {c.s.url} ***")
    elif not any(h in c.s.url for h in ("localhost", "127.0.0.1")):
        raise SystemExit(f"Refusing: local profile is not localhost ({c.s.url})")

    rows = c.search_read("purchase.order", [["name", "=", args.po]], ["state", "order_line", "partner_id"])
    if not rows:
        raise SystemExit(f"PO {args.po} not found")
    po = rows[0]
    line_ids = po["order_line"]
    print(f"{args.po} | vendor {po['partner_id'][1]} | state {po['state']} | {len(line_ids)} lines -> price {args.price}")
    if args.dry_run or not line_ids:
        return
    c.execute_kw("purchase.order.line", "write", [line_ids, {"price_unit": args.price}])
    total = c.search_read("purchase.order", [["name", "=", args.po]], ["amount_total"])[0]["amount_total"]
    print(f"  updated {len(line_ids)} lines. new amount_total ${round(total, 2)}")


if __name__ == "__main__":
    main()
