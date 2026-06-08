"""AIR-vs-SEA expedite sheet. Re-runnable; takes the PO numbers to potentially air.

For each product on those POs, computes how much to AIR (vs let it arrive by sea)
to avoid running out by each target date. Other open POs' arrival timing comes from
their Delivery (stock.picking) scheduled_date. Two targets by default: end of
month+2 and month+3 (e.g. run in June -> Aug-end and Sep-end).

Every derived cell is a live formula (Available, Demand-to-date, Need, AIR, Short)
so you can edit inputs (stock, demand, on-schedule supply) and it recalculates.

Run:
  cd inventorymgr && PYTHONPATH=src uv run python scripts/build_air_sheet.py --prod \
      --po P60165 --po P60167
  # optional explicit targets:
      --target 2026-08-31 --target 2026-09-30
"""

from __future__ import annotations

import argparse
import datetime
import math

from openpyxl.utils import get_column_letter as col

from inventorymgr.air import (
    add_months,
    fetch_air_data,
    last_day_of_month,
    month_range,
    onsched_through,
)
from inventorymgr.sources.gsheets import GSheets
from inventorymgr.sources.odoo_client import OdooClient
from inventorymgr.sources.odoo_source import read_monthly_sales

AIR_ROUND = 5
NEG_FILL = {"red": 0.96, "green": 0.80, "blue": 0.80}
BASE_HEADERS = ["product_id", "Display Name", "On Hand", "Outgoing", "Available", "Air-PO Qty"]
NB = len(BASE_HEADERS)  # demand block starts at column NB+1
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

    air_qty, products, onsched, input_arrivals = fetch_air_data(client, args.pos)
    pids = list(products)
    print(f"Input POs {args.pos} -> {len(pids)} products. Delivery dates: {input_arrivals}")
    if not pids:
        raise SystemExit("No remaining qty on those POs.")
    sales = read_monthly_sales(client, pids)

    months = month_range(today.year, today.month, targets[-1].year, targets[-1].month)
    n_months = len(months)
    dem0 = NB + 1                      # first demand column (1-indexed)
    blk0 = dem0 + n_months             # first target-block column

    def demand_of(pid):
        return [sales.get(pid, {}).get(f"{y - 1:04d}-{m:02d}", 0) for (y, m) in months]

    # ---- header ----
    header = list(BASE_HEADERS)
    header += [f"Sales {y - 1}-{m:02d}" for (y, m) in months]
    target_month_idx = []
    for t in targets:
        target_month_idx.append(months.index((t.year, t.month)))
        lab = t.strftime("%b-%d")
        header += [f"Onsched≤{lab}", f"Demand→{lab}", f"Need {lab}", f"AIR {lab}", f"Short {lab}"]

    # ---- rows (sorted by worst shortfall at the last target) ----
    def sort_key(pid):
        d = demand_of(pid)
        return sum(d) - (products[pid].on_hand - products[pid].outgoing)

    grid = [header]
    air_totals = [0.0] * len(targets)
    short_counts = [0] * len(targets)
    for r, pid in enumerate(sorted(pids, key=sort_key, reverse=True), start=2):
        p = products[pid]
        dvals = demand_of(pid)
        row = [pid, p.display_name or p.name, p.on_hand, p.outgoing, f"=C{r}-D{r}", air_qty.get(pid, 0)]
        row += dvals
        for k, t in enumerate(targets):
            ti = target_month_idx[k]
            ons = onsched_through(onsched.get(pid), t.isoformat())
            start = blk0 + k * PER_TARGET
            ons_c, dem_c, need_c = col(start), col(start + 1), col(start + 2)
            dem_first, dem_last = col(dem0), col(dem0 + ti)
            row += [
                ons,
                f"=SUM({dem_first}{r}:{dem_last}{r})",
                f"=MAX(0,{dem_c}{r}-E{r}-{ons_c}{r})",
                f"=MIN(CEILING({need_c}{r},{AIR_ROUND}),F{r})",
                f"=MAX(0,{need_c}{r}-F{r})",
            ]
            # mirror in python for the terminal summary
            need = max(0, sum(dvals[: ti + 1]) - (p.on_hand - p.outgoing) - ons)
            air_totals[k] += min(_ceil_round(need), air_qty.get(pid, 0))
            if need > air_qty.get(pid, 0):
                short_counts[k] += 1
        grid.append(row)

    # ---- write sheet ----
    g = GSheets()
    title = f"AIR Plan {today:%Y-%m-%d} ({'+'.join(args.pos)})"
    sh = g.create(title)
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
