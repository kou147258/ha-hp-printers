"""Tests for the maintenance buttons, through the entity layer.

The client's write path has its own tests. These check the part a user
actually touches: which buttons appear on a given printer, and which ones
refuse to fire.

The refusals are the point. A button that fires when it should not spends ink
and paper, and no amount of correct behaviour elsewhere makes that acceptable.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from homeassistant.exceptions import HomeAssistantError
import pytest

from custom_components.hp_printers import button as button_platform
from custom_components.hp_printers.api import HPPrinterWriteError
from custom_components.hp_printers.button import BUTTONS, HPMaintenanceButton

REPORTS = {
    "cleaningPage": False,
    "cleaningPageLevel2": False,
    "cleaningPageLevel3": False,
    "paperFeedCleaningPage": True,
    "ribSmearCleaningPage": True,
    "alignmentPage": True,
}

CALIBRATION = {
    "availableCalibrations": ["penAlignSemiauto"],
    "requiresMedia": "true",
}


def _coordinator(
    can_write: bool = True,
    reports: dict | None = None,
    calibration: dict | None = None,
    paper_present: bool | None = None,
) -> MagicMock:
    client = MagicMock()
    client.can_write = can_write
    client.base_url = "https://printer.local:443"
    client.async_get_reports = AsyncMock(
        return_value=REPORTS if reports is None else reports
    )
    client.async_get_calibration_capabilities = AsyncMock(
        return_value=CALIBRATION if calibration is None else calibration
    )
    client.async_run_report = AsyncMock(return_value={})
    client.async_run_calibration = AsyncMock(return_value={})

    coordinator = MagicMock()
    coordinator.client = client
    coordinator.product_info = MagicMock(serial_number="SN-1", make_and_model="ST 750")
    coordinator.config_entry = MagicMock(entry_id="e1", title="Printer")
    coordinator.data = MagicMock(paper_present=paper_present)
    # async_setup_entry reads the coordinator off the entry, so the stand-in
    # entry has to point back at this object.
    coordinator.runtime_data = coordinator
    return coordinator


def _description(key: str):
    return next(d for d in BUTTONS if d.key == key)


async def _collect(coordinator) -> list[str]:
    added: list[HPMaintenanceButton] = []
    await button_platform.async_setup_entry(
        MagicMock(), coordinator, lambda entities: added.extend(entities)
    )
    return [e.entity_description.key for e in added]


# ------------------------------------------------------- which buttons exist


async def test_a_printer_offering_everything_gets_every_button() -> None:
    """Six operations on the test machine: five cleans and one alignment."""
    keys = await _collect(_coordinator())
    assert set(keys) == {
        "clean_ink_light",
        "clean_ink_medium",
        "clean_ink_strong",
        "clean_paper_feed",
        "clean_rib_smear",
        "calibrate_printhead",
    }


async def test_a_printer_with_no_write_path_gets_no_buttons() -> None:
    """An LEDM client has no write path, so there is nothing to offer.

    Not a password condition: CDP does not authenticate writes, so the
    buttons must appear whether or not one is configured.
    """
    assert await _collect(_coordinator(can_write=False)) == []


async def test_buttons_appear_without_a_password_too() -> None:
    """No credential is required, so no credential may be demanded.

    Gating the buttons on the password would remove the feature from exactly
    the machines that most need it, for a credential the protocol ignores.
    """
    coordinator = _coordinator()
    keys = await _collect(coordinator)
    assert "calibrate_printhead" in keys
    assert len(keys) == 6


async def test_a_printer_reporting_no_reports_gets_no_buttons() -> None:
    """An absent reports document means the model offers nothing to press."""
    assert await _collect(_coordinator(reports={})) == []


async def test_only_the_cleans_this_model_actually_lists_are_offered() -> None:
    """A model with no level-3 clean does not get a level-3 button.

    Asking a printer for a purge it does not have returns an error the user
    would have to decode. Better that the button was never on the card.
    """
    partial = {k: v for k, v in REPORTS.items() if k != "cleaningPageLevel3"}
    keys = await _collect(_coordinator(reports=partial))
    assert "clean_ink_strong" not in keys
    assert "clean_ink_light" in keys


async def test_no_alignment_button_when_the_model_cannot_align() -> None:
    """The calibration button follows availableCalibrations, not a constant."""
    keys = await _collect(_coordinator(calibration={"availableCalibrations": []}))
    assert "calibrate_printhead" not in keys
    assert "clean_ink_light" in keys


# ----------------------------------------------------------- what pressing does


async def test_pressing_a_clean_sends_the_report_id_the_device_listed() -> None:
    """The request names the report, and nothing else is sent."""
    coordinator = _coordinator()
    entity = HPMaintenanceButton(coordinator, _description("clean_ink_strong"))

    await entity.async_press()

    coordinator.client.async_run_report.assert_awaited_once_with("cleaningPageLevel3")
    coordinator.client.async_run_calibration.assert_not_awaited()


async def test_pressing_alignment_sends_the_advertised_type() -> None:
    """And the calibration path does not go through the report endpoint."""
    coordinator = _coordinator()
    entity = HPMaintenanceButton(coordinator, _description("calibrate_printhead"))

    await entity.async_press()

    coordinator.client.async_run_calibration.assert_awaited_once_with(
        "penAlignSemiauto"
    )
    coordinator.client.async_run_report.assert_not_awaited()


# ------------------------------------------------------------ the refusals


async def test_alignment_is_refused_only_when_the_printer_says_the_tray_is_empty() -> (
    None
):
    """The check is on the tray, not on the operation needing media.

    ``requiresMedia`` describes the alignment itself, so every printer that
    has one would set it -- refusing on it would leave the button permanently
    unpressable on exactly the machines that need it. The one checkable fact
    is the tray: if the device says it is empty, there is nothing to print a
    pattern onto.
    """
    coordinator = _coordinator(paper_present=False)
    entity = HPMaintenanceButton(coordinator, _description("calibrate_printhead"))

    with pytest.raises(HomeAssistantError) as caught:
        await entity.async_press()

    assert caught.value.translation_key == "calibration_no_paper"
    coordinator.client.async_run_calibration.assert_not_awaited()


async def test_alignment_presses_when_the_tray_state_is_unknown() -> None:
    """A printer that reports no paper level still gets a working button.

    This is the measured case on the CDP test machine: it has no paper sensor
    at all, so ``paper_present`` is None. Reading unknown as empty would leave
    the alignment permanently unpressable on the one printer whose alignment
    is actually failing -- a feature that is there and can never be used.
    """
    coordinator = _coordinator(paper_present=None)
    entity = HPMaintenanceButton(coordinator, _description("calibrate_printhead"))

    await entity.async_press()

    coordinator.client.async_run_calibration.assert_awaited_once_with(
        "penAlignSemiauto"
    )


async def test_alignment_presses_when_paper_is_present() -> None:
    """And when the device positively reports paper in the tray."""
    coordinator = _coordinator(paper_present=True)
    entity = HPMaintenanceButton(coordinator, _description("calibrate_printhead"))

    await entity.async_press()

    coordinator.client.async_run_calibration.assert_awaited_once()


async def test_a_refused_write_reaches_the_user_rather_than_the_log_only() -> None:
    """A refused clean has to surface, or the user pressed nothing.

    The press does not raise, so swallowing the error would leave a button
    that looks like it worked. The device's own message is carried through,
    because "busy" and "wrong password" need different responses.
    """
    coordinator = _coordinator()
    coordinator.client.async_run_report = AsyncMock(
        side_effect=HPPrinterWriteError("maintenance cycle in progress")
    )
    entity = HPMaintenanceButton(coordinator, _description("clean_ink_light"))

    with pytest.raises(HomeAssistantError) as caught:
        await entity.async_press()

    assert caught.value.translation_key == "maintenance_failed"
    assert (
        "maintenance cycle in progress"
        in caught.value.translation_placeholders["error"]
    )


async def test_a_second_press_while_one_is_in_flight_is_refused() -> None:
    """Two purges at once is not something a printer will do.

    The window is between "the request was sent" and "the printer answered",
    which on a real device is seconds. Without the guard the second press goes
    out anyway and comes back as an error the printer phrases in its own
    words, after the user has already been told the press was accepted.
    """
    coordinator = _coordinator()
    release = asyncio.Event()

    async def _slow(_report_id: str) -> dict:
        await release.wait()
        return {}

    coordinator.client.async_run_report = AsyncMock(side_effect=_slow)
    entity = HPMaintenanceButton(coordinator, _description("clean_ink_light"))

    first = asyncio.create_task(entity.async_press())
    await asyncio.sleep(0)  # let the first press reach the awaiting request

    with pytest.raises(HomeAssistantError) as caught:
        await entity.async_press()
    assert caught.value.translation_key == "maintenance_already_running"
    assert coordinator.client.async_run_report.await_count == 1

    release.set()
    await first


async def test_the_busy_flag_is_released_even_when_the_write_fails() -> None:
    """A refused press must not wedge the button.

    The flag is set and cleared around the request, so a device error that
    raises has to leave it clear. Otherwise one busy printer needs a reload
    to become cleanable again, and the user is not told why.
    """
    coordinator = _coordinator()
    coordinator.client.async_run_report = AsyncMock(
        side_effect=HPPrinterWriteError("busy")
    )
    entity = HPMaintenanceButton(coordinator, _description("clean_ink_light"))

    with pytest.raises(HomeAssistantError):
        await entity.async_press()

    coordinator.client.async_run_report = AsyncMock(return_value={})
    await entity.async_press()
    assert coordinator.client.async_run_report.await_count == 1
