from inventorymgr.config import load_class_params, resolve_class_params


def test_dish_towels_params_seeded():
    # Values are user-maintained, so assert they load as positive ints, not exact numbers.
    dt = load_class_params()[("Dish Towels", "")]
    assert dt.class_name == "Dish Towels"
    assert isinstance(dt.moq_step, int) and dt.moq_step > 0
    assert isinstance(dt.lead_days, int) and dt.lead_days > 0


def test_collection_override_resolves():
    p = load_class_params()
    coll = resolve_class_params(p, "Pillows", "Collegiate")
    assert coll.moq_step == 25 and coll.lead_days == 60
    geo = resolve_class_params(p, "Pillows", "Geography")  # no Geography row -> class default
    assert geo.lead_days == 90 and geo.moq_step == 10


def test_all_classes_present():
    params = load_class_params()
    assert ("Dish Towels", "") in params
    assert len([k for k in params if k[1] == ""]) == 36
