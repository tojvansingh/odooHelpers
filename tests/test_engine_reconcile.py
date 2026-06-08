"""Reconcile the engine against the hand-built sample plan.

The engine must reproduce the "4.2 DT Plan" order column exactly, except for the
cells where the user manually overrode the formula (custom/discontinued items and
new-product initial buys). Verified 2026-06-07: those overrides are exactly the
rows below — everything else matches the mechanical formula.
"""

from pathlib import Path

from inventorymgr.engine import make_plan
from inventorymgr.sources.sample_plan import MOQ_STEP, load_sample_plan

SAMPLE = Path(__file__).resolve().parents[1] / "All Products 4.3.xlsx"

# Sheet rows (1-indexed) where the human replaced the order formula with a hardcode:
#   Custom/one-off:      216 Biltmore, 217 Biltmore Christmas, 337 US Open (custom)
#   Discontinued/skip:   223 Air Force, 319 Berkeley Athletic, 336 Montclair
#   New-product buys:    322 Brown, 323 Pepperdine, 324 Howard, 325 Maryland,
#                        326 Gonzaga, 327 Loyola Marymount
KNOWN_OVERRIDES = {216, 217, 223, 319, 322, 323, 324, 325, 326, 327, 336, 337}


def test_engine_reproduces_sample_order_column():
    rows = load_sample_plan(SAMPLE)
    assert len(rows) == 334  # all Dish Towels rows

    mismatches = {
        sheet_row
        for sheet_row, pi, sheet_order in rows
        if make_plan(pi, MOQ_STEP).baseline_qty != sheet_order
    }
    assert mismatches == KNOWN_OVERRIDES, (
        f"unexpected: {sorted(mismatches - KNOWN_OVERRIDES)}; "
        f"missing: {sorted(KNOWN_OVERRIDES - mismatches)}"
    )


def test_match_count():
    rows = load_sample_plan(SAMPLE)
    matches = sum(
        1 for _, pi, sheet_order in rows if make_plan(pi, MOQ_STEP).baseline_qty == sheet_order
    )
    assert matches == 322
