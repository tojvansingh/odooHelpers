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
| `po_units.py` | Total units on order in a PO — ordered / received / remaining | `uv run python scripts/odoo/po_units.py --po P60165 --prod` |

## Requested / not yet built

_(add ideas here; each becomes a script above)_
