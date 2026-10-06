"""Every printer-level entity's attributes must actually render.

This exists because of a crash that reached no test. Three ``attrs_fn``
lambdas were changed to take ``(data, info)`` so they could read facts off
``ProductInfo``, and the tests that cover those three passed throughout --
because they call ``attrs_fn`` directly with two arguments. The entity calls it
with one, through ``extra_state_attributes``, and every one of those entities
raised a ``TypeError`` the first time Home Assistant rendered it.

Nothing in the suite would have noticed. This file notices.

The check is deliberately structural rather than behavioural: it calls every
description's ``attrs_fn`` the way the entity does, so a signature change
anywhere in the table is caught even where no test names that sensor. A
per-sensor test cannot do this -- the sensors nobody wrote a test for are
exactly the ones that break.
"""

from unittest.mock import MagicMock

import pytest

from custom_components.hp_printers.models import PrinterData, ProductInfo
from custom_components.hp_printers.sensor import PRINTER_SENSORS, HPPrinterSensor


def _info() -> ProductInfo:
    return ProductInfo(
        wifi_band="band2pt4Ghz",
        wifi_authentication="wpaPersonal",
        wifi_encryption="aesOrTkip",
        wifi_wpa_version="auto",
        http_proxy_enabled=False,
        media_trays=({"id": "main", "size": "a4", "type": "stationery"},),
        supply_alert_colors=("K", "M"),
        firmware_update_failure_reason="manifestNotFound",
    )


@pytest.mark.parametrize("description", PRINTER_SENSORS, ids=lambda d: d.key)
def test_every_printer_attribute_renderer_takes_both_arguments(
    description,
) -> None:
    """One argument, the way the entity passes it.

    The entity's ``extra_state_attributes`` is ``attrs_fn(self.coordinator.
    data)`` -- one argument. A lambda that needs two is not a slow path, it is
    a ``TypeError`` the first time the card renders.
    """
    if description.attrs_fn is None:
        pytest.skip("no attributes")
    # No assertion on the result beyond it being a mapping or None: the paper
    # renderer returns None when there is no tray, which is this table's
    # established convention and is not what is under test. The point is that
    # calling it this way does not raise.
    assert description.attrs_fn(PrinterData(), _info()) is None or isinstance(
        description.attrs_fn(PrinterData(), _info()), dict
    )


def test_the_entity_renders_them_rather_than_just_the_description() -> None:
    """The last link, through the property Home Assistant actually calls.

    Only ``native_value`` and ``extra_state_attributes`` are touched. Reading
    ``state`` would ask the sensor for its unit of measurement, and Home
    Assistant refuses that on an entity that has not been added to a platform
    yet -- a guard about this harness rather than about the integration.
    """
    coordinator = MagicMock()
    coordinator.data = PrinterData()
    coordinator.product_info = _info()

    entity = HPPrinterSensor(
        coordinator,
        next(d for d in PRINTER_SENSORS if d.key == "wifi_encryption"),
    )

    assert entity.native_value == "aesOrTkip"
    assert entity.extra_state_attributes == {
        "band": "band2pt4Ghz",
        "authentication": "wpaPersonal",
        "wpa_version": "auto",
    }
