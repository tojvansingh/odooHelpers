"""Render assembled plans into a Google Sheet for human review (customer-aware demand).

One tab per Class, reused/overwritten each run. The projection starts from On Hand
and consumes, each month, MAX(Sales forecast, Booked-returning) + Booked-new — so
committed orders from returning customers aren't double-counted, while new-customer
orders add on top. Inventory projection, Proj End, and Recommended are live formulas.

Per-row layout: base cols A–L, then five monthly blocks: Inv (formula), Sales (last-yr
forecast), Booked-ret, Booked-new, Inc.
"""

from __future__ import annotations

import gspread
from openpyxl.utils import get_column_letter as col

BASE_HEADERS = [
    "product_id", "Display Name", "Collection", "Avg Cost", "On Hand", "Outgoing",
    "Incoming", "Proj End", "MOQ", "Recommended", "Order Qty (final)", "Flags",
]
NB = len(BASE_HEADERS)  # 12; monthly blocks start at NB+1
NEG_FILL = {"red": 0.96, "green": 0.80, "blue": 0.80}


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


def _grid(months, results, moq) -> list[list]:
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
    ranges = [
        {"sheetId": ws.id, "startRowIndex": 1, "endRowIndex": end,
         "startColumnIndex": NB, "endColumnIndex": NB + n_months},  # Inv block
        {"sheetId": ws.id, "startRowIndex": 1, "endRowIndex": end,
         "startColumnIndex": 7, "endColumnIndex": 8},               # Proj End (col H)
    ]
    ws.spreadsheet.batch_update({"requests": [{"addConditionalFormatRule": {
        "rule": {"ranges": ranges,
                 "booleanRule": {"condition": {"type": "NUMBER_LESS", "values": [{"userEnteredValue": "0"}]},
                                 "format": {"backgroundColor": NEG_FILL}}},
        "index": 0}}]})


def write_class_tab(ws, months, results, moq) -> None:
    grid = _grid(months, results, moq)
    ws.resize(rows=max(len(grid), 2), cols=len(grid[0]))
    ws.update(values=grid, range_name="A1", value_input_option="USER_ENTERED")
    ws.freeze(rows=1)
    ws.format("1:1", {"textFormat": {"bold": True}})
    ws.hide_columns(0, 1)
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
    """class_to_plan: {class_name: (months, results, moq_step)}. Reuses the named spreadsheet."""
    sh = gsheets.open_or_create(title)
    keep = list(class_to_plan.keys())
    for class_name, (months, results, moq) in class_to_plan.items():
        ws = _reset_tab(sh, class_name, max(len(results) + 1, 2), NB + 5 * len(months))
        write_class_tab(ws, months, results, moq)
    for w in sh.worksheets():
        if w.title not in keep:
            sh.del_worksheet(w)
    return sh
