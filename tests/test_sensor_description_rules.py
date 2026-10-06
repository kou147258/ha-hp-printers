"""Sensor descriptions have to satisfy the rules Home Assistant enforces.

Home Assistant refuses to add a sensor that offers ``options`` without
``device_class = SensorDeviceClass.ENUM``, and raises again on every
subsequent state write. Neither failure names the description that caused it:
one shows up as a missing entity, the other as a coordinator throwing on every
poll. Six sensors here were written with ``state_class=SensorDeviceClass.ENUM``
instead -- a slip that no linter catches, because ``state_class`` accepts any
string and ``SensorDeviceClass.ENUM`` happens to be one.

So the table is checked as a whole, not per sensor: the descriptions that are
wrong are by definition the ones nobody wrote a test for.
"""

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass

from custom_components.hp_printers.sensor import CONSUMABLE_SENSORS, PRINTER_SENSORS

DESCRIPTIONS = (*PRINTER_SENSORS, *CONSUMABLE_SENSORS)


def test_every_sensor_with_options_declares_the_enum_device_class() -> None:
    """Without this Home Assistant drops the entity and then throws on write."""
    offenders = [
        d.key
        for d in DESCRIPTIONS
        if getattr(d, "options", None) and d.device_class is not SensorDeviceClass.ENUM
    ]
    assert not offenders, (
        f"sensors offering options without the enum device class: {offenders}"
    )


def test_no_description_mistakes_a_state_class_for_a_device_class() -> None:
    """The exact slip, pinned so it cannot come back.

    ``state_class`` is for measurements and ``SensorStateClass`` has no enum
    member, so a value from the other enum is a type error the linter cannot
    see -- both are plain strings at the point of the call.
    """
    valid = {c.value for c in SensorStateClass}
    offenders = [
        d.key
        for d in DESCRIPTIONS
        if d.state_class is not None and d.state_class not in valid
    ]
    assert not offenders, f"state_class values that are not measurements: {offenders}"


def test_a_state_class_is_only_used_where_it_means_something() -> None:
    """An enum or a categorical reading has no state class to report."""
    offenders = [
        d.key
        for d in DESCRIPTIONS
        if d.state_class is not None
        and (d.options or d.device_class is SensorDeviceClass.ENUM)
    ]
    assert not offenders, (
        "these describe a category rather than a measurement, so a state "
        f"class on them is meaningless: {offenders}"
    )


def test_the_table_is_not_empty() -> None:
    """A check over an empty table passes; this makes that impossible."""
    assert len(DESCRIPTIONS) > 100
