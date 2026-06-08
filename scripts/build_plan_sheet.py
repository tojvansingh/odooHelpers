"""Pull from Odoo, assemble plans, and write a review Google Sheet.

Run: cd inventorymgr && PYTHONPATH=src uv run python scripts/build_plan_sheet.py --class "Dish Towels"
"""

from __future__ import annotations

import argparse
import datetime

from inventorymgr.assemble import assemble_plan
from inventorymgr.config import load_class_params
from inventorymgr.sheets_output import build_review_spreadsheet
from inventorymgr.sources.gsheets import GSheets
from inventorymgr.sources.odoo_client import OdooClient
from inventorymgr.sources.odoo_source import (
    read_monthly_sales,
    read_open_po_remaining,
    read_products_by_class,
)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--class", dest="classes", action="append", help="Class name (repeatable)")
    args = ap.parse_args()
    classes = args.classes or ["Dish Towels"]

    client = OdooClient()
    params_all = load_class_params()
    today = datetime.date.today()

    blocks: dict = {}
    for cls in classes:
        params = params_all.get(cls)
        if params is None:
            raise SystemExit(f"No params for class {cls!r} in data/class_params.csv")
        products = read_products_by_class(client, cls)
        pids = list(products)
        sales = read_monthly_sales(client, pids)
        remaining = read_open_po_remaining(client, pids)
        months, results = assemble_plan(products, sales, remaining, params, today.year, today.month)
        blocks[cls] = (months, results, params.moq_step)
        print(f"{cls}: {len(products)} products, {len(months)}-mo horizon")

    sh = build_review_spreadsheet(GSheets(), blocks)
    print("\nCreated:", sh.title)
    print("URL:", sh.url)


if __name__ == "__main__":
    main()
