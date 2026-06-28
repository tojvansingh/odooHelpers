# Odoo utility scripts

Small, callable scripts for ad-hoc Odoo interactions — a running tally of things to automate.
Run from the project root:

```
cd odooHelpers
uv run python scripts/odoo/<script>.py [args] [--prod]
```

`--prod` targets production (`catstudio.odoo.com`); without it, the local Docker Odoo.

| Script | What it does | Example |
|---|---|---|
| `scripts/odoo/po_units.py` | Total units on order in a PO — ordered / received / remaining | `uv run python scripts/odoo/po_units.py --po P60165 --prod` |
| `scripts/odoo/po_set_price.py` | Set the unit price on every line of a PO (`--prod` for production) | `uv run python scripts/odoo/po_set_price.py --po P60261 --price 2.95 --prod` |
| `scripts/odoo/po_fix_descriptions.py` | Backfill vendor product codes into a PO's line descriptions (for POs made before create_pos.py set them); `--dry-run` to preview | `uv run python scripts/odoo/po_fix_descriptions.py --po P60274 --prod --dry-run` |

## Workflow tools (in `scripts/`)

| Script | What it does | Example |
|---|---|---|
| `build_plan_sheet.py` | Customer-aware order plan → Google Sheet. Collection can be set per `--class` as `Class:Collection` — comma-separate for multiple (`Pillows:Geography,Astrology`) and/or prefix `<>` to exclude (`Pillows:<>Holiday`); `--collection` is the default for classes lacking their own. Includes a 3M/6M sales-vs-last-year trend block (big % swings highlighted). Also `--vendor`/`--arrive`. Run `-h` for full column docs | `... build_plan_sheet.py --prod --class "Pillows:Geography,Astrology" --class "Dish Towels:<>Holiday"` |
| `build_air_sheet.py` | AIR-vs-SEA expedite analysis for delayed POs → Google Sheet | `... build_air_sheet.py --prod --po P60165 --po P60167` |
| `create_pos.py` | Draft POs from a plan sheet's Order Qty (final), grouped by vendor (local; `--prod` for production). `--tab` is repeatable — rows from all tabs merge, so several classes for one vendor collapse into a single PO | `... create_pos.py --sheet <KEY> --tab "Dish Towels" --tab "Pillows" --vendor "Orchid Overseas" --date-planned 2026-11-15 --prod` |
| `split_receipt.py` | Split a PO receipt into AIR + SEA receipts with separate dates (**LOCAL** only) | `... split_receipt.py --po P60165 --air "001D:100" --air-date 2026-08-15 --sea-date 2026-11-15` |
| `cleanup_sheets.py` | Trash obsolete generated sheets in the Drive folder | `... cleanup_sheets.py --apply` |
| `build_warehouse_dashboard.py` | Refresh the "Shipping and Manufacturing Dashboard" Google Sheet: Overview KPIs, Manufacturing + MO Detail, Shipping + SO Detail, and a Production Plan tab (editable per-class capacity & staffing rate, hotspots, prioritized parts-aware MOs to make tomorrow) | `... build_warehouse_dashboard.py --prod` |

## Requested / not yet built

_(add ideas here; each becomes a script above)_
