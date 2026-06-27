"""Receive items against POs from a vendor packing-list sheet, with PO-scoped fuzzy matching.

Vendors use loose item names ("CALIFORNIA"), so each name is matched only against the
items ON THAT PO — the PO disambiguates whether it's a pillow, glass, dish towel, etc.

Sheet columns (configurable): PO# (e.g. P60199), Design (vendor item name), Qty (received).

Matching: tokens are graded (exact > prefix > typo-fuzzy) and IDF-weighted so a rare,
distinctive token (VAIL) outweighs a common one (VALLEY/PILLOW). A match is only auto-filled
when it's confident AND clearly ahead of the runner-up; otherwise the cell is left EMPTY and
the top candidates go in a "Suggestions" column for the operator to choose.

Modes:
  annotate (default): fills "Disambiguated Item Name" (confident only; else blank),
    "On Order Qty", "Received Qty", "Suggestions". Operator-filled Disambiguated cells are
    never overwritten. The operator fills/fixes Disambiguated and re-runs to refresh.
  --receive: set the matched quantities on each PO's open incoming receipt and validate it
    (auto-creating a backorder for the remainder). Gated: --prod for production; --dry-run preview.

Run:
  cd odooHelpers && PYTHONPATH=src uv run python scripts/receive_from_sheet.py --sheet <KEY> [--prod]
  ...                                                                          --receive [--prod] [--dry-run]
"""

from __future__ import annotations

import argparse
import math
import re
from collections import defaultdict
from difflib import SequenceMatcher

from inventorymgr.sources.gsheets import GSheets
from inventorymgr.sources.odoo_client import OdooClient

DISAMBIG, ONORDER, RECEIVED, SUGGEST = "Disambiguated Item Name", "On Order Qty", "Received Qty", "Suggestions"
CONFIDENT = 0.62     # min score to auto-fill a match
MARGIN = 0.08        # min lead over the runner-up to auto-fill (else ambiguous -> blank)
FUZZY_TOK = 0.82     # token typo tolerance (LOUSIANA ~ LOUISIANA)


def norm(s: str) -> str:
    s = re.sub(r"\[[^\]]*\]", " ", s or "")  # drop [SKU] codes
    s = re.sub(r"[^A-Za-z0-9 ]", " ", s).upper()
    return re.sub(r"\s+", " ", s).strip()


def _token_quality(t: str, ct: list[str], ct_set: set) -> float:
    if t in ct_set:                                   # exact token
        return 1.0
    if any(w.startswith(t) for w in ct):              # candidate token EXTENDS vendor token (AMERICA -> AMERICA250)
        return 0.55
    best = max((SequenceMatcher(None, t, w).ratio() for w in ct), default=0.0)
    return best if best >= FUZZY_TOK else best * 0.4  # weak fuzzy heavily discounted


def best_match(name: str, candidates: list[str]) -> tuple[str | None, float, float, list[str]]:
    """Return (best_display, score, margin_over_runner_up, top3_suggestions)."""
    cand_norm = {dn: norm(dn) for dn in candidates}
    n = max(1, len(candidates))
    vt = norm(name).split()
    if not vt:
        return (None, 0.0, 0.0, [])
    # IDF of each vendor token among the candidates (rare -> high weight)
    idf = {}
    for t in set(vt):
        df = sum(1 for cn in cand_norm.values() if t in cn.split())
        idf[t] = math.log((n + 1) / (df + 1)) + 1

    def score(cn: str) -> float:
        ct = cn.split()
        ct_set = set(ct)
        num = den = 0.0
        for t in vt:
            num += idf[t] * _token_quality(t, ct, ct_set)
            den += idf[t]
        return num / den if den else 0.0

    scored = sorted(((score(cn), dn) for dn, cn in cand_norm.items()), reverse=True)
    if not scored:
        return (None, 0.0, 0.0, [])
    top_score, top_dn = scored[0]
    margin = top_score - (scored[1][0] if len(scored) > 1 else 0.0)
    return (top_dn, top_score, margin, [dn for _, dn in scored[:3]])


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


def resolve(items: dict, vendor_name: str, existing: str):
    """Return (pid, write_disambig, ordered, received, suggestions).

    pid is None when unresolved. An operator-filled `existing` is honored and never blanked.
    """
    if not items:
        return (None, existing, "", "", "")
    by_name = {v["display_name"]: pid for pid, v in items.items()}
    existing = (existing or "").strip()
    if existing in by_name:                                   # operator confirmed an exact PO item
        pid = by_name[existing]
        return (pid, existing, items[pid]["ordered"], items[pid]["received"], "")
    if existing:                                              # operator typed something else -> resolve, keep their text
        dn, sc, mg, sugg = best_match(existing, list(by_name))
        if dn and sc >= CONFIDENT:
            pid = by_name[dn]
            return (pid, existing, items[pid]["ordered"], items[pid]["received"], "")
        return (None, existing, "", "", " | ".join(sugg))     # unresolved; leave their text + suggestions
    dn, sc, mg, sugg = best_match(vendor_name, list(by_name))  # first pass on the vendor name
    if dn and sc >= CONFIDENT and mg >= MARGIN:
        pid = by_name[dn]
        return (pid, dn, items[pid]["ordered"], items[pid]["received"], "")
    return (None, "", "", "", " | ".join(sugg))               # uncertain -> blank + suggestions


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
    for c2 in (args.po_col, args.item_col, args.qty_col):
        if c2 not in header:
            raise SystemExit(f"Column {c2!r} not in sheet header {header}")
    pi, ii, qi = header.index(args.po_col), header.index(args.item_col), header.index(args.qty_col)
    col = {}
    for nm in (DISAMBIG, ONORDER, RECEIVED, SUGGEST):
        if nm not in header:
            header.append(nm)
        col[nm] = header.index(nm)
    width = len(header)

    po_items = fetch_po_items(c, {r[pi].strip() for r in vals[1:] if len(r) > pi and r[pi].strip()})

    resolved = []  # (row, pid, qty)
    blanks = unresolved = 0
    for r in vals[1:]:
        while len(r) < width:
            r.append("")
        po = r[pi].strip()
        if not po and not r[ii].strip():
            continue
        pid, disp, ordered, received, sugg = resolve(po_items.get(po, {}), r[ii], r[col[DISAMBIG]])
        r[col[DISAMBIG]] = disp
        r[col[ONORDER]] = ordered
        r[col[RECEIVED]] = received
        r[col[SUGGEST]] = sugg
        try:
            qty = float(r[qi] or 0)
        except ValueError:
            qty = 0
        resolved.append((r, pid, qty))
        if pid is None:
            unresolved += 1
            if not (r[col[DISAMBIG]]).strip():
                blanks += 1

    if args.receive:
        skipped = [r[ii] for r, pid, q in resolved if pid is None and q > 0]
        if skipped:
            print(f"  skipping {len(skipped)} unresolved rows (blank Disambiguated): {skipped[:6]}")
        receive(c, resolved, pi, args.dry_run)
        return

    ws.update(values=[header] + vals[1:], range_name="A1", value_input_option="USER_ENTERED")
    print(f"annotated {len(resolved)} rows | auto-filled: {len(resolved)-unresolved} | "
          f"left blank for review: {blanks}")
    print("Pick from 'Suggestions' into 'Disambiguated Item Name' for blank rows, re-run, then add --receive.")


def receive(c: OdooClient, resolved, pi: int, dry: bool) -> None:
    by_po: dict = defaultdict(dict)
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
