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

## Workflow tools (in `scripts/`)

| Script | What it does | Example |
|---|---|---|
| `build_plan_sheet.py` | Customer-aware order plan → Google Sheet (per Class; `--collection`/`--vendor`/`--arrive`) | `... build_plan_sheet.py --prod --class "Pillows" --collection Geography --vendor JKM` |
| `build_air_sheet.py` | AIR-vs-SEA expedite analysis for delayed POs → Google Sheet | `... build_air_sheet.py --prod --po P60165 --po P60167` |
| `create_pos.py` | Draft POs from a plan sheet's Order Qty (final), grouped by vendor (local; `--prod` for production) | `... create_pos.py --sheet <KEY> --tab "Dish Towels" --vendor "Orchid Overseas" --date-planned 2026-11-15 --prod` |
| `split_receipt.py` | Split a PO receipt into AIR + SEA receipts with separate dates (**LOCAL** only) | `... split_receipt.py --po P60165 --air "001D:100" --air-date 2026-08-15 --sea-date 2026-11-15` |
| `cleanup_sheets.py` | Trash obsolete generated sheets in the Drive folder | `... cleanup_sheets.py --apply` |
| `build_warehouse_dashboard.py` | Refresh the "Warehouse Dashboard" Google Sheet: production output + backlog by Class, retail/wholesale shipments, late & blocked orders | `... build_warehouse_dashboard.py --prod` |

## Requested / not yet built

_(add ideas here; each becomes a script above)_
