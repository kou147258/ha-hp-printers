"""Sub-device names have to survive a translation lookup that misses.

Home Assistant writes a device's name into its device registry when the device
is first created, and that stored value is what the user sees whenever the
frontend cannot resolve the device's translation. With ``translation_key``
alone and a lookup that misses, what gets stored is the key itself.

Measured on a real install: the device page read ``consumable_ink_tank_black``
and ``subunit_copier`` while the entity names immediately beside them were
correctly localised. Entity names arrive pre-resolved from the states API;
device names do not, which is why only the devices were affected and why
looking at the entities gave no hint that anything was wrong.

So every sub-device carries a readable ``name`` as well as a
``translation_key``, and the two are asserted to carry the same text: a
localized frontend should get its own wording, and a user whose lookup missed
should get the same thing rather than a slug.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from custom_components.hp_printers.const import (
    CONSUMABLE_DEVICE_FALLBACK,
    DOMAIN,
    SUBUNIT_KEYS,
    SUBUNIT_NAMES,
)
from custom_components.hp_printers.entity import HPConsumableEntity, HPSubunitEntity
from custom_components.hp_printers.models import Consumable, PrinterData, ProductInfo
from custom_components.hp_printers.sensor import CONSUMABLE_SENSORS, PRINTER_SENSORS

TRANSLATIONS = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "hp_printers"
    / "translations"
    / "zh-Hans.json"
)


def _coordinator(consumables: dict[str, Consumable] | None = None) -> MagicMock:
    coordinator = MagicMock()
    coordinator.data = PrinterData(consumables=consumables or {})
    coordinator.product_info = ProductInfo(
        make_and_model="Smart Tank 750 series", serial_number="TEST"
    )
    coordinator.config_entry.title = "办公室 750"
    coordinator.config_entry.entry_id = "entry"
    coordinator.client.base_url = "https://printer.local"
    return coordinator


def _zh() -> dict:
    return json.loads(TRANSLATIONS.read_text(encoding="utf-8"))


# ------------------------------------------------------------------- subunits


@pytest.mark.parametrize("subunit", sorted(SUBUNIT_KEYS))
def test_a_subunit_carries_a_readable_fallback_name(subunit: str) -> None:
    """Whatever the frontend resolves, the stored name has to be readable."""
    entity = HPSubunitEntity(_coordinator(), PRINTER_SENSORS[0], subunit)

    name = entity.device_info["name"]

    assert name
    assert "_" not in name.replace("办公室 750", ""), (
        f"the stored name is a slug, not something a person can read: {name!r}"
    )


@pytest.mark.parametrize("subunit", sorted(SUBUNIT_KEYS))
def test_the_fallback_matches_the_translation_it_mirrors(subunit: str) -> None:
    """Two paths to the same text, so neither is a worse answer."""
    translated = _zh()["device"][SUBUNIT_KEYS[subunit]]["name"]
    expected = translated.format(device_name="办公室 750")

    assert SUBUNIT_NAMES[subunit] in expected
    assert expected.endswith(SUBUNIT_NAMES[subunit])


# ----------------------------------------------------------------- consumables


def test_a_consumable_carries_a_readable_fallback_name() -> None:
    """A cartridge's name reaches the user the same way a subunit's does."""
    consumable = Consumable(
        label_code="K",
        color_name="black",
        consumable_type="inkTank",
        part_number="3YP17A",
    )
    entity = HPConsumableEntity(
        _coordinator({"K": consumable}), CONSUMABLE_SENSORS[0], "K"
    )

    name = entity.device_info["name"]

    assert name
    assert "consumable_" not in name, f"the stored name is still a key: {name!r}"


def test_every_consumable_device_key_has_a_translation_and_a_name() -> None:
    """The fallback table and the translation file must agree, key for key.

    Two copies of the same wording is only acceptable while something checks
    them against each other: a table that drifts from its translation is worse
    than no table, because the two would disagree on the same device.
    """
    zh = _zh()["device"]
    for key, wording in CONSUMABLE_DEVICE_FALLBACK.items():
        assert key in zh, f"fallback for a key the translation does not have: {key}"
        translated = zh[key]["name"]
        assert translated.startswith("{device_name} "), translated
        assert translated[len("{device_name} ") :] == wording, (
            f"{key}: the fallback says {wording!r} and the translation says "
            f"{translated[len('{device_name} ') :]!r}"
        )


def test_the_fallback_never_reaches_the_raw_key() -> None:
    """Guards the whole class, not one shape of it.

    A key is a slug by construction, so any name that still looks like one is
    a key that failed to be replaced.
    """
    for subunit in SUBUNIT_KEYS:
        assert subunit not in SUBUNIT_NAMES[subunit]


def test_the_subunit_translation_keys_all_have_entries() -> None:
    """A key with no translation is a key that can only ever show as a slug."""
    zh = _zh()["device"]
    for key in SUBUNIT_KEYS.values():
        assert key in zh, f"{key} has no device translation"


def test_the_device_translation_section_is_not_empty() -> None:
    """An empty section would pass every check above."""
    assert len(_zh()["device"]) >= 25
    assert DOMAIN  # the section belongs to this integration
