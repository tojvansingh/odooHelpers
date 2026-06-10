"""AIR-vs-SEA expedite sheet. Re-runnable; takes the PO numbers to potentially air.

Demand model (avoids double-counting Outgoing): for each target period,
    demand = max(last-year forecast, booked from RETURNING customers) + booked from NEW customers
where "booked" = open outgoing orders shipping within the period (phased by ship date),
and "returning" = the customer also bought that product in the same period last year.
The projection starts from On Hand (booked demand lives in the demand term, not netted out).

Three tabs:
  - "Deliveries": Ready incoming receipt moves (supply). Feeds Onsched via SUMIFS.
  - "Bookings": open outgoing orders (demand) tagged returning/new with ship date + customer.
    Feeds Booked-ret / Booked-new via SUMIFS.
  - "AIR vs SEA": one row per product; every derived cell is a live formula.

Run:
  cd odooHelpers && PYTHONPATH=src uv run python scripts/build_air_sheet.py --prod \
      --po P60165 --po P60167  [--target 2026-08-31 --target 2026-09-30]
"""

from __future__ import annotations

import argparse
import datetime
import math
from collections import defaultdict

import gspread
from openpyxl.utils import get_column_letter as col

from inventorymgr.air import (
    add_months,
    commercial_partner_map,
    fetch_air_data,
    fetch_booked_outgoing,
    fetch_lastyear_buyer_sets,
    last_day_of_month,
    month_range,
    onsched_through,
)
from inventorymgr.sources.gsheets import GSheets
from inventorymgr.sources.odoo_client import OdooClient
from inventorymgr.sources.odoo_source import read_monthly_sales

AIR_ROUND = 5
NEG_FILL = {"red": 0.96, "green": 0.80, "blue": 0.80}
DELIV_HEADERS = ["product_id", "Display Name", "PO", "Type", "Delivery Date", "Qty"]
BOOK_HEADERS = ["product_id", "Display Name", "Customer", "Type", "Ship Date", "Qty"]
BASE_HEADERS = ["product_id", "Display Name", "On Hand", "Air-PO Qty"]
NB = len(BASE_HEADERS)
PER_TARGET = 7  # Onsched, Booked-ret, Booked-new, Demand, Need, AIR, Short


def _ceil_round(x: float) -> float:
    return 0 if x <= 0 else math.ceil(x / AIR_ROUND) * AIR_ROUND


def _reset_tab(sh, name: str, rows: int, cols: int):
    """Get-or-create a worksheet by name, cleared of values AND conditional formats."""
    try:
        ws = sh.worksheet(name)
    except gspread.WorksheetNotFound:
        return sh.add_worksheet(title=name, rows=rows, cols=cols)
    ws.clear()
    for meta in sh.fetch_sheet_metadata().get("sheets", []):
        if meta["properties"]["sheetId"] == ws.id:
            n = len(meta.get("conditionalFormats", []))
            if n:
                sh.batch_update({"requests": [
                    {"deleteConditionalFormatRule": {"sheetId": ws.id, "index": i}}
                    for i in range(n - 1, -1, -1)
                ]})
    ws.resize(rows=rows, cols=cols)
    return ws


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--po", dest="pos", action="append", required=True, help="PO number to air (repeatable)")
    ap.add_argument("--target", dest="targets", action="append", help="target date YYYY-MM-DD (repeatable)")
    ap.add_argument("--prod", action="store_true", help="use the production Odoo profile")
    args = ap.parse_args()

    client = OdooClient(profile="prod" if args.prod else "local")
    today = datetime.date.today()
    if args.targets:
        targets = sorted(datetime.date.fromisoformat(t) for t in args.targets)
    else:
        targets = [last_day_of_month(*add_months(today.year, today.month, n)) for n in (2, 3)]

    air_qty, products, onsched, deliveries = fetch_air_data(client, args.pos)
    pids = list(products)
    if not pids:
        raise SystemExit("No ready incoming on those POs.")
    sales = read_monthly_sales(client, pids)
    months = month_range(today.year, today.month, targets[-1].year, targets[-1].month)
    n_months = len(months)

    # Last-year buyer set per product, over the horizon's same months one year back.
    src_lo, src_hi = (months[0][0] - 1, months[0][1]), (months[-1][0] - 1, months[-1][1])
    buyers = fetch_lastyear_buyer_sets(
        client, pids, f"{src_lo[0]:04d}-{src_lo[1]:02d}-01", last_day_of_month(*src_hi).isoformat()
    )
    bookings_raw = fetch_booked_outgoing(client, pids)
    # Normalize partners to the commercial (company) level so a customer's order partner and
    # ship-to contact match — otherwise returning customers look "new".
    all_partners = set().union(*buyers.values()) if buyers else set()
    all_partners |= {b["partner_id"] for b in bookings_raw if b["partner_id"]}
    cmap = commercial_partner_map(client, all_partners)
    buyers_comm = {pid: {cmap.get(p, p) for p in s} for pid, s in buyers.items()}
    bookings = []
    for b in bookings_raw:
        bc = cmap.get(b["partner_id"]) if b["partner_id"] else None
        returning = bc is not None and bc in buyers_comm.get(b["pid"], set())
        bookings.append({**b, "type": "returning" if returning else "new",
                         "display_name": products[b["pid"]].display_name})
    book_by_pid: dict[int, list] = defaultdict(list)
    for b in bookings:
        book_by_pid[b["pid"]].append((b["date"], b["qty"], b["type"]))

    def demand_of(pid):
        return [sales.get(pid, {}).get(f"{y - 1:04d}-{m:02d}", 0) for (y, m) in months]

    def booked_through(pid, typ, cutoff):
        return sum(q for (d, q, t) in book_by_pid.get(pid, []) if t == typ and (d is None or d <= cutoff))

    print(f"Input POs {args.pos} -> {len(pids)} products, {len(deliveries)} ready incoming, {len(bookings)} open orders")

    # ---------- Deliveries tab ----------
    deliv_grid = [DELIV_HEADERS]
    for d in sorted(deliveries, key=lambda x: (x["display_name"], x["date"] or "9999", x["po"] or "")):
        deliv_grid.append([d["pid"], d["display_name"], d["po"] or "", d["type"], d["date"] or "", d["qty"]])

    # ---------- Bookings tab ----------
    book_grid = [BOOK_HEADERS]
    for b in sorted(bookings, key=lambda x: (x["display_name"], x["date"] or "9999", x["customer"])):
        book_grid.append([b["pid"], b["display_name"], b["customer"], b["type"], b["date"] or "", b["qty"]])

    # ---------- AIR tab ----------
    dem0 = NB + 1
    blk0 = dem0 + n_months
    header = list(BASE_HEADERS) + [f"Sales {y - 1}-{m:02d}" for (y, m) in months]
    target_month_idx = [months.index((t.year, t.month)) for t in targets]
    for t in targets:
        lab = t.strftime("%b-%d")
        header += [f"Onsched≤{lab}", f"Booked-ret≤{lab}", f"Booked-new≤{lab}",
                   f"Demand→{lab}", f"Need {lab}", f"AIR {lab}", f"Short {lab}"]

    def sort_key(pid):
        return sum(demand_of(pid)) + sum(q for _, q, _ in book_by_pid.get(pid, [])) - products[pid].on_hand

    grid = [header]
    air_totals = [0.0] * len(targets)
    short_counts = [0] * len(targets)
    for r, pid in enumerate(sorted(pids, key=sort_key, reverse=True), start=2):
        p = products[pid]
        dvals = demand_of(pid)
        row = [pid, p.display_name or p.name, p.on_hand,
               f'=SUMIFS(Deliveries!F:F,Deliveries!A:A,A{r},Deliveries!D:D,"air")']
        row += dvals
        for k, t in enumerate(targets):
            ti = target_month_idx[k]
            start = blk0 + k * PER_TARGET
            ons_c, bret_c, bnew_c, dem_c, need_c = (col(start), col(start + 1), col(start + 2),
                                                    col(start + 3), col(start + 4))
            dem_first, dem_last = col(dem0), col(dem0 + ti)
            date_fn = f"DATE({t.year},{t.month},{t.day})"
            row += [
                f'=SUMIFS(Deliveries!F:F,Deliveries!A:A,A{r},Deliveries!D:D,"on-schedule",Deliveries!E:E,"<="&{date_fn})',
                f'=SUMIFS(Bookings!F:F,Bookings!A:A,A{r},Bookings!D:D,"returning",Bookings!E:E,"<="&{date_fn})',
                f'=SUMIFS(Bookings!F:F,Bookings!A:A,A{r},Bookings!D:D,"new",Bookings!E:E,"<="&{date_fn})',
                f"=MAX(SUM({dem_first}{r}:{dem_last}{r}),{bret_c}{r})+{bnew_c}{r}",
                f"=MAX(0,{dem_c}{r}-C{r}-{ons_c}{r})",
                f"=MIN(CEILING({need_c}{r},{AIR_ROUND}),D{r})",
                f"=MAX(0,{need_c}{r}-D{r})",
            ]
            iso = t.isoformat()
            demand = max(sum(dvals[: ti + 1]), booked_through(pid, "returning", iso)) + booked_through(pid, "new", iso)
            need = max(0, demand - p.on_hand - onsched_through(onsched.get(pid), iso))
            air_totals[k] += min(_ceil_round(need), air_qty.get(pid, 0))
            if need > air_qty.get(pid, 0):
                short_counts[k] += 1
        grid.append(row)

    # ---------- write ----------
    g = GSheets()
    title = f"AIR Plan — {'+'.join(sorted(args.pos))}"
    sh = g.open_or_create(title)
    for name, gr in [("Deliveries", deliv_grid), ("Bookings", book_grid)]:
        w = _reset_tab(sh, name, max(len(gr), 2), len(gr[0]))
        w.update(values=gr, range_name="A1", value_input_option="USER_ENTERED")
        w.freeze(rows=1)
        w.format("1:1", {"textFormat": {"bold": True}})
        w.hide_columns(0, 1)

    ws = _reset_tab(sh, "AIR vs SEA", max(len(grid), 2), len(grid[0]))
    ws.update(values=grid, range_name="A1", value_input_option="USER_ENTERED")
    ws.freeze(rows=1)
    ws.format("1:1", {"textFormat": {"bold": True}})
    ws.hide_columns(0, 1)

    n_rows = len(grid) - 1
    short_ranges = [
        {"sheetId": ws.id, "startRowIndex": 1, "endRowIndex": 1 + n_rows,
         "startColumnIndex": blk0 + k * PER_TARGET + 6 - 1, "endColumnIndex": blk0 + k * PER_TARGET + 6}
        for k in range(len(targets))
    ]
    sh.batch_update({"requests": [{"addConditionalFormatRule": {
        "rule": {"ranges": short_ranges,
                 "booleanRule": {"condition": {"type": "NUMBER_GREATER", "values": [{"userEnteredValue": "0"}]},
                                 "format": {"backgroundColor": NEG_FILL}}},
        "index": 0}}]})

    for w in sh.worksheets():
        if w.title not in ("Deliveries", "Bookings", "AIR vs SEA"):
            sh.del_worksheet(w)

    print()
    for k, t in enumerate(targets):
        print(f"target {t}: total AIR = {air_totals[k]:.0f}, items short even after airing = {short_counts[k]}")
    print("\nSheet:", title)
    print("URL:", sh.url)


if __name__ == "__main__":
    main()
