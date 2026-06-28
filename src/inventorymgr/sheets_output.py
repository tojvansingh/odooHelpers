"""Render assembled plans into a Google Sheet for human review (customer-aware demand).

One tab per Class, reused/overwritten each run. The projection starts from On Hand
and consumes, each month, MAX(Sales forecast, Booked-returning) + Booked-new — so
committed orders from returning customers aren't double-counted, while new-customer
orders add on top. Inventory projection, Proj End, and Recommended are live formulas.

Per-row layout: base cols A–R (incl. a trailing-sales trend block — 3M/6M units this
year vs the same months last year, with % change), then five monthly blocks: Inv
(formula), Sales (last-yr forecast), Booked-ret, Booked-new, Inc.
"""

from __future__ import annotations

import gspread
from openpyxl.utils import get_column_letter as col

BASE_HEADERS = [
    "product_id", "Display Name", "Collection", "Avg Cost", "On Hand", "Outgoing",
    "Incoming", "Proj End", "MOQ", "Recommended", "Order Qty (final)", "Flags",
    # trailing-sales trend: this-year units vs the same months last year, with % change.
    "Sales 3M", "3M LY", "3M Δ%", "Sales 6M", "6M LY", "6M Δ%",
]
NB = len(BASE_HEADERS)  # 18; monthly blocks start at NB+1. On Hand=E, Proj End=H, MOQ=I (unchanged)
TY3, LY3, D3, TY6, LY6, D6 = 13, 14, 15, 16, 17, 18  # 1-based column indices of the trend block
NEG_FILL = {"red": 0.96, "green": 0.80, "blue": 0.80}
POS_FILL = {"red": 0.80, "green": 0.94, "blue": 0.80}
CHG_THRESHOLD = 0.30  # |% change| >= this gets highlighted (up=green, down=red)


def _headers(months) -> list[str]:
    tgt = [f"{y}-{m:02d}" for (y, m) in months]
    src = [f"{y - 1}-{m:02d}" for (y, m) in months]
    return (
        BASE_HEADERS
        + [f"Inv {t}" for t in tgt]
        + [f"Sales {s}" for s in src]
        + [f"Booked-ret {t}" for t in tgt]
        + [f"Booked-new {t}" for t in tgt]
        + [f"Inc {t}" for t in tgt]
    )


def _grid(months, results, moq, windows) -> list[list]:
    n = len(months)
    inv0, sal0, br0, bn0, inc0 = NB + 1, NB + 1 + n, NB + 1 + 2 * n, NB + 1 + 3 * n, NB + 1 + 4 * n
    inc_first, inc_last, last_inv = col(inc0), col(inc0 + n - 1), col(inv0 + n - 1)
    grid = [_headers(months)]
    for r, (pid, res) in enumerate(
        sorted(results, key=lambda kr: kr[1].recommended_qty, reverse=True), start=2
    ):
        p = res.product
        inv = []
        for j in range(n):
            s, br, bn, ic = col(sal0 + j), col(br0 + j), col(bn0 + j), col(inc0 + j)
            prev = f"E{r}" if j == 0 else f"{col(inv0 + j - 1)}{r}"
            # month-end inv = prev - demand + incoming;  demand = MAX(Sales, Booked-ret) + Booked-new
            inv.append(f"={prev}-(MAX({s}{r},{br}{r})+{bn}{r})+{ic}{r}")
        ty3, ly3, ty6, ly6 = windows.get(pid, (0, 0, 0, 0))
        # % change = (this yr - last yr) / last yr; blank when there's no prior-year baseline
        d3 = f'=IF({col(LY3)}{r}=0,"",({col(TY3)}{r}-{col(LY3)}{r})/{col(LY3)}{r})'
        d6 = f'=IF({col(LY6)}{r}=0,"",({col(TY6)}{r}-{col(LY6)}{r})/{col(LY6)}{r})'
        grid.append(
            [
                pid, p.display_name or p.name, p.collection, round(p.average_cost or 0, 2),
                p.on_hand, p.outgoing,
                f"=SUM({inc_first}{r}:{inc_last}{r})",   # Incoming (within horizon)
                f"={last_inv}{r}",                       # Proj End = last projected month
                moq,
                f"=CEILING(MAX(0,-H{r}),MAX(1,I{r}))",   # Recommended = round shortfall up to MOQ
                res.recommended_qty,                     # Order Qty (final) — editable
                ", ".join(res.flags),
                ty3, ly3, d3, ty6, ly6, d6,              # trailing-sales trend
            ]
            + inv
            + list(res.forecasts)
            + list(res.booked_returning)
            + list(res.booked_new)
            + list(res.incoming)
        )
    return grid


def _apply_shading(ws, n_months: int, n_rows: int) -> None:
    if n_rows < 1:
        return
    end = 1 + n_rows
    neg_ranges = [
        {"sheetId": ws.id, "startRowIndex": 1, "endRowIndex": end,
         "startColumnIndex": NB, "endColumnIndex": NB + n_months},  # Inv block
        {"sheetId": ws.id, "startRowIndex": 1, "endRowIndex": end,
         "startColumnIndex": 7, "endColumnIndex": 8},               # Proj End (col H)
    ]
    # the two Δ% columns (0-based indices), for the big-change highlight
    chg_ranges = [
        {"sheetId": ws.id, "startRowIndex": 1, "endRowIndex": end,
         "startColumnIndex": D3 - 1, "endColumnIndex": D3},
        {"sheetId": ws.id, "startRowIndex": 1, "endRowIndex": end,
         "startColumnIndex": D6 - 1, "endColumnIndex": D6},
    ]
    rule = lambda ranges, cond, fill: {"addConditionalFormatRule": {
        "rule": {"ranges": ranges, "booleanRule": {"condition": cond, "format": {"backgroundColor": fill}}},
        "index": 0}}
    t = str(CHG_THRESHOLD)
    ws.spreadsheet.batch_update({"requests": [
        rule(neg_ranges, {"type": "NUMBER_LESS", "values": [{"userEnteredValue": "0"}]}, NEG_FILL),
        rule(chg_ranges, {"type": "NUMBER_GREATER_THAN_EQ", "values": [{"userEnteredValue": t}]}, POS_FILL),
        rule(chg_ranges, {"type": "NUMBER_LESS_THAN_EQ", "values": [{"userEnteredValue": "-" + t}]}, NEG_FILL),
    ]})


def write_class_tab(ws, months, results, moq, windows) -> None:
    grid = _grid(months, results, moq, windows)
    ws.resize(rows=max(len(grid), 2), cols=len(grid[0]))
    ws.update(values=grid, range_name="A1", value_input_option="USER_ENTERED")
    ws.freeze(rows=1)
    ws.format("1:1", {"textFormat": {"bold": True}})
    ws.hide_columns(0, 1)
    end = max(len(grid), 2)
    pct = {"numberFormat": {"type": "PERCENT", "pattern": "0%"}}
    ws.format(f"{col(D3)}2:{col(D3)}{end}", pct)
    ws.format(f"{col(D6)}2:{col(D6)}{end}", pct)
    _apply_shading(ws, len(months), len(grid) - 1)


def _reset_tab(sh, name: str, rows: int, cols: int):
    try:
        ws = sh.worksheet(name)
    except gspread.WorksheetNotFound:
        return sh.add_worksheet(title=name, rows=rows, cols=cols)
    ws.clear()
    for meta in sh.fetch_sheet_metadata().get("sheets", []):
        if meta["properties"]["sheetId"] == ws.id:
            k = len(meta.get("conditionalFormats", []))
            if k:
                sh.batch_update({"requests": [
                    {"deleteConditionalFormatRule": {"sheetId": ws.id, "index": i}}
                    for i in range(k - 1, -1, -1)
                ]})
    ws.resize(rows=rows, cols=cols)
    return ws


def build_review_spreadsheet(gsheets, class_to_plan: dict, title: str):
    """class_to_plan: {class_name: (months, results, moq_step, windows)}. Reuses the named spreadsheet.

    windows: {product_id: (sales_3m, sales_3m_last_yr, sales_6m, sales_6m_last_yr)}."""
    sh = gsheets.open_or_create(title)
    keep = list(class_to_plan.keys())
    for class_name, (months, results, moq, windows) in class_to_plan.items():
        ws = _reset_tab(sh, class_name, max(len(results) + 1, 2), NB + 5 * len(months))
        write_class_tab(ws, months, results, moq, windows)
    for w in sh.worksheets():
        if w.title not in keep:
            sh.del_worksheet(w)
    return sh
