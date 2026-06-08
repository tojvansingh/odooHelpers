"""Connectivity check for Google Sheets: create a sheet in the shared folder and write to it.

Run: cd inventorymgr && PYTHONPATH=src uv run python scripts/smoke_gsheets.py
"""

from __future__ import annotations

import datetime

from inventorymgr.sources.gsheets import GSheets


def main() -> None:
    g = GSheets()
    title = f"inventorymgr smoke {datetime.datetime.now():%Y-%m-%d %H%M%S}"
    sh = g.create(title)
    sh.sheet1.update_acell("A1", "hello from inventorymgr")
    sh.sheet1.update_acell("A2", "you can delete this test sheet")
    print("created:", title)
    print("url:", sh.url)


if __name__ == "__main__":
    main()
