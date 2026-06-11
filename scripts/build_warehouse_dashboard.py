"""Build the Warehouse Dashboard Google Sheet (manufacturing + shipping metrics).

Run: cd odooHelpers && uv run python scripts/build_warehouse_dashboard.py [--prod] [--dry-run]

Tabs:
  Manufacturing — units finished per class (last 5 weekdays, this week, avg/weekday
                  this month) and open-MO backlog bucketed by scheduled start.
  Shipping      — retail vs wholesale orders shipped (# and $) per day, plus open
                  deliveries: due-but-waiting-on-manufacturing, late and its split
                  (waiting on manufacturing / waiting on inventory / ready).
"""

from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import sys
from zoneinfo import ZoneInfo

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import gspread  # noqa: E402

from inventorymgr import warehouse_metrics as wm  # noqa: E402
from inventorymgr.sources.gsheets import GSheets  # noqa: E402
from inventorymgr.sources.odoo_client import OdooClient  # noqa: E402

DEFAULT_TITLE = "Warehouse Dashboard"
# Channel split by the sale order's "Order Source"; anything not listed is wholesale.
RETAIL_SOURCES = {"Shopify Retail", "Amazon FBA", "Amazon FBM"}

BOLD = {"textFormat": {"bold": True}}
USD = {"numberFormat": {"type": "CURRENCY", "pattern": "$#,##0"}}


class TabGrid:
    """Rows plus the formatting hints collected while building them."""

    def __init__(self):
        self.rows: list[list] = []
        self.bold_rows: list[int] = []
        self.currency_ranges: list[str] = []

    def add(self, cells: list | None = None, bold: bool = False) -> int:
        self.rows.append(cells or [])
        if bold:
            self.bold_rows.append(len(self.rows))
        return len(self.rows)  # 1-based row index


def _num(x: float):
    return round(x, 1) if abs(x - round(x)) > 1e-9 else int(round(x))


def _day_label(d: dt.date) -> str:
    return f"{d:%a} {d.month}/{d.day}"


def _backlog_table(g: TabGrid, title: str, backlog) -> None:
    g.add([title], bold=True)
    g.add(["Class", *wm.DUE_BUCKETS, "Total"], bold=True)
    for c in sorted(backlog, key=lambda c: -sum(backlog[c].values())):
        vals = [backlog[c].get(b, 0) for b in wm.DUE_BUCKETS]
        g.add([c] + [_num(v) for v in vals] + [_num(sum(vals))])
    tot = [sum(backlog[c].get(b, 0) for c in backlog) for b in wm.DUE_BUCKETS]
    g.add(["TOTAL"] + [_num(v) for v in tot] + [_num(sum(tot))], bold=True)


def build_manufacturing_tab(output, backlog, subcon, days, today, month_label, wd_elapsed) -> TabGrid:
    g = TabGrid()
    g.add([f"MANUFACTURING — refreshed {dt.datetime.now():%Y-%m-%d %H:%M}"], bold=True)
    g.add()
    g.add(["Units finished per class (in-house)"], bold=True)
    g.add(["Class"] + [_day_label(d) for d in days] + ["This Week", f"Avg/Weekday ({month_label})"], bold=True)
    wk_start, month_start = wm.week_start(today), today.replace(day=1)

    def out_row(by_day):
        week = sum(q for d, q in by_day.items() if d >= wk_start)
        month = sum(q for d, q in by_day.items() if d >= month_start)
        return [_num(by_day.get(d, 0)) for d in days] + [_num(week), round(month / wd_elapsed, 1)]

    classes = sorted(output, key=lambda c: -sum(output[c].values()))
    for c in classes:
        g.add([c] + out_row(output[c]))
    total_by_day = {d: sum(output[c].get(d, 0) for c in classes) for d in {d for c in classes for d in output[c]}}
    g.add(["TOTAL"] + out_row(total_by_day), bold=True)

    g.add()
    _backlog_table(g, "Open MO backlog — units to make in-house, by scheduled start", backlog)
    g.add()
    _backlog_table(g, "Subcontracted backlog — units on order with outside vendors", subcon)
    return g


def build_shipping_tab(ships, exc, days, today, month_label, wd_elapsed, tz_name) -> TabGrid:
    g = TabGrid()
    g.add([f"SHIPPING — refreshed {dt.datetime.now():%Y-%m-%d %H:%M}"], bold=True)
    g.add()
    g.add(["Orders shipped from warehouse (Petaluma + Massachusetts; Amazon-fulfilled FBA excluded)"], bold=True)
    g.add([""] + [_day_label(d) for d in days] + ["This Week", f"Avg/Weekday ({month_label})"], bold=True)
    wk_start, month_start = wm.week_start(today), today.replace(day=1)
    n_day_cols = len(days) + 2

    def chan_rows(label, by_day):
        counts = {d: len(ids) for d, (ids, _) in by_day.items()}
        values = {d: v for d, (_, v) in by_day.items()}
        for name, data, fmt in ((f"{label} — orders", counts, _num), (f"{label} — $", values, lambda v: round(v))):
            week = sum(q for d, q in data.items() if d >= wk_start)
            month = sum(q for d, q in data.items() if d >= month_start)
            r = g.add([name] + [fmt(data.get(d, 0)) for d in days]
                      + [fmt(week), round(month / wd_elapsed, 1 if fmt is _num else None)])
            if "$" in name:
                g.currency_ranges.append(f"B{r}:{chr(ord('A') + n_day_cols)}{r}")

    both = {d: (ids_r | ids_w, vr + vw)
            for d in set(ships["Retail"]) | set(ships["Wholesale"])
            for ids_r, vr in [ships["Retail"].get(d, (set(), 0.0))]
            for ids_w, vw in [ships["Wholesale"].get(d, (set(), 0.0))]}
    chan_rows("Retail", ships["Retail"])
    chan_rows("Wholesale", ships["Wholesale"])
    chan_rows("TOTAL", both)

    g.add()
    g.add(["Open deliveries (customer orders due today or earlier)"], bold=True)
    g.add(["", "# Orders", "$ Order Total", "$ Unshipped"], bold=True)
    for label, key in (
        ("Due to ship, waiting on manufacturing", "due_mfg"),
        ("Late (past scheduled ship date)", "late"),
        ("   late — waiting on manufacturing", "late_mfg"),
        ("   late — waiting on inventory (not mfg)", "late_inv"),
        ("   late — ready to ship", "late_ready"),
    ):
        e = exc[key]
        r = g.add([label, e["orders"], round(e["total"]), round(e["unshipped"])])
        g.currency_ranges.append(f"C{r}:D{r}")

    g.add()
    for note in (
        f"Retail = order source in: {', '.join(sorted(RETAIL_SOURCES))}; everything else is wholesale.",
        "$ per day = sale-line value shipped that day (excl. tax, after discounts);"
        " $ Order Total = full order amount incl. tax/shipping.",
        "Waiting on manufacturing = a short item on the delivery is tied to an open MO,"
        " or its product has an open MO or a BOM. Otherwise: waiting on inventory.",
        f"Late = open delivery past its scheduled date. Days bucketed in {tz_name}.",
    ):
        g.add([note])
    return g


def print_grid(name: str, g: TabGrid) -> None:
    print(f"\n=== {name} ===")
    widths = {}
    for row in g.rows:
        for i, cell in enumerate(row):
            widths[i] = min(max(widths.get(i, 0), len(str(cell))), 44)
    for row in g.rows:
        print("  ".join(str(c).ljust(widths[i]) for i, c in enumerate(row)).rstrip())


def write_sheet(title: str, grids: dict[str, TabGrid]) -> str:
    sh = GSheets().open_or_create(title)
    for name, g in grids.items():
        n_cols = max(len(r) for r in g.rows)
        try:
            ws = sh.worksheet(name)
            ws.clear()
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title=name, rows=len(g.rows), cols=n_cols)
        ws.resize(rows=len(g.rows), cols=max(n_cols, 8))
        ws.update(values=g.rows, range_name="A1", value_input_option="USER_ENTERED")
        ws.batch_format(
            [{"range": f"{r}:{r}", "format": BOLD} for r in g.bold_rows]
            + [{"range": rng, "format": USD} for rng in g.currency_ranges]
        )
        ws.columns_auto_resize(0, 1)
    for w in sh.worksheets():
        if w.title not in grids and w.title == "Sheet1":
            sh.del_worksheet(w)
    return sh.url


def main() -> None:
    ap = argparse.ArgumentParser(description="Build the Warehouse Dashboard Google Sheet")
    ap.add_argument("--prod", action="store_true", help="use the production Odoo profile")
    ap.add_argument("--title", default=DEFAULT_TITLE, help="spreadsheet name to create/reuse")
    ap.add_argument("--dry-run", action="store_true", help="print metrics; don't touch Google Sheets")
    args = ap.parse_args()

    client = OdooClient(profile="prod" if args.prod else "local")
    tz = ZoneInfo(client.user_tz)
    today = dt.datetime.now(tz).date()
    days = wm.last_weekdays(today, 5)
    since = min(today.replace(day=1), days[0], wm.week_start(today))
    wd_elapsed = wm.weekdays_elapsed_in_month(today)
    month_label = f"{today:%b}"

    ptype_ids = wm.outgoing_picking_type_ids(client)
    print(f"Odoo: {client.s.url}  tz={tz}  today={today}  picking types={ptype_ids}")

    output = wm.fetch_production_output(client, tz, since)
    backlog, subcon = wm.fetch_production_backlog(client, tz, today)
    ships = wm.fetch_shipments(client, tz, ptype_ids, since, RETAIL_SOURCES)
    exc = wm.fetch_open_exceptions(client, tz, ptype_ids, today)

    grids = {
        "Manufacturing": build_manufacturing_tab(output, backlog, subcon, days, today, month_label, wd_elapsed),
        "Shipping": build_shipping_tab(ships, exc, days, today, month_label, wd_elapsed, str(tz)),
    }
    for name, g in grids.items():
        print_grid(name, g)
    if not args.dry_run:
        print("\nSheet:", write_sheet(args.title, grids))


if __name__ == "__main__":
    main()
