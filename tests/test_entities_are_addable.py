"""Every entity, built for real, checked against Home Assistant's own rules.

The description tables parse, lint, and are perfectly happy to be wrong.
Home Assistant's answers are stricter, and it expresses them by rejecting the
entity -- with a message that names neither the entity nor the description,
which is why the same fault survived several rounds of table-level checks.

Two of its rules are reproduced here, applied to the built entity rather than
to the description it came from:

* a sensor offering ``options`` must declare the enum device class;
* a sensor with a non-numeric device class must not declare a unit.

Both are read out of Home Assistant's own modules rather than restated, so
this cannot drift from the version the integration actually runs against. The
second rule is what caught a boolean that had been written into the sensor
table: it had an enum device class and no options, so the first rule was
satisfied and only the second one objected -- and the object Home Assistant
rejected was a sensor reading "off" and "on".
"""

from unittest.mock import MagicMock

# Home Assistant's own list of device classes that do not mean a number.
from homeassistant.components.sensor import (
    NON_NUMERIC_DEVICE_CLASSES,
    SensorDeviceClass,
)
import pytest

from custom_components.hp_printers.binary_sensor import (
    CONSUMABLE_BINARY_SENSORS,
    PRINTER_BINARY_SENSORS,
)
from custom_components.hp_printers.entity import HPConsumableEntity, HPSubunitEntity
from custom_components.hp_printers.models import Consumable, PrinterData, ProductInfo
from custom_components.hp_printers.sensor import (
    CONSUMABLE_SENSORS,
    PRINTER_SENSORS,
    HPPrinterSensor,
)

NON_NUMERIC = set(NON_NUMERIC_DEVICE_CLASSES)

CONSUMABLES = {
    "K": Consumable(
        label_code="K",
        color_name="black",
        consumable_type="inkTank",
        level_percent=100.0,
        brand="genuinehp",
        part_number="3YP17A",
    ),
    "CMY": Consumable(
        label_code="CMY",
        color_name="tricolor",
        consumable_type="inkCartridge",
        level_percent=20.0,
        brand="genuinehp",
    ),
}


def _coordinator():
    """A coordinator double carrying the models the entities read."""
    coordinator = MagicMock()
    coordinator.data = PrinterData(consumables=CONSUMABLES, status="ready")
    coordinator.product_info = ProductInfo(
        make_and_model="Smart Tank 750 series", serial_number="TEST"
    )
    coordinator.config_entry.title = "办公室 750"
    coordinator.config_entry.entry_id = "entry"
    coordinator.client.base_url = "https://printer.local"
    coordinator.last_update_success = True
    return coordinator


def _entities() -> list:
    coordinator = _coordinator()
    built = [HPPrinterSensor(coordinator, d) for d in PRINTER_SENSORS]
    for subunit in ("scanner", "copy"):
        built += [
            HPSubunitEntity(coordinator, d, subunit)
            for d in PRINTER_SENSORS
            if d.subunit == subunit
        ]
    for code in CONSUMABLES:
        built += [HPConsumableEntity(coordinator, d, code) for d in CONSUMABLE_SENSORS]
    return built


ENTITIES = _entities()
IDS = [e.entity_description.key for e in ENTITIES]


@pytest.mark.parametrize("entity", ENTITIES, ids=IDS)
def test_a_sensor_with_options_declares_the_enum_device_class(entity) -> None:
    """Home Assistant's first rejection, applied to the built entity."""
    if getattr(entity, "options", None):
        assert entity.device_class is SensorDeviceClass.ENUM, (
            f"{entity.entity_description.key} offers options without the enum "
            "device class, and Home Assistant will refuse the entity"
        )


@pytest.mark.parametrize("entity", ENTITIES, ids=IDS)
def test_a_non_numeric_sensor_declares_no_unit(entity) -> None:
    """Home Assistant's other rejection, and the one that caught the boolean.

    A boolean in the sensor table can satisfy the rule above by offering no
    options at all. What it cannot do is be numeric, and Home Assistant says
    so: a non-numeric device class alongside a unit is a contradiction it
    refuses rather than renders.

    Read off the description rather than off the entity: the entity's
    ``native_unit_of_measurement`` is resolved through the platform, which a
    test double answers with a Mock, and a Mock is truthy.
    """
    description = entity.entity_description
    if description.device_class in NON_NUMERIC:
        assert description.native_unit_of_measurement is None, (
            f"{description.key} is a {description.device_class} with a unit of "
            "measurement, which Home Assistant rejects; a boolean belongs in "
            "binary_sensor.py"
        )


def test_the_inventory_is_not_empty() -> None:
    """A parametrised list that came out empty would pass everything above."""
    assert len(ENTITIES) > 100
    assert len({e.entity_description.key for e in ENTITIES}) > 50


def test_the_binary_platform_is_checked_too() -> None:
    """The binary platform's descriptions are covered too.

    A boolean in the sensor table is only half the possible mistake. The
    descriptions are checked for a key and nothing more: the binary
    platform's own rules are Home Assistant's to enforce, and duplicating
    them here is how the copy drifts.
    """
    descriptions = (*PRINTER_BINARY_SENSORS, *CONSUMABLE_BINARY_SENSORS)
    assert len(descriptions) > 20
    for description in descriptions:
        assert description.key, "a binary sensor without a key cannot be built"


def test_the_proxy_flag_lives_in_the_binary_platform() -> None:
    """The entity this whole file was written for, named so it is not a story.

    It is a boolean. It was written into the sensor table, given the enum
    device class, offered no options, and therefore satisfied the first rule
    while Home Assistant rejected it on the second.
    """
    sensor_keys = {d.key for d in PRINTER_SENSORS} | {d.key for d in CONSUMABLE_SENSORS}
    binary_keys = {d.key for d in PRINTER_BINARY_SENSORS} | {
        d.key for d in CONSUMABLE_BINARY_SENSORS
    }

    assert "http_proxy_enabled" not in sensor_keys
    assert "http_proxy_enabled" in binary_keys


def test_the_two_platforms_do_not_publish_the_same_key() -> None:
    """A key in both is an entity Home Assistant will render twice."""
    sensor_keys = {d.key for d in PRINTER_SENSORS} | {d.key for d in CONSUMABLE_SENSORS}
    binary_keys = {d.key for d in PRINTER_BINARY_SENSORS} | {
        d.key for d in CONSUMABLE_BINARY_SENSORS
    }

    assert not (sensor_keys & binary_keys), sensor_keys & binary_keys
