"""Render assembled plans into a Google Sheet for human review.

The sheet is transparent and editable: the monthly Sales (demand) and Incoming values
are written as data, and the inventory projection + recommended order are written as
LIVE FORMULAS that reference them. Edit any input (On Hand, Outgoing, a Sales month, an
Incoming month) and the projection, Proj End, and Recommended update automatically.

Per-row column layout (one tab per Class):
    A product_id (hidden)   E On Hand        I Proj End      M Flags
    B Display Name          F Outgoing       J MOQ           N..   Inv <month>   (formulas)
    C Collection            G Available (=E-F)  K Recommended  ..   Sales <month> (last-yr demand)
    D Avg Cost              H Incoming (=SUM)  L Order Qty(final)  ..  Inc <month>  (PO arrivals)
"""

from __future__ import annotations

import datetime

from openpyxl.utils import get_column_letter as col

BASE_HEADERS = [
    "product_id", "Display Name", "Collection", "Avg Cost", "On Hand", "Outgoing",
    "Available", "Incoming", "Proj End", "MOQ", "Recommended", "Order Qty (final)", "Flags",
]
NB = len(BASE_HEADERS)  # 13 base columns; monthly blocks start at column NB+1


def _headers(months) -> list[str]:
    targets = [f"{y}-{m:02d}" for (y, m) in months]
    sources = [f"{y - 1}-{m:02d}" for (y, m) in months]  # demand = last year's same month
    return (
        BASE_HEADERS
        + [f"Inv {t}" for t in targets]
        + [f"Sales {s}" for s in sources]
        + [f"Inc {t}" for t in targets]
    )


def _grid(months, results, moq) -> list[list]:
    n = len(months)
    inv0, dem0, inc0 = NB + 1, NB + 1 + n, NB + 1 + 2 * n
    inc_first, inc_last, last_inv = col(inc0), col(inc0 + n - 1), col(inv0 + n - 1)
    grid = [_headers(months)]
    for r, (pid, res) in enumerate(
        sorted(results, key=lambda kr: kr[1].recommended_qty, reverse=True), start=2
    ):
        p = res.product
        inv_formulas = []
        for j in range(n):
            demc, incc = col(dem0 + j), col(inc0 + j)
            prev = f"G{r}" if j == 0 else f"{col(inv0 + j - 1)}{r}"
            inv_formulas.append(f"={prev}-{demc}{r}+{incc}{r}")
        grid.append(
            [
                pid, p.display_name or p.name, p.collection, round(p.average_cost or 0, 2),
                p.on_hand, p.outgoing,
                f"=E{r}-F{r}",                              # Available = On Hand - Outgoing
                f"=SUM({inc_first}{r}:{inc_last}{r})",      # Incoming (within horizon)
                f"={last_inv}{r}",                          # Proj End = last projected month
                moq,
                f"=CEILING(MAX(0,-I{r}),MAX(1,J{r}))",      # Recommended = round shortfall up to MOQ
                res.recommended_qty,                        # Order Qty (final) — editable
                ", ".join(res.flags),
            ]
            + inv_formulas
            + list(res.forecasts)
            + list(res.incoming)
        )
    return grid


def write_class_tab(ws, months, results, moq) -> None:
    grid = _grid(months, results, moq)
    ws.resize(rows=max(len(grid), 2), cols=len(grid[0]))
    ws.update(values=grid, range_name="A1", value_input_option="USER_ENTERED")
    ws.freeze(rows=1)
    ws.format("1:1", {"textFormat": {"bold": True}})
    ws.hide_columns(0, 1)  # hide product_id (column A)


def build_review_spreadsheet(gsheets, class_to_plan: dict, title: str | None = None):
    """class_to_plan: {class_name: (months, results, moq_step)}. Returns the gspread Spreadsheet."""
    title = title or f"Inventory Plan {datetime.date.today():%Y-%m-%d}"
    sh = gsheets.create(title)
    for i, (class_name, (months, results, moq)) in enumerate(class_to_plan.items()):
        if i == 0:
            ws = sh.sheet1
            ws.update_title(class_name)
        else:
            ws = sh.add_worksheet(title=class_name, rows=len(results) + 1, cols=NB + 3 * len(months))
        write_class_tab(ws, months, results, moq)
    return sh
