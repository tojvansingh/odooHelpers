"""List/trash obsolete generated sheets in the Drive folder.

Dry run by default; pass --apply to move matches to Drive trash (recoverable 30 days).
Targets smoke tests and the old dated "AIR Plan 2026-..." sheets (superseded by the
single reused "AIR Plan — <POs>" sheet). Leaves everything else (incl. plan sheets).

Run: cd inventorymgr && PYTHONPATH=src uv run python scripts/cleanup_sheets.py [--apply]
"""

from __future__ import annotations

import argparse

from inventorymgr.sources.gsheets import GSheets


def is_obsolete(name: str) -> bool:
    return name.startswith("inventorymgr smoke") or name.startswith("AIR Plan 2026-")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually trash matches (default: dry run)")
    args = ap.parse_args()

    g = GSheets()
    trashed = 0
    for f in sorted(g.list_in_folder(), key=lambda x: x["name"]):
        obsolete = is_obsolete(f["name"])
        print(("TRASH  " if obsolete else "keep   ") + f["name"])
        if obsolete and args.apply:
            g.trash(f["id"])
            trashed += 1
    print(f"\n{'trashed ' + str(trashed) if args.apply else 'dry run — pass --apply to trash'}")


if __name__ == "__main__":
    main()
