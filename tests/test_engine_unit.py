from inventorymgr.engine import ceil_to_step, make_plan, project
from inventorymgr.model import PlanInput, Product


def mk(**kw) -> Product:
    base = dict(
        name="X",
        display_name="[X] X",
        class_name="Dish Towels",
        collection="Geography",
        on_hand=0,
        incoming=0,
        outgoing=0,
    )
    base.update(kw)
    return Product(**base)


def test_ceil_to_step():
    assert ceil_to_step(0, 50) == 0
    assert ceil_to_step(1, 50) == 50
    assert ceil_to_step(50, 50) == 50
    assert ceil_to_step(51, 50) == 100
    assert ceil_to_step(-10, 50) == 0


def test_project_with_incoming_matches_sample_row4():
    forecasts = [80, 21, 26, 57, 73, 33, 45, 30, 64, 57, 28]
    series = project(90, forecasts, {2: 400})
    assert series[-1] == -24


def test_order_qty_rounds_shortfall_up_to_moq():
    res = make_plan(PlanInput(mk(), [10, 10, 10], {}), 50)
    assert res.ending_inventory == -30
    assert res.baseline_qty == 50


def test_custom_is_not_auto_reordered():
    res = make_plan(PlanInput(mk(collection="Custom"), [100], {}), 50)
    assert res.baseline_qty == 100  # mechanical baseline still computed
    assert "custom" in res.flags
    assert res.recommended_qty == 0  # but recommendation is "don't auto-order"


def test_no_history_flag():
    res = make_plan(PlanInput(mk(on_hand=200), [0, 0, 0], {}), 50)
    assert "no_history" in res.flags
    assert res.baseline_qty == 0


def test_oos_flags():
    res = make_plan(PlanInput(mk(on_hand=5, outgoing=10), [0], {}), 50)
    assert "oos" in res.flags
    assert "oos_no_incoming" in res.flags
