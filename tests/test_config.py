from inventorymgr.config import load_class_params


def test_dish_towels_params_seeded():
    params = load_class_params()
    dt = params["Dish Towels"]
    assert dt.moq_step == 50
    assert dt.lead_days == 150


def test_all_classes_present():
    params = load_class_params()
    # 36 distinct Classes were extracted from the Odoo stock export.
    assert len(params) == 36
