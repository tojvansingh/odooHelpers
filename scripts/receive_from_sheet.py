"""Receive items against POs from a vendor packing-list sheet, with PO-scoped fuzzy matching.

Vendors use loose item names ("CALIFORNIA"), so each name is matched only against the
items ON THAT PO — the PO disambiguates whether it's a pillow, glass, dish towel, etc.

Sheet columns (configurable): PO# (e.g. P60199), Design (vendor item name), Qty (received).

Modes:
  annotate (default): for each row, fill three columns —
      "Disambiguated Item Name" (best match; "?? ..." = low confidence; "NO MATCH" = none),
      "On Order Qty", "Received Qty" (already-received on that PO line).
    The operator corrects "Disambiguated Item Name" and re-runs to refresh the other two.
  --receive: set the matched quantities on each PO's open incoming receipt and validate it
    (auto-creating a backorder for the remainder). Gated: --prod for production; --dry-run preview.

Run:
  cd odooHelpers && PYTHONPATH=src uv run python scripts/receive_from_sheet.py --sheet <KEY> [--prod]
  ...                                                                          --receive [--prod] [--dry-run]
"""

from __future__ import annotations

import argparse
import re
from collections import defaultdict
from difflib import SequenceMatcher

from inventorymgr.sources.gsheets import GSheets
from inventorymgr.sources.odoo_client import OdooClient

DISAMBIG, ONORDER, RECEIVED = "Disambiguated Item Name", "On Order Qty", "Received Qty"
LOW_CONFIDENCE = 0.45


def norm(s: str) -> str:
    s = re.sub(r"\[[^\]]*\]", " ", s or "")  # drop [SKU] codes
    s = re.sub(r"[^A-Za-z0-9 ]", " ", s).upper()
    return re.sub(r"\s+", " ", s).strip()


def _tok_match(t: str, w: str) -> bool:
    # equal / prefix / typo-tolerant (catches LOUSIANA~LOUISIANA, CAPRICON~CAPRICORN)
    return w == t or w.startswith(t) or t.startswith(w) or SequenceMatcher(None, t, w).ratio() >= 0.8


def match_score(vendor: str, cand: str) -> float:
    v, c = norm(vendor), norm(cand)
    if not v or not c:
        return 0.0
    vt, ct = v.split(), c.split()
    covered = sum(1 for t in vt if any(_tok_match(t, w) for w in ct)) / len(vt)
    return 0.65 * covered + 0.35 * SequenceMatcher(None, v, c).ratio()


def best_match(name: str, candidates: list[str]) -> tuple[str | None, float]:
    scored = sorted(((match_score(name, dn), dn) for dn in candidates), reverse=True)
    return (scored[0][1], scored[0][0]) if scored else (None, 0.0)


def fetch_po_items(c: OdooClient, po_names) -> dict:
    """{po_name: {pid: {'display_name','ordered','received'}}}, aggregated by product."""
    lines = c.search_read(
        "purchase.order.line", [["order_id.name", "in", list(po_names)]],
        ["order_id", "product_id", "product_qty", "qty_received"],
    )
    out: dict = defaultdict(dict)
    for ln in lines:
        if not ln.get("product_id"):
            continue
        po, pid = ln["order_id"][1], ln["product_id"][0]
        d = out[po].setdefault(pid, {"display_name": ln["product_id"][1], "ordered": 0.0, "received": 0.0})
        d["ordered"] += ln["product_qty"] or 0
        d["received"] += ln["qty_received"] or 0
    return out


def resolve(items: dict, vendor_name: str, disambig: str):
    """Return (pid, display_name, ordered, received, score). items: {pid: {...}}."""
    if not items:
        return (None, "NO MATCH", "", "", 0.0)
    by_name = {v["display_name"]: pid for pid, v in items.items()}
    chosen = (disambig or "").strip()
    # operator confirmed an exact PO item name -> lock it in
    if chosen in by_name:
        pid = by_name[chosen]
        return (pid, chosen, items[pid]["ordered"], items[pid]["received"], 1.0)
    target = chosen if chosen and chosen.upper() != "NO MATCH" else vendor_name
    dn, score = best_match(target, list(by_name))
    if dn is None or score < 0.2:
        return (None, "NO MATCH", "", "", score)
    pid = by_name[dn]
    label = dn if score >= LOW_CONFIDENCE else f"?? {dn}"
    return (pid, label, items[pid]["ordered"], items[pid]["received"], score)


def main() -> None:
    ap = argparse.ArgumentParser(description="Receive items against POs from a packing-list sheet (PO-scoped fuzzy match)")
    ap.add_argument("--sheet", required=True, help="Google Sheet key")
    ap.add_argument("--tab", help="worksheet name (default: first tab)")
    ap.add_argument("--po-col", default="PO#")
    ap.add_argument("--item-col", default="Design")
    ap.add_argument("--qty-col", default="Qty")
    ap.add_argument("--receive", action="store_true", help="validate the receipts (default: annotate only)")
    ap.add_argument("--prod", action="store_true", help="target PRODUCTION (default: local)")
    ap.add_argument("--dry-run", action="store_true", help="with --receive: preview, do not write to Odoo")
    args = ap.parse_args()

    c = OdooClient(profile="prod" if args.prod else "local")
    if args.prod:
        print(f"*** {'RECEIVE' if args.receive else 'READ'} target: PRODUCTION {c.s.url} ***")
    elif args.receive and not any(h in c.s.url for h in ("localhost", "127.0.0.1")):
        raise SystemExit(f"Refusing --receive: local profile is not localhost ({c.s.url})")

    sh = GSheets().open_by_key(args.sheet)
    ws = sh.worksheet(args.tab) if args.tab else sh.worksheets()[0]
    vals = ws.get_all_values()
    header = vals[0]
    for col in (args.po_col, args.item_col, args.qty_col):
        if col not in header:
            raise SystemExit(f"Column {col!r} not in sheet header {header}")
    pi, ii, qi = header.index(args.po_col), header.index(args.item_col), header.index(args.qty_col)
    col = {}
    for nm in (DISAMBIG, ONORDER, RECEIVED):
        if nm not in header:
            header.append(nm)
        col[nm] = header.index(nm)
    width = len(header)

    data = [r for r in vals[1:] if r and len(r) > max(pi, ii) and (r[pi].strip() or r[ii].strip())]
    po_items = fetch_po_items(c, {r[pi].strip() for r in data if r[pi].strip()})

    # resolve every row
    resolved = []  # (row, pid, qty)
    low = nomatch = 0
    for r in vals[1:]:
        while len(r) < width:
            r.append("")
        po = r[pi].strip()
        if not po and not r[ii].strip():
            continue
        pid, label, ordered, received, score = resolve(po_items.get(po, {}), r[ii], r[col[DISAMBIG]])
        r[col[DISAMBIG]] = label
        r[col[ONORDER]] = ordered
        r[col[RECEIVED]] = received
        try:
            qty = float(r[qi] or 0)
        except ValueError:
            qty = 0
        resolved.append((r, pid, qty))
        if pid is None:
            nomatch += 1
        elif score < LOW_CONFIDENCE:
            low += 1

    if args.receive:
        receive(c, resolved, pi, po_items, args.dry_run)
        return

    ws.update(values=[header] + vals[1:], range_name="A1", value_input_option="USER_ENTERED")
    print(f"annotated {len(resolved)} rows | low-confidence (??): {low} | no-match: {nomatch}")
    print("Fix any 'Disambiguated Item Name' cells and re-run; then add --receive to post the receipts.")


def receive(c: OdooClient, resolved, pi: int, po_items: dict, dry: bool) -> None:
    by_po: dict = defaultdict(dict)  # po -> pid -> qty
    for r, pid, qty in resolved:
        if pid and qty > 0:
            by_po[r[pi].strip()][pid] = by_po[r[pi].strip()].get(pid, 0) + qty
    for po, recv in by_po.items():
        po_rec = c.search_read("purchase.order", [["name", "=", po]], ["picking_ids"])
        if not po_rec:
            print(f"  {po}: not found"); continue
        picks = c.search_read(
            "stock.picking",
            [["id", "in", po_rec[0]["picking_ids"]], ["state", "not in", ["done", "cancel"]],
             ["picking_type_code", "=", "incoming"]],
            ["name", "state"],
        )
        if not picks:
            print(f"  {po}: no open incoming receipt"); continue
        pick = picks[0]
        moves = c.search_read("stock.move", [["picking_id", "=", pick["id"]], ["state", "not in", ["done", "cancel"]]],
                              ["product_id", "product_uom_qty"])
        applied = 0
        for m in moves:
            pid = m["product_id"][0]
            if pid in recv:
                if not dry:
                    c.execute_kw("stock.move", "write", [[m["id"]], {"quantity": recv[pid], "picked": True}])
                applied += 1
        print(f"  {po} receipt {pick['name']}: setting {applied} of {len(recv)} matched products")
        if dry:
            continue
        res = c.execute_kw("stock.picking", "button_validate", [[pick["id"]]])
        if isinstance(res, dict) and res.get("res_model") == "stock.backorder.confirmation":
            ctx = res.get("context", {})
            wiz = c.execute_kw("stock.backorder.confirmation", "create", [{"pick_ids": [(6, 0, [pick["id"]])]}], {"context": ctx})
            c.execute_kw("stock.backorder.confirmation", "process", [[wiz]], {"context": ctx})
        state = c.search_read("stock.picking", [["id", "=", pick["id"]]], ["state"])[0]["state"]
        print(f"    -> validated {pick['name']} (now {state})")


if __name__ == "__main__":
    main()
