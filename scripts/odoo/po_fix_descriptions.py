"""Rewrite a PO's line Descriptions to include the vendor's product code.

Mirrors a manually-added line: each line name is rebuilt from product.display_name
read WITH the PO's vendor in context, so Odoo prepends the supplierinfo product code
([GN41619]…); products with no seller for that vendor keep their internal code.
Use to backfill POs created before create_pos.py set this itself.

Run:
  cd odooHelpers && PYTHONPATH=src uv run python scripts/odoo/po_fix_descriptions.py \
      --po P60274 [--prod] [--dry-run]
"""

from __future__ import annotations

import argparse


def main() -> None:
    ap = argparse.ArgumentParser(description="Backfill vendor product codes into a PO's line descriptions")
    ap.add_argument("--po", required=True, help="PO name, e.g. P60274")
    ap.add_argument("--prod", action="store_true", help="target PRODUCTION (default: local)")
    ap.add_argument("--dry-run", action="store_true", help="preview changes, do not write")
    args = ap.parse_args()

    from inventorymgr.sources.odoo_client import OdooClient
    c = OdooClient(profile="prod" if args.prod else "local")
    if args.prod:
        print(f"*** target: PRODUCTION {c.s.url} ***")

    po = c.search_read("purchase.order", [["name", "=", args.po]], ["partner_id", "order_line", "state"])
    if not po:
        raise SystemExit(f"{args.po} not found")
    po = po[0]
    partner_id = po["partner_id"][0]
    print(f"{args.po}: vendor {po['partner_id'][1]} | state {po['state']} | {len(po['order_line'])} lines")

    lines = c.search_read("purchase.order.line", [["id", "in", po["order_line"]]], ["product_id", "name"])
    pids = [l["product_id"][0] for l in lines if l.get("product_id")]
    descr = {r["id"]: r for r in c.execute_kw(
        "product.product", "read", [pids, ["display_name", "description_purchase"]],
        {"context": {"partner_id": partner_id}})}

    def target_name(line) -> str | None:
        if not line.get("product_id"):
            return None
        r = descr.get(line["product_id"][0], {})
        nm = r.get("display_name")
        if not nm:
            return None
        if r.get("description_purchase"):
            nm += "\n" + r["description_purchase"]
        return nm

    changes = []
    for l in lines:
        nm = target_name(l)
        if nm and nm != l["name"]:
            changes.append((l["id"], l["name"], nm))

    print(f"  {len(changes)} of {len(lines)} lines would change")
    for _id, old, new in changes[:8]:
        print(f"    {old!r}\n      -> {new!r}")
    if len(changes) > 8:
        print(f"    … and {len(changes) - 8} more")

    if args.dry_run or not changes:
        return
    for _id, _old, new in changes:
        c.execute_kw("purchase.order.line", "write", [[_id], {"name": new}])
    print(f"  updated {len(changes)} line descriptions on {args.po}")


if __name__ == "__main__":
    main()
