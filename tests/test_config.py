from inventorymgr.config import load_class_params


def test_dish_towels_params_seeded():
    # Values are user-maintained, so assert they load as positive ints, not exact numbers.
    dt = load_class_params()["Dish Towels"]
    assert dt.class_name == "Dish Towels"
    assert isinstance(dt.moq_step, int) and dt.moq_step > 0
    assert isinstance(dt.lead_days, int) and dt.lead_days > 0


def test_all_classes_present():
    params = load_class_params()
    # 36 distinct Classes were extracted from the Odoo stock export.
    assert len(params) == 36
