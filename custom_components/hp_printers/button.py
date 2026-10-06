"""Buttons for the maintenance operations the printer itself offers.

Why this platform exists
------------------------
The printer enumerates its own maintenance operations -- three strengths of
ink-path clean, a paper-feed clean, a rib-smear clean, and a printhead
alignment -- and this turns them into buttons. The user asked for buttons
rather than automation, and that is the right shape for this: every one of
these operations spends ink and paper, and a 60-second poll firing one on a
schedule is not something anyone wants.

What is deliberately not here
-----------------------------
No automatic trigger, no script-enabled-by-default, and no "fix my printer"
action. The entity only does something when a person presses it.

Which buttons appear is decided by the device
---------------------------------------------
A button is created only for an operation the printer lists in its own
reports document. A model without a printhead clean has no ``cleaningPage``
to offer, and offering the button anyway produces an error the user would
have to decode. The admin password is not part of this condition: CDP does
not authenticate writes, so gating on it would hide a working feature.
"""

from dataclasses import dataclass
import logging

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import HPPrinterError
from .const import DOMAIN
from .coordinator import HPPrinterConfigEntry, HPPrinterDataUpdateCoordinator
from .entity import HPPrinterEntity

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class HPMaintenanceButtonDescription(ButtonEntityDescription):
    """A button that starts one maintenance operation.

    ``report_id`` is the device's own identifier, taken from the ``reports``
    document rather than invented here: the request body has to name the
    report the device knows, and the only trustworthy source for that name is
    the device.
    """

    report_id: str | None = None
    calibration_type: str | None = None


# The cleaning cycles, weakest first, with the consequence named in the
# translation rather than left for the user to discover by trying one.
#
# Three ink-path strengths are not three labels for one thing: each one runs
# a longer purge, and the escalation is the user's to make. Paper-feed and
# rib-smear are separate systems -- a paper-feed clean does nothing for a
# smear on the printhead -- so a single "cleaning" button would hide a
# choice the user is paying for.
BUTTONS: tuple[HPMaintenanceButtonDescription, ...] = (
    HPMaintenanceButtonDescription(
        key="clean_ink_light",
        translation_key="clean_ink_light",
        report_id="cleaningPage",
    ),
    HPMaintenanceButtonDescription(
        key="clean_ink_medium",
        translation_key="clean_ink_medium",
        report_id="cleaningPageLevel2",
    ),
    HPMaintenanceButtonDescription(
        key="clean_ink_strong",
        translation_key="clean_ink_strong",
        report_id="cleaningPageLevel3",
    ),
    HPMaintenanceButtonDescription(
        key="clean_paper_feed",
        translation_key="clean_paper_feed",
        report_id="paperFeedCleaningPage",
    ),
    HPMaintenanceButtonDescription(
        key="clean_rib_smear",
        translation_key="clean_rib_smear",
        report_id="ribSmearCleaningPage",
    ),
    HPMaintenanceButtonDescription(
        key="calibrate_printhead",
        translation_key="calibrate_printhead",
        calibration_type="penAlignSemiauto",
    ),
)


class HPMaintenanceButton(HPPrinterEntity, ButtonEntity):
    """One pressable maintenance operation."""

    entity_description: HPMaintenanceButtonDescription

    _attr_should_poll = False
    # A button is not a reading: nothing about it goes stale, and leaving it
    # in the "available" state forever is what tells Home Assistant to
    # draw it as a normal control.
    _attr_available = True

    def __init__(
        self,
        coordinator: HPPrinterDataUpdateCoordinator,
        description: HPMaintenanceButtonDescription,
    ) -> None:
        """Initialize the button."""
        super().__init__(coordinator, description)
        self._busy = False

    async def async_press(self) -> None:
        """Start the operation.

        The printer answers immediately with an acknowledgement and runs the
        cycle asynchronously -- a clean can take minutes. So this confirms
        that the request was *accepted*, and the user is told that, rather
        than being told the clean finished. Reporting completion here would
        be a claim the device has not made.
        """
        client = self.coordinator.client
        description = self.entity_description

        if self._busy:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="maintenance_already_running",
            )

        self._busy = True
        try:
            if description.calibration_type is not None:
                capabilities = await client.async_get_calibration_capabilities()
                available = capabilities.get("availableCalibrations") or []
                if description.calibration_type not in available:
                    raise HomeAssistantError(
                        translation_domain=DOMAIN,
                        translation_key="calibration_unavailable",
                    )
                # The alignment prints a test pattern, so it needs paper. That
                # is not a reason to refuse, and refusing on it would be a bug:
                # the flag is a property of the operation, so a printer that
                # always sets it would have a button that can never be
                # pressed. Refuse only on the one thing that is checkable --
                # the device saying the tray is empty -- and put the
                # requirement in the button's own name so it is visible
                # before the press, not after it.
                if self.coordinator.data.paper_present is False:
                    raise HomeAssistantError(
                        translation_domain=DOMAIN,
                        translation_key="calibration_no_paper",
                    )
                await client.async_run_calibration(description.calibration_type)
                return

            if description.report_id is not None:
                await client.async_run_report(description.report_id)
                return

            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="maintenance_unavailable",
            )
        except HPPrinterError as err:
            _LOGGER.error("Maintenance operation %s failed: %s", description.key, err)
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="maintenance_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        finally:
            self._busy = False


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HPPrinterConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the maintenance buttons the device and the entry support."""
    coordinator = entry.runtime_data
    client = coordinator.client

    # No write path on this client: an LEDM model, whose maintenance surface
    # is not a documented LEDM resource. Either way there is nothing honest
    # to put on the card.
    if not getattr(client, "can_write", False):
        _LOGGER.debug(
            "No write path for %s; maintenance buttons not created", entry.title
        )
        return

    reports = await client.async_get_reports()
    if not reports:
        _LOGGER.debug(
            "%s lists no reports; maintenance buttons not created", entry.title
        )
        return

    capabilities = await client.async_get_calibration_capabilities()
    available_calibrations = capabilities.get("availableCalibrations") or []

    created: list[HPMaintenanceButton] = []
    for description in BUTTONS:
        if description.report_id is not None:
            if description.report_id not in reports:
                continue
        elif description.calibration_type is not None:
            if description.calibration_type not in available_calibrations:
                continue
        created.append(HPMaintenanceButton(coordinator, description))

    _LOGGER.debug("Created %d maintenance button(s) for %s", len(created), entry.title)
    async_add_entities(created)


__all__ = [
    "BUTTONS",
    "HPMaintenanceButton",
    "HPMaintenanceButtonDescription",
    "async_setup_entry",
]
