"""Build the Warehouse Dashboard Google Sheet (manufacturing + shipping metrics).

Run: cd odooHelpers && uv run python scripts/build_warehouse_dashboard.py [--prod] [--dry-run]

Tabs:
  Manufacturing — units finished per class (last 5 weekdays, this week, avg/weekday
                  this month) and open-MO backlog bucketed by scheduled start,
                  split in-house vs subcontracted.
  Shipping      — retail vs wholesale orders shipped (# and $) per day, plus open
                  deliveries: due-but-waiting-on-manufacturing, late and its split
                  (waiting on manufacturing / waiting on inventory / ready).
"""

from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import subprocess
import sys
from zoneinfo import ZoneInfo

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import gspread  # noqa: E402
from gspread.utils import a1_range_to_grid_range  # noqa: E402
from openpyxl.utils import get_column_letter  # noqa: E402

from inventorymgr import warehouse_metrics as wm  # noqa: E402
from inventorymgr.sources.gsheets import GSheets  # noqa: E402
from inventorymgr.sources.odoo_client import OdooClient  # noqa: E402

DEFAULT_TITLE = "Warehouse Dashboard"
# Channel split by the sale order's "Order Source"; anything not listed is wholesale.
RETAIL_SOURCES = {"Shopify Retail", "Amazon FBA", "Amazon FBM"}

# Scheduler (--if-due) settings: refresh once per evening, the next time the laptop is
# awake + plugged in. Flip REQUIRE_AC to False to also run on battery.
RUN_HOUR, RUN_MINUTE = 16, 30
REQUIRE_AC = True
STAMP_FILE = pathlib.Path.home() / "Library/Application Support/warehouse-dashboard/last_success.txt"

SLATE = {"red": 0.247, "green": 0.317, "blue": 0.376}
WHITE = {"red": 1, "green": 1, "blue": 1}
HEADER_BG = {"red": 0.937, "green": 0.937, "blue": 0.937}
TOTAL_BG = {"red": 0.965, "green": 0.965, "blue": 0.965}
SUMMARY_BG = {"red": 0.929, "green": 0.957, "blue": 0.988}   # This Week / Avg columns
PASTDUE_BG = {"red": 0.957, "green": 0.80, "blue": 0.80}
GRAY_TEXT = {"red": 0.45, "green": 0.45, "blue": 0.45}

KIND_FORMATS = {
    "title": {"textFormat": {"bold": True, "fontSize": 12}},
    "section": {"backgroundColor": SLATE,
                "textFormat": {"bold": True, "foregroundColor": WHITE}},
    "header": {"backgroundColor": HEADER_BG, "textFormat": {"bold": True},
               "horizontalAlignment": "CENTER",
               "borders": {"bottom": {"style": "SOLID"}}},
    "total": {"backgroundColor": TOTAL_BG, "textFormat": {"bold": True},
              "borders": {"top": {"style": "SOLID"}}},
    "note": {"textFormat": {"italic": True, "fontSize": 9, "foregroundColor": GRAY_TEXT}},
}
INT_FMT = {"numberFormat": {"type": "NUMBER", "pattern": "#,##0"}}
DEC_FMT = {"numberFormat": {"type": "NUMBER", "pattern": "#,##0.0"}}
USD = {"numberFormat": {"type": "CURRENCY", "pattern": "$#,##0"}}
TAB_COLORS = {
    "Manufacturing": {"red": 0.31, "green": 0.51, "blue": 0.74},
    "Shipping": {"red": 0.42, "green": 0.66, "blue": 0.31},
}


class TabGrid:
    """Rows plus the formatting hints collected while building them."""

    def __init__(self):
        self.rows: list[list] = []
        self.kinds: list[str] = []          # one of KIND_FORMATS keys or "data"
        self.fmts: list[tuple[str, dict]] = []   # (A1 range, cell format)
        self.pastdue_ranges: list[str] = []      # red-highlight when > 0

    def add(self, cells: list | None = None, kind: str = "data") -> int:
        self.rows.append(cells or [])
        self.kinds.append(kind)
        return len(self.rows)  # 1-based row index

    def fmt(self, rng: str, f: dict) -> None:
        self.fmts.append((rng, f))


def _num(x: float):
    return round(x, 1) if abs(x - round(x)) > 1e-9 else int(round(x))


def _day_label(d: dt.date) -> str:
    return f"{d:%a} {d.month}/{d.day}"


def _backlog_table(g: TabGrid, title: str, backlog) -> None:
    g.add([title], kind="section")
    hdr = g.add(["Class", *wm.DUE_BUCKETS, "Total"], kind="header")
    for c in sorted(backlog, key=lambda c: -sum(backlog[c].values())):
        vals = [backlog[c].get(b, 0) for b in wm.DUE_BUCKETS]
        g.add([c] + [_num(v) for v in vals] + [_num(sum(vals))])
    tot = [sum(backlog[c].get(b, 0) for c in backlog) for b in wm.DUE_BUCKETS]
    last = g.add(["TOTAL"] + [_num(v) for v in tot] + [_num(sum(tot))], kind="total")
    g.fmt(f"B{hdr + 1}:F{last}", INT_FMT)
    if last - 1 > hdr:
        g.pastdue_ranges.append(f"B{hdr + 1}:B{last - 1}")


def build_manufacturing_tab(output, backlog, subcon, days, today, month_label, wd_elapsed) -> TabGrid:
    g = TabGrid()
    g.add([f"MANUFACTURING — refreshed {dt.datetime.now():%Y-%m-%d %H:%M}"], kind="title")
    g.add()
    g.add(["Units finished per class (in-house)"], kind="section")
    hdr = g.add(["Class"] + [_day_label(d) for d in days]
                + ["This Week", f"Avg/Weekday ({month_label})"], kind="header")
    wk_start, month_start = wm.week_start(today), today.replace(day=1)

    def out_row(by_day):
        week = sum(q for d, q in by_day.items() if d >= wk_start)
        month = sum(q for d, q in by_day.items() if d >= month_start)
        return [_num(by_day.get(d, 0)) for d in days] + [_num(week), round(month / wd_elapsed, 1)]

    classes = sorted(output, key=lambda c: -sum(output[c].values()))
    for c in classes:
        g.add([c] + out_row(output[c]))
    total_by_day = {d: sum(output[c].get(d, 0) for c in classes) for d in {d for c in classes for d in output[c]}}
    last = g.add(["TOTAL"] + out_row(total_by_day), kind="total")
    g.fmt(f"B{hdr + 1}:G{last}", INT_FMT)
    g.fmt(f"H{hdr + 1}:H{last}", DEC_FMT)
    g.fmt(f"G{hdr + 1}:H{last}", {"backgroundColor": SUMMARY_BG})

    g.add()
    _backlog_table(g, "Open MO backlog — units to make in-house, by scheduled start", backlog)
    g.add()
    _backlog_table(g, "Subcontracted backlog — units on order with outside vendors", subcon)
    return g


def build_shipping_tab(ships, exc, days, today, month_label, wd_elapsed, tz_name) -> TabGrid:
    g = TabGrid()
    g.add([f"SHIPPING — refreshed {dt.datetime.now():%Y-%m-%d %H:%M}"], kind="title")
    g.add()
    g.add(["Orders shipped from warehouse (Petaluma + Massachusetts; Amazon-fulfilled FBA excluded)"],
          kind="section")
    hdr = g.add([""] + [_day_label(d) for d in days]
                + ["This Week", f"Avg/Weekday ({month_label})"], kind="header")
    wk_start, month_start = wm.week_start(today), today.replace(day=1)

    def chan_rows(label, by_day, kind="data"):
        counts = {d: len(ids) for d, (ids, _) in by_day.items()}
        values = {d: v for d, (_, v) in by_day.items()}
        for name, data, money in ((f"{label} — orders", counts, False), (f"{label} — $", values, True)):
            week = sum(q for d, q in data.items() if d >= wk_start)
            month = sum(q for d, q in data.items() if d >= month_start)
            fmt = (lambda v: round(v)) if money else _num
            r = g.add([name] + [fmt(data.get(d, 0)) for d in days]
                      + [fmt(week), round(month / wd_elapsed, None if money else 1)], kind=kind)
            g.fmt(f"B{r}:H{r}", USD if money else INT_FMT)
            if not money:
                g.fmt(f"H{r}:H{r}", DEC_FMT)

    both = {d: (ids_r | ids_w, vr + vw)
            for d in set(ships["Retail"]) | set(ships["Wholesale"])
            for ids_r, vr in [ships["Retail"].get(d, (set(), 0.0))]
            for ids_w, vw in [ships["Wholesale"].get(d, (set(), 0.0))]}
    first = len(g.rows) + 1
    chan_rows("Retail", ships["Retail"])
    chan_rows("Wholesale", ships["Wholesale"])
    chan_rows("TOTAL", both, kind="total")
    g.fmt(f"G{first}:H{len(g.rows)}", {"backgroundColor": SUMMARY_BG})

    g.add()
    g.add(["Open deliveries (customer orders due today or earlier)"], kind="section")
    g.add(["", "# Orders", "$ Order Total", "$ Unshipped"], kind="header")
    for label, key, kind in (
        ("Due to ship, waiting on manufacturing", "due_mfg", "data"),
        ("Late (past scheduled ship date)", "late", "total"),
        ("   late — waiting on manufacturing", "late_mfg", "data"),
        ("   late — waiting on inventory (not mfg)", "late_inv", "data"),
        ("   late — ready to ship", "late_ready", "data"),
    ):
        e = exc[key]
        r = g.add([label, e["orders"], round(e["total"]), round(e["unshipped"])], kind=kind)
        g.fmt(f"B{r}:B{r}", INT_FMT)
        g.fmt(f"C{r}:D{r}", USD)

    g.add()
    for note in (
        f"Retail = order source in: {', '.join(sorted(RETAIL_SOURCES))}; everything else is wholesale.",
        "$ per day = sale-line value shipped that day (excl. tax, after discounts);"
        " $ Order Total = full order amount incl. tax/shipping.",
        "Waiting on manufacturing = a short item on the delivery is tied to an open MO,"
        " or its product has an open MO or a BOM. Otherwise: waiting on inventory.",
        f"Late = open delivery past its scheduled date. Days bucketed in {tz_name}.",
    ):
        g.add([note], kind="note")
    return g


def print_grid(name: str, g: TabGrid) -> None:
    print(f"\n=== {name} ===")
    widths = {}
    for row in g.rows:
        for i, cell in enumerate(row):
            widths[i] = min(max(widths.get(i, 0), len(str(cell))), 44)
    for row in g.rows:
        print("  ".join(str(c).ljust(widths[i]) for i, c in enumerate(row)).rstrip())


def _wipe_formatting(sh, ws) -> None:
    """Clear all cell formats and conditional rules so reruns never leave stale styling."""
    reqs = [{"updateCells": {"range": {"sheetId": ws.id}, "fields": "userEnteredFormat"}}]
    for meta in sh.fetch_sheet_metadata().get("sheets", []):
        if meta["properties"]["sheetId"] == ws.id:
            n = len(meta.get("conditionalFormats", []))
            reqs += [{"deleteConditionalFormatRule": {"sheetId": ws.id, "index": i}}
                     for i in range(n - 1, -1, -1)]
    sh.batch_update({"requests": reqs})


def write_tab(sh, name: str, g: TabGrid) -> None:
    n_cols = max(len(r) for r in g.rows)
    try:
        ws = sh.worksheet(name)
        ws.clear()
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=name, rows=len(g.rows), cols=n_cols)
    _wipe_formatting(sh, ws)
    ws.resize(rows=len(g.rows), cols=n_cols)
    ws.update(values=g.rows, range_name="A1", value_input_option="USER_ENTERED")
    ws.freeze(cols=1)

    end_col = get_column_letter(n_cols)
    fmts = []
    for r, kind in enumerate(g.kinds, start=1):
        if kind in KIND_FORMATS:
            rng = f"A{r}" if kind in ("title", "note") else f"A{r}:{end_col}{r}"
            fmts.append({"range": rng, "format": KIND_FORMATS[kind]})
    fmts += [{"range": rng, "format": f} for rng, f in g.fmts]
    ws.batch_format(fmts)

    reqs = [
        {"updateDimensionProperties": {
            "range": {"sheetId": ws.id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1},
            "properties": {"pixelSize": 310}, "fields": "pixelSize"}},
        {"updateDimensionProperties": {
            "range": {"sheetId": ws.id, "dimension": "COLUMNS", "startIndex": 1, "endIndex": n_cols},
            "properties": {"pixelSize": 95}, "fields": "pixelSize"}},
        {"updateSheetProperties": {
            "properties": {"sheetId": ws.id, "tabColorStyle": {"rgbColor": TAB_COLORS.get(name, SLATE)}},
            "fields": "tabColorStyle"}},
    ]
    for rng in g.pastdue_ranges:
        reqs.append({"addConditionalFormatRule": {"rule": {
            "ranges": [a1_range_to_grid_range(rng, ws.id)],
            "booleanRule": {"condition": {"type": "NUMBER_GREATER", "values": [{"userEnteredValue": "0"}]},
                            "format": {"backgroundColor": PASTDUE_BG}}}, "index": 0}})
    sh.batch_update({"requests": reqs})


def write_sheet(title: str, grids: dict[str, TabGrid]) -> str:
    sh = GSheets().open_or_create(title)
    for name, g in grids.items():
        write_tab(sh, name, g)
    for w in sh.worksheets():
        if w.title not in grids and w.title == "Sheet1":
            sh.del_worksheet(w)
    return sh.url


def main() -> None:
    ap = argparse.ArgumentParser(description="Build the Warehouse Dashboard Google Sheet")
    ap.add_argument("--prod", action="store_true", help="use the production Odoo profile")
    ap.add_argument("--title", default=DEFAULT_TITLE, help="spreadsheet name to create/reuse")
    ap.add_argument("--dry-run", action="store_true", help="print metrics; don't touch Google Sheets")
    ap.add_argument("--if-due", action="store_true",
                    help="scheduler mode: only refresh when on AC power and a run is owed "
                         f"since {RUN_HOUR:02d}:{RUN_MINUTE:02d}; records success to skip repeats")
    args = ap.parse_args()
    if args.if_due:
        run_if_due(args)
    else:
        build_dashboard(args)


def build_dashboard(args) -> None:
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


def _on_ac_power() -> bool:
    try:
        out = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return True  # not a battery-powered Mac (or pmset missing): don't block the run
    return "AC Power" in out or "Battery Power" not in out


def _read_stamp() -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(STAMP_FILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def run_if_due(args) -> None:
    """Catch-up guard for the scheduler: refresh once per evening, the next time the
    laptop is awake and plugged in. Failure leaves the stamp unwritten, so a run that
    fails (e.g. Wi-Fi not up yet on wake) is retried at the next poll."""
    now = dt.datetime.now().astimezone()
    stamp = f"[{now:%Y-%m-%d %H:%M}]"
    if REQUIRE_AC and not _on_ac_power():
        print(f"{stamp} skip: on battery (will run when next plugged in)")
        return
    last = _read_stamp()
    if not wm.is_due(last, now, RUN_HOUR, RUN_MINUTE):
        print(f"{stamp} skip: already refreshed since {RUN_HOUR:02d}:{RUN_MINUTE:02d} (last {last:%Y-%m-%d %H:%M})")
        return
    print(f"{stamp} due (last success {last}) — refreshing")
    build_dashboard(args)
    STAMP_FILE.parent.mkdir(parents=True, exist_ok=True)
    STAMP_FILE.write_text(now.isoformat())
    print(f"{stamp} success recorded")


if __name__ == "__main__":
    main()
