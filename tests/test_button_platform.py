"""What the button platform actually creates, per client.

Counting the description table answers a different question from the one the
user asked, and the difference is exactly where the LEDM model lost its
buttons: the table said fourteen, the platform created none, and only the
platform's number is visible in Home Assistant.

The gate is ``getattr(client, "can_write", False)``. A client that does not
define the property is read-only as far as the buttons are concerned, and it
says nothing -- no error, no failed entity, an empty card. So these tests
drive ``async_setup_entry`` for each client class, and one of them pins the
thing that let it happen: both clients must declare a write path.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.hp_printers.api import LEDMClient
from custom_components.hp_printers.api_cdp import CDPClient
from custom_components.hp_printers.button import BUTTONS, async_setup_entry
from custom_components.hp_printers.models import PrinterData, ProductInfo

INTERNAL_JOBS = (
    "cleaningPage",
    "cleaningPageLevel2",
    "cleaningPageLevel3",
    "ribSmearCleaningPage",
    "cleaningVerificationPage",
    "pqDiagnosticsPage",
    "configurationPage",
    "diagnosticsPage",
    "eventLogReport",
    "networkSummary",
    "extendedConfigurationPage",
    "wirelessNetworkPage",
    "privacyReport",
)


def _entry() -> MagicMock:
    """A config entry whose runtime_data is a coordinator-shaped double.

    Built with a plain MagicMock rather than ``spec=``: the platform reaches
    for ``config_entry`` and ``product_info`` as well as ``client`` and
    ``data``, and a spec only permits the ones the class body happens to name.
    """
    entry = MagicMock()
    entry.title = "Office 750"
    coordinator = MagicMock()
    coordinator.client = None
    coordinator.data = PrinterData()
    coordinator.product_info = ProductInfo(make_and_model="Smart Tank")
    coordinator.config_entry = entry
    entry.runtime_data = coordinator
    return entry


def _client(cls, reports: dict | None = None, jobs: tuple = (), calibrations=()):
    client = cls(MagicMock(), "printer.local", 443, True, False)
    client.async_get_reports = AsyncMock(return_value=reports or {})
    client.async_get_internal_jobs = AsyncMock(return_value=jobs)
    client.async_get_calibration_capabilities = AsyncMock(
        return_value={"availableCalibrations": list(calibrations)}
    )
    return client


async def _setup(client) -> list:
    entry = _entry()
    entry.runtime_data.client = client
    added: list = []
    await async_setup_entry(MagicMock(), entry, lambda entities: added.extend(entities))
    return added


# --------------------------------------------------------------- the gate


def test_both_protocols_declare_a_write_path() -> None:
    """The property is load-bearing, and its absence is silent.

    ``button.py`` reads it with ``getattr(..., False)``, so a client missing it
    is treated as read-only and gets no buttons, no error and no entity. The
    LEDM client did not define it for the whole life of this integration, and
    the buttons it has always documented never appeared.
    """
    for cls in (LEDMClient, CDPClient):
        client = cls(MagicMock(), "printer.local", 443, True, False)
        assert client.can_write is True, (
            f"{cls.__name__} does not declare a write path, so the button "
            "platform creates nothing for it"
        )


# ------------------------------------------------------- what gets created


async def test_the_ledm_model_gets_its_fourteen_buttons() -> None:
    """The number Home Assistant will show, not the number in the table."""
    client = _client(LEDMClient, jobs=INTERNAL_JOBS, calibrations=("Alignment",))

    created = await _setup(client)

    assert len(created) == 14, [d.entity_description.key for d in created]
    assert any(d.entity_description.key == "ledm_calibrate_printhead" for d in created)
    assert any(d.entity_description.key == "ledm_clean_ink_light" for d in created)


async def test_the_cdp_model_gets_its_fifteen() -> None:
    """Same question for the CDP model, which had buttons all along."""
    client = _client(
        CDPClient,
        reports={d.report_id: {} for d in BUTTONS if d.report_id},
        calibrations=("penAlignSemiauto",),
    )

    created = await _setup(client)

    assert len(created) == 15, [d.entity_description.key for d in created]
    assert any(d.entity_description.key == "calibrate_printhead" for d in created)


async def test_a_model_with_no_maintenance_surface_creates_nothing() -> None:
    """Absent is not an error, and it is not a crash either."""
    client = _client(LEDMClient, jobs=(), calibrations=())

    created = await _setup(client)

    assert created == []


@pytest.mark.parametrize(
    ("client_factory", "expected_key"),
    [
        (
            lambda: _client(
                LEDMClient, jobs=INTERNAL_JOBS, calibrations=("Alignment",)
            ),
            "ledm_calibrate_printhead",
        ),
        (
            lambda: _client(
                CDPClient,
                reports={d.report_id: {} for d in BUTTONS if d.report_id},
                calibrations=("penAlignSemiauto",),
            ),
            "calibrate_printhead",
        ),
    ],
)
async def test_the_alignment_button_exists_on_both_models(
    client_factory, expected_key
) -> None:
    """The one operation both protocols have, named by each in its own words."""
    created = await _setup(client_factory())

    keys = [d.entity_description.key for d in created]
    assert expected_key in keys
