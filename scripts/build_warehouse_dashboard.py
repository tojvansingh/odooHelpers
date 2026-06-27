"""Build the Warehouse Dashboard Google Sheet (manufacturing + shipping metrics).

Run: cd odooHelpers && uv run python scripts/build_warehouse_dashboard.py [--prod] [--dry-run]

Tabs:
  Manufacturing   — units finished per class (last 5 weekdays, this week, avg/weekday
                    this month) and open-MO backlog bucketed by scheduled start,
                    split in-house vs subcontracted.
  MO Detail       — the individual open MOs behind each backlog bucket, with the
                    customer orders each one blocks.
  Shipping        — retail vs wholesale orders shipped (# and $) per day, plus open
                    deliveries: due-but-waiting-on-manufacturing, late and its split
                    (waiting on manufacturing / waiting on inventory / ready).
  SO Detail       — the individual open customer orders behind each Shipping bucket.
  Production Plan — editable per-class daily capacity, a hotspot view (work-days to
                    clear at that capacity), and a prioritized list of MOs to make
                    tomorrow with a ✓ cut-line that fits within capacity. Capacity
                    edits are preserved across refreshes.
"""

from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import subprocess
import sys
from collections import defaultdict
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
# awake. Set REQUIRE_AC True to only run while plugged in (risks never running if the
# laptop is only ever awake on battery).
RUN_HOUR, RUN_MINUTE = 16, 30
REQUIRE_AC = False
STAMP_FILE = pathlib.Path.home() / "Library/Application Support/warehouse-dashboard/last_success.txt"

SLATE = {"red": 0.247, "green": 0.317, "blue": 0.376}
STEEL = {"red": 0.60, "green": 0.66, "blue": 0.71}          # subsection band (lighter slate)
WHITE = {"red": 1, "green": 1, "blue": 1}
HEADER_BG = {"red": 0.937, "green": 0.937, "blue": 0.937}
TOTAL_BG = {"red": 0.965, "green": 0.965, "blue": 0.965}
SUMMARY_BG = {"red": 0.929, "green": 0.957, "blue": 0.988}   # This Week / Avg columns
PASTDUE_BG = {"red": 0.957, "green": 0.80, "blue": 0.80}
PASTDUE_BG_LIGHT = {"red": 0.984, "green": 0.93, "blue": 0.93}
GRAY_TEXT = {"red": 0.45, "green": 0.45, "blue": 0.45}
GREEN_TEXT = {"red": 0.18, "green": 0.49, "blue": 0.20}
EDIT_BG = {"red": 1.0, "green": 0.976, "blue": 0.792}      # editable capacity cells (soft yellow)

KIND_FORMATS = {
    "title": {"textFormat": {"bold": True, "fontSize": 12}},
    "section": {"backgroundColor": SLATE,
                "textFormat": {"bold": True, "foregroundColor": WHITE}},
    "subsection": {"backgroundColor": STEEL,
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
    "MO Detail": {"red": 0.60, "green": 0.73, "blue": 0.88},
    "Shipping": {"red": 0.42, "green": 0.66, "blue": 0.31},
    "SO Detail": {"red": 0.65, "green": 0.80, "blue": 0.56},
    "Production Plan": {"red": 0.90, "green": 0.57, "blue": 0.22},
}


class TabGrid:
    """Rows plus the formatting hints collected while building them."""

    def __init__(self):
        self.rows: list[list] = []
        self.kinds: list[str] = []          # one of KIND_FORMATS keys or "data"
        self.fmts: list[tuple[str, dict]] = []   # (A1 range, cell format)
        self.pastdue_ranges: list[str] = []      # red-highlight when > 0
        self.col_widths: list[int] | None = None  # per-column px; None = default scheme

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


def build_manufacturing_tab(output, backlog, subcon, days, today, month_label, wd_elapsed, tz_name) -> TabGrid:
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

    g.add()
    for note in (
        "Units finished = quantity produced on completed (done) in-house MOs, by finish date"
        f" in {tz_name}. Subcontracted MOs are excluded from this table.",
        "Backlog = units still to make on open MOs (qty to produce − already produced),"
        " bucketed by the MO's scheduled start date vs today: Past due = start date before"
        " today; Next 2 weeks = within 14 days; 2–4 weeks = 14–28 days; >4 weeks = beyond.",
        "In-house = MOs run at Petaluma; Subcontracted = MOs fulfilled by outside vendors"
        " (WH/SBC). See the 'MO Detail' tab for the individual MOs behind each bucket.",
    ):
        g.add([note], kind="note")
    return g


def build_shipping_tab(ships, summary, days, today, month_label, wd_elapsed, tz_name) -> TabGrid:
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
        ("        ready — no review needed", "late_ready_clean", "data"),
        ("        ready — needs review", "late_ready_review", "data"),
    ):
        e = summary[key]
        r = g.add([label, e["orders"], round(e["total"]), round(e["unshipped"])], kind=kind)
        g.fmt(f"B{r}:B{r}", INT_FMT)
        g.fmt(f"C{r}:D{r}", USD)

    g.add()
    for note in (
        f"Retail = order source in: {', '.join(sorted(RETAIL_SOURCES))}; everything else is wholesale.",
        "$ per day = sale-line value shipped that day (excl. tax, after discounts);"
        " $ Order Total = full order amount incl. tax/shipping.",
        "An order is counted under a row if it has an open (not-yet-done) outgoing delivery"
        " whose scheduled date is today or earlier. Late = that scheduled date is before today.",
        "Waiting on manufacturing = a short item on the delivery is tied to an open MO,"
        " or its product has an open MO or a BOM. Otherwise: waiting on inventory; ready = stock"
        " on hand but not yet shipped. See the 'SO Detail' tab for the individual orders.",
        "Ready to ship splits into 'no review needed' (should just ship) vs 'needs review' (the delivery"
        " is flagged to-review — e.g. Routing Guide, Cancel Date, Invoice Due; reasons in SO Detail).",
        f"Late-mfg orders also appear under 'Due to ship, waiting on manufacturing'. Dates in {tz_name}.",
    ):
        g.add([note], kind="note")
    return g


MO_DETAIL_COLS = ["MO", "Product", "Qty to make", "Due bucket", "Start", "Days late", "Components", "Blocking orders"]
MO_DETAIL_WIDTHS = [115, 255, 90, 100, 90, 80, 115, 210]
SO_DETAIL_WIDTHS = [95, 225, 95, 80, 105, 105, 330]


def build_mo_detail_tab(mo_details, today, tz_name) -> TabGrid:
    g = TabGrid()
    g.col_widths = MO_DETAIL_WIDTHS
    bi = {b: i for i, b in enumerate(wm.DUE_BUCKETS)}
    g.add([f"MO DETAIL — open manufacturing orders by class, refreshed {dt.datetime.now():%Y-%m-%d %H:%M}"],
          kind="title")
    for sub, label in ((False, "In-house MOs"), (True, "Subcontracted MOs")):
        items = [m for m in mo_details if m["subcontract"] == sub]
        g.add()
        g.add([f"{label} — {len(items)} open MOs, {int(round(sum(m['qty'] for m in items))):,} units"],
              kind="section")
        if not items:
            g.add(["(none)"], kind="note")
            continue
        classes = sorted({m["klass"] for m in items},
                         key=lambda c: -sum(m["qty"] for m in items if m["klass"] == c))
        for cls in classes:
            crows = sorted([m for m in items if m["klass"] == cls], key=lambda m: (bi[m["bucket"]], -m["qty"]))
            units = int(round(sum(m["qty"] for m in crows)))
            pastdue = int(round(sum(m["qty"] for m in crows if m["days_late"] > 0)))
            head = f"{cls} — {len(crows)} MOs, {units:,} units" + (f"  ({pastdue:,} past due)" if pastdue else "")
            g.add([head], kind="subsection")
            hdr = g.add(MO_DETAIL_COLS, kind="header")
            for m in crows:
                blocking = ", ".join(m["blocking_sos"])
                if m["blocking_sos"] and m["blocking_late"]:
                    blocking = "⚠ " + blocking
                g.add([m["name"], m["product"], _num(m["qty"]), m["bucket"],
                       f"{m['start']:%Y-%m-%d}", m["days_late"] if m["days_late"] > 0 else "",
                       m["components"], blocking])
            g.fmt(f"C{hdr + 1}:C{len(g.rows)}", INT_FMT)
            g.pastdue_ranges.append(f"F{hdr + 1}:F{len(g.rows)}")  # red when days-late > 0
    g.add()
    for note in (
        "Grouped by product Class, then bucket within each class (Past due first). Bucket ="
        " scheduled start vs today; Past-due Days-late cells are highlighted.",
        "Components = Odoo's component-availability status for the MO (e.g. Available / Not Available).",
        "Blocking orders = customer sale orders waiting on this MO (via the make-to-order link);"
        " ⚠ marks that at least one of those deliveries is already past its scheduled date.",
    ):
        g.add([note], kind="note")
    return g


def build_so_detail_tab(exc, today, tz_name) -> TabGrid:
    g = TabGrid()
    g.col_widths = SO_DETAIL_WIDTHS
    g.add([f"SO DETAIL — open customer deliveries by class, refreshed {dt.datetime.now():%Y-%m-%d %H:%M}"],
          kind="title")
    orders = exc["orders"]
    late = [r for r in orders if r["late"]]
    ready = [r for r in late if not r["mfg"] and not r["inv"]]

    def _sched(r):
        return f"{r['scheduled']:%Y-%m-%d}" if r["scheduled"] else ""

    def _late(r):
        return r["days_late"] if r["days_late"] > 0 else ""

    def class_section(title, rows, class_field):
        g.add()
        distinct = len({r["name"] for r in rows})
        total = int(round(sum(r["total"] for r in rows)))
        g.add([f"{title} — {distinct} orders, ${total:,} order value"], kind="section")
        if not rows:
            g.add(["(none)"], kind="note")
            return
        by_cls = defaultdict(list)
        for r in rows:
            for cls in (r[class_field] or {"(no class)": []}):
                by_cls[cls].append(r)
        for cls in sorted(by_cls, key=lambda c: -sum(x["unshipped"] for x in by_cls[c])):
            crows = sorted(by_cls[cls], key=lambda r: -r["days_late"])
            g.add([f"{cls} — {len(crows)} orders"], kind="subsection")
            hdr = g.add(["Order", "Customer", "Scheduled", "Days late", "$ Order Total",
                         "$ Unshipped", f"Waiting on ({cls})"], kind="header")
            for r in crows:
                items = r[class_field].get(cls, [])
                waiting = ", ".join(items[:5]) + (" …" if len(items) > 5 else "")
                g.add([r["name"], r["partner"], _sched(r), _late(r),
                       round(r["total"]), round(r["unshipped"]), waiting])
            g.fmt(f"D{hdr + 1}:D{len(g.rows)}", INT_FMT)
            g.fmt(f"E{hdr + 1}:F{len(g.rows)}", USD)

    def flat_section(title, rows, review=False):
        g.add()
        total = int(round(sum(r["total"] for r in rows)))
        g.add([f"{title} — {len(rows)} orders, ${total:,} order value"], kind="section")
        if not rows:
            g.add(["(none)"], kind="note")
            return
        cols = ["Order", "Customer", "Scheduled", "Days late", "$ Order Total", "$ Unshipped"]
        hdr = g.add(cols + (["Review reason"] if review else []), kind="header")
        for r in sorted(rows, key=lambda r: -r["days_late"]):
            base = [r["name"], r["partner"], _sched(r), _late(r), round(r["total"]), round(r["unshipped"])]
            g.add(base + ([", ".join(r["reasons"])] if review else []))
        g.fmt(f"D{hdr + 1}:D{len(g.rows)}", INT_FMT)
        g.fmt(f"E{hdr + 1}:F{len(g.rows)}", USD)

    class_section("Due to ship, waiting on manufacturing", [r for r in orders if r["mfg"]], "mfg_classes")
    class_section("Late — waiting on manufacturing", [r for r in late if r["mfg"]], "mfg_classes")
    class_section("Late — waiting on inventory (not manufacturing)",
                  [r for r in late if not r["mfg"] and r["inv"]], "inv_classes")
    flat_section("Late — ready to ship, no review needed", [r for r in ready if not r["review"]])
    flat_section("Late — ready to ship, needs review", [r for r in ready if r["review"]], review=True)

    g.add()
    for note in (
        "Sections mirror the Shipping summary. The three waiting sections are grouped by the product"
        " Class of the items being waited on; an order needing several classes appears under each, so"
        " a section's class subtotals can exceed its distinct order count (shown in the section header).",
        "Ready-to-ship orders have stock available but aren't shipped; split into no review needed vs"
        " needs review (the delivery is flagged 'to review' — reasons shown in the last column).",
        "Days late = today − the order's earliest open-delivery scheduled date. 'Waiting on' lists that"
        " class's not-yet-available items (first 5). Late-mfg orders also appear under 'Due to ship'.",
    ):
        g.add([note], kind="note")
    return g


PLAN_TAB = "Production Plan"
PLAN_COLS = ["MO", "Product", "Qty", "Priority", "Components", "Blocking orders", "Cum. units", "Make tmrw?"]
PLAN_WIDTHS = [115, 250, 70, 110, 115, 200, 90, 95]


def _priority(m: dict) -> tuple:
    """Sort key (lower = make sooner): late-order blockers, then any customer order,
    then most-overdue, then soonest bucket, then biggest qty."""
    return (not m["blocking_late"], not bool(m["blocking_sos"]),
            -m["days_late"], wm.DUE_BUCKETS.index(m["bucket"]), -m["qty"])


def _priority_label(m: dict) -> str:
    if m["blocking_late"]:
        return "⚠ Late order"
    if m["blocking_sos"]:
        return "Customer order"
    if m["days_late"] > 0:
        return "Past due"
    return "Stock build"


def build_production_plan_tab(mo_details, backlog, output, today, wd_elapsed, cap_overrides) -> TabGrid:
    g = TabGrid()
    g.col_widths = PLAN_WIDTHS
    month_start = today.replace(day=1)
    inhouse = [m for m in mo_details if not m["subcontract"]]
    classes = sorted({m["klass"] for m in inhouse},
                     key=lambda c: -sum(m["qty"] for m in inhouse if m["klass"] == c))

    def seed(c):
        # Manager's saved edit wins; else a placeholder = best single-day output observed
        # (demonstrated capability beats the average, which low recent volume drags down).
        if c in cap_overrides:
            return cap_overrides[c]
        peak = max(output.get(c, {}).values(), default=0)
        return max(int(round(peak)), 1)

    g.add([f"PRODUCTION PLAN — what to make tomorrow, refreshed {dt.datetime.now():%Y-%m-%d %H:%M}"], kind="title")

    # 1) Editable daily capacity (preserved across refreshes).
    g.add()
    g.add(["Daily capacity — units/day per class  (EDIT these; tweaks are kept on refresh)"], kind="section")
    g.add(["Class", "Units/Day"], kind="header")
    cap_cell = {}
    cap_first = len(g.rows) + 1
    for c in classes:
        r = g.add([c, seed(c)])
        cap_cell[c] = f"$B${r}"
    if classes:
        g.fmt(f"B{cap_first}:B{len(g.rows)}", {"backgroundColor": EDIT_BG, **INT_FMT})

    # 2) Hotspots: live work-days-to-clear from the editable capacity above.
    g.add()
    g.add(["Class load & hotspots — work-days to clear the actionable backlog at the capacity above"],
          kind="section")
    hdr = g.add(["Class", "Past-due", "Due ≤2 wks", "On late orders", "Capacity/day",
                 "Work-days to clear", "Status"], kind="header")
    for c in classes:
        past = int(round(backlog.get(c, {}).get(wm.DUE_BUCKETS[0], 0)))
        soon = int(round(backlog.get(c, {}).get(wm.DUE_BUCKETS[1], 0)))
        on_late = int(round(sum(m["qty"] for m in inhouse if m["klass"] == c and m["blocking_late"])))
        r = g.add([c, past, soon, on_late, f"={cap_cell[c]}",
                   f"=IFERROR(ROUND((B{len(g.rows)+1}+C{len(g.rows)+1})/E{len(g.rows)+1},1),0)",
                   f'=IF(F{len(g.rows)+1}>5,"🔴 HOTSPOT",IF(F{len(g.rows)+1}>2,"🟡 Watch","🟢 OK"))'])
    g.fmt(f"B{hdr+1}:E{len(g.rows)}", INT_FMT)
    g.fmt(f"B{hdr+1}:B{len(g.rows)}", {"backgroundColor": PASTDUE_BG_LIGHT})

    # 3) Prioritized pick list per class; ✓ marks MOs that fit within the day's capacity.
    g.add()
    g.add(["Tomorrow's suggested MOs — prioritized; ✓ = fits within that class's daily capacity"],
          kind="section")
    for c in classes:
        rows = sorted([m for m in inhouse if m["klass"] == c], key=_priority)
        pending = int(round(sum(m["qty"] for m in rows)))
        g.add([f"{c} — {len(rows)} MOs, {pending:,} units pending  (capacity {cap_cell[c].replace('$','')})"],
              kind="subsection")
        hdr = g.add(PLAN_COLS, kind="header")
        first = len(g.rows) + 1
        for m in rows:
            rr = len(g.rows) + 1
            blocking = ", ".join(m["blocking_sos"][:3]) + (" …" if len(m["blocking_sos"]) > 3 else "")
            g.add([m["name"], m["product"], _num(m["qty"]), _priority_label(m), m["components"],
                   blocking, f"=SUM(C{first}:C{rr})", f'=IF(G{rr}<={cap_cell[c]},"✓","")'])
        g.fmt(f"C{first}:C{len(g.rows)}", INT_FMT)
        g.fmt(f"G{first}:G{len(g.rows)}", INT_FMT)
        g.fmt(f"H{first}:H{len(g.rows)}", {"horizontalAlignment": "CENTER",
                                           "textFormat": {"bold": True, "foregroundColor": GREEN_TEXT}})

    g.add()
    for note in (
        "Capacity cells are yours to edit — the hotspot work-days and the ✓ cut-line below recompute live,"
        " and your edits are read back and kept each nightly refresh.",
        "Priority order: ⚠ Late order (MO blocks a past-due customer delivery) → Customer order (blocks an"
        " open order) → Past due (scheduled start passed) → Stock build. Within a tier, most-overdue first.",
        "✓ = this MO fits within the class's daily capacity (cumulative units ≤ capacity). Work-days to clear"
        " = (past-due + due-within-2-weeks units) ÷ capacity/day; 🔴 >5 days, 🟡 >2 days.",
        "Subcontracted MOs are excluded (made by outside vendors). Components = Odoo readiness; 'Not Available'"
        " MOs need parts before they can run.",
    ):
        g.add([note], kind="note")
    return g


def read_capacity_overrides(sh) -> dict[str, float]:
    """Read the manager's edited Units/Day values from the existing Production Plan tab,
    so a refresh preserves them instead of resetting to the seeded defaults."""
    try:
        ws = sh.worksheet(PLAN_TAB)
    except gspread.WorksheetNotFound:
        return {}
    vals = ws.get_all_values()
    out: dict[str, float] = {}
    in_cap = False
    for row in vals:
        head = (row[0] if row else "").strip()
        if head.startswith("Daily capacity"):
            in_cap = True
            continue
        if in_cap:
            if head in ("", "Class"):
                if head == "":  # blank row ends the capacity table
                    break
                continue
            try:
                out[head] = float(str(row[1]).replace(",", ""))
            except (IndexError, ValueError):
                pass
    return out


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

    if g.col_widths:
        width_reqs = [
            {"updateDimensionProperties": {
                "range": {"sheetId": ws.id, "dimension": "COLUMNS", "startIndex": i, "endIndex": i + 1},
                "properties": {"pixelSize": px}, "fields": "pixelSize"}}
            for i, px in enumerate(g.col_widths)
        ]
    else:
        width_reqs = [
            {"updateDimensionProperties": {
                "range": {"sheetId": ws.id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1},
                "properties": {"pixelSize": 310}, "fields": "pixelSize"}},
            {"updateDimensionProperties": {
                "range": {"sheetId": ws.id, "dimension": "COLUMNS", "startIndex": 1, "endIndex": n_cols},
                "properties": {"pixelSize": 95}, "fields": "pixelSize"}},
        ]
    reqs = width_reqs + [
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


def write_sheet(sh, grids: dict[str, TabGrid]) -> str:
    for name, g in grids.items():
        write_tab(sh, name, g)
    for w in sh.worksheets():
        if w.title not in grids and w.title == "Sheet1":
            sh.del_worksheet(w)
    ordered = [sh.worksheet(name) for name in grids]  # detail tab after its summary
    sh.reorder_worksheets(ordered)
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
    blocking = wm.fetch_mo_blocking_map(client, tz, ptype_ids, today)
    mo_details = wm.fetch_mo_details(client, tz, today, blocking)

    # Open the sheet first so we can preserve the manager's edited capacity values.
    sh = None if args.dry_run else GSheets().open_or_create(args.title)
    cap_overrides = read_capacity_overrides(sh) if sh else {}

    grids = {
        "Manufacturing": build_manufacturing_tab(output, backlog, subcon, days, today, month_label, wd_elapsed, str(tz)),
        "MO Detail": build_mo_detail_tab(mo_details, today, str(tz)),
        "Shipping": build_shipping_tab(ships, exc["summary"], days, today, month_label, wd_elapsed, str(tz)),
        "SO Detail": build_so_detail_tab(exc, today, str(tz)),
        "Production Plan": build_production_plan_tab(mo_details, backlog, output, today, wd_elapsed, cap_overrides),
    }
    for name, g in grids.items():
        print_grid(name, g)
    if sh is not None:
        print("\nSheet:", write_sheet(sh, grids))


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
