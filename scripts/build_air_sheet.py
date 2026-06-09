"""AIR-vs-SEA expedite sheet. Re-runnable; takes the PO numbers to potentially air.

Two tabs:
  - "Deliveries": every Ready (assigned) incoming receipt move for the affected
    products — product, PO, type (air = on an input PO / on-schedule = other),
    Delivery date, Qty. This is the auditable source of all supply.
  - "AIR vs SEA": one row per product. Air-PO Qty and Onsched<=target are live
    SUMIFS over the Deliveries tab; Available, Demand, Need, AIR, Short are formulas.
    Edit the Deliveries dates/qtys or the Sales/stock inputs and everything recalcs.

Supply counts ONLY receipts in Ready state (cancelled/done receipts are excluded),
each dated by its own move scheduled date.

Run:
  cd inventorymgr && PYTHONPATH=src uv run python scripts/build_air_sheet.py --prod \
      --po P60165 --po P60167  [--target 2026-08-31 --target 2026-09-30]
"""

from __future__ import annotations

import argparse
import datetime
import math

from openpyxl.utils import get_column_letter as col

from inventorymgr.air import add_months, fetch_air_data, last_day_of_month, month_range, onsched_through
from inventorymgr.sources.gsheets import GSheets
from inventorymgr.sources.odoo_client import OdooClient
from inventorymgr.sources.odoo_source import read_monthly_sales

AIR_ROUND = 5
NEG_FILL = {"red": 0.96, "green": 0.80, "blue": 0.80}
DELIV_HEADERS = ["product_id", "Display Name", "PO", "Type", "Delivery Date", "Qty"]
BASE_HEADERS = ["product_id", "Display Name", "On Hand", "Outgoing", "Available", "Air-PO Qty"]
NB = len(BASE_HEADERS)
PER_TARGET = 5  # Onsched, Demand->T, Need, AIR, Short


def _ceil_round(x: float) -> float:
    return 0 if x <= 0 else math.ceil(x / AIR_ROUND) * AIR_ROUND


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
    print(f"Input POs {args.pos} -> {len(pids)} products, {len(deliveries)} ready incoming moves")
    if not pids:
        raise SystemExit("No ready incoming on those POs.")
    sales = read_monthly_sales(client, pids)
    months = month_range(today.year, today.month, targets[-1].year, targets[-1].month)
    n_months = len(months)
    dem0 = NB + 1
    blk0 = dem0 + n_months

    def demand_of(pid):
        return [sales.get(pid, {}).get(f"{y - 1:04d}-{m:02d}", 0) for (y, m) in months]

    # ---------- Deliveries tab ----------
    deliv_grid = [DELIV_HEADERS]
    for d in sorted(deliveries, key=lambda x: (x["display_name"], x["date"] or "9999", x["po"] or "")):
        deliv_grid.append([d["pid"], d["display_name"], d["po"] or "", d["type"], d["date"] or "", d["qty"]])

    # ---------- AIR tab ----------
    header = list(BASE_HEADERS)
    header += [f"Sales {y - 1}-{m:02d}" for (y, m) in months]
    target_month_idx = [months.index((t.year, t.month)) for t in targets]
    for t in targets:
        lab = t.strftime("%b-%d")
        header += [f"Onsched≤{lab}", f"Demand→{lab}", f"Need {lab}", f"AIR {lab}", f"Short {lab}"]

    def sort_key(pid):
        return sum(demand_of(pid)) - (products[pid].on_hand - products[pid].outgoing)

    grid = [header]
    air_totals = [0.0] * len(targets)
    short_counts = [0] * len(targets)
    for r, pid in enumerate(sorted(pids, key=sort_key, reverse=True), start=2):
        p = products[pid]
        dvals = demand_of(pid)
        row = [
            pid, p.display_name or p.name, p.on_hand, p.outgoing,
            f"=C{r}-D{r}",
            f'=SUMIFS(Deliveries!F:F,Deliveries!A:A,A{r},Deliveries!D:D,"air")',
        ]
        row += dvals
        for k, t in enumerate(targets):
            ti = target_month_idx[k]
            start = blk0 + k * PER_TARGET
            ons_c, dem_c, need_c = col(start), col(start + 1), col(start + 2)
            dem_first, dem_last = col(dem0), col(dem0 + ti)
            row += [
                f'=SUMIFS(Deliveries!F:F,Deliveries!A:A,A{r},Deliveries!D:D,"on-schedule",'
                f'Deliveries!E:E,"<="&DATE({t.year},{t.month},{t.day}))',
                f"=SUM({dem_first}{r}:{dem_last}{r})",
                f"=MAX(0,{dem_c}{r}-E{r}-{ons_c}{r})",
                f"=MIN(CEILING({need_c}{r},{AIR_ROUND}),F{r})",
                f"=MAX(0,{need_c}{r}-F{r})",
            ]
            need = max(0, sum(dvals[: ti + 1]) - (p.on_hand - p.outgoing) - onsched_through(onsched.get(pid), t.isoformat()))
            air_totals[k] += min(_ceil_round(need), air_qty.get(pid, 0))
            if need > air_qty.get(pid, 0):
                short_counts[k] += 1
        grid.append(row)

    # ---------- write ----------
    g = GSheets()
    title = f"AIR Plan {today:%Y-%m-%d} ({'+'.join(args.pos)})"
    sh = g.create(title)

    # Write Deliveries FIRST so the AIR tab's SUMIFS references resolve at entry time.
    dv = sh.add_worksheet(title="Deliveries", rows=max(len(deliv_grid), 2), cols=len(DELIV_HEADERS))
    dv.update(values=deliv_grid, range_name="A1", value_input_option="USER_ENTERED")
    dv.freeze(rows=1)
    dv.format("1:1", {"textFormat": {"bold": True}})
    dv.hide_columns(0, 1)

    ws = sh.sheet1
    ws.update_title("AIR vs SEA")
    ws.resize(rows=max(len(grid), 2), cols=len(grid[0]))
    ws.update(values=grid, range_name="A1", value_input_option="USER_ENTERED")
    ws.freeze(rows=1)
    ws.format("1:1", {"textFormat": {"bold": True}})
    ws.hide_columns(0, 1)

    n_rows = len(grid) - 1
    short_ranges = [
        {"sheetId": ws.id, "startRowIndex": 1, "endRowIndex": 1 + n_rows,
         "startColumnIndex": blk0 + k * PER_TARGET + 4 - 1, "endColumnIndex": blk0 + k * PER_TARGET + 4}
        for k in range(len(targets))
    ]
    sh.batch_update({"requests": [{"addConditionalFormatRule": {
        "rule": {"ranges": short_ranges,
                 "booleanRule": {"condition": {"type": "NUMBER_GREATER", "values": [{"userEnteredValue": "0"}]},
                                 "format": {"backgroundColor": NEG_FILL}}},
        "index": 0}}]})

    print()
    for k, t in enumerate(targets):
        print(f"target {t}: total AIR = {air_totals[k]:.0f}, items short even after airing = {short_counts[k]}")
    print("\nCreated:", title)
    print("URL:", sh.url)


if __name__ == "__main__":
    main()
