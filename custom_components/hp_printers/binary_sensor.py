"""Binary sensor platform for the HP Printers integration."""

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import HPPrinterConfigEntry
from .entity import HPConsumableEntity, HPPrinterEntity
from .models import Consumable, PrinterData, ProductInfo

PARALLEL_UPDATES = 0

# Cartridge states the device reports as healthy. Anything else -- low,
# veryLow, outOfSupply, unauthorised variants -- is treated as a problem.
HEALTHY_CONSUMABLE_STATES = {"ok", "newgenuinehp", "new", "good"}

# The level at which the paper sensor is treated as low, as a percentage of
# capacity. HP does not publish a paper threshold the way it publishes a
# cartridge one, and the captured models fill a 100-sheet tray rather than
# warning first, so this is an integration policy rather than a device
# reading. It is deliberately generous: the point is to catch a printer
# about to stop mid-job, not to nag.
PAPER_LOW_PERCENT = 20


def _paper_low(data: PrinterData) -> bool | None:
    """Return True when the main tray is low, False when it is fine.

    None when the printer reports no level, so the entity is not created
    rather than sitting at a reassuring "off".
    """
    tray = data.main_paper_tray
    if tray is None:
        return None
    percent = tray.level_percent
    if percent is None:
        return None
    return percent < PAPER_LOW_PERCENT


@dataclass(frozen=True, kw_only=True)
class HPPrinterBinarySensorDescription(BinarySensorEntityDescription):
    """Describes a printer-level binary sensor."""

    value_fn: Callable[[PrinterData, ProductInfo], bool | None]


@dataclass(frozen=True, kw_only=True)
class HPConsumableBinarySensorDescription(BinarySensorEntityDescription):
    """Describes a cartridge-level binary sensor."""

    value_fn: Callable[[Consumable], bool | None]


PRINTER_BINARY_SENSORS: tuple[HPPrinterBinarySensorDescription, ...] = (
    # --- attack surface ---
    # Each of these is something switched on in the printer's own settings
    # that lets something else on the network reach it. They are not faults
    # -- most are on by default and the machine works perfectly -- but they
    # are the answers to "is this thing exposed", and nothing else in the
    # integration would let anyone see them.
    HPPrinterBinarySensorDescription(
        key="snmp_public",
        translation_key="snmp_public",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=BinarySensorDeviceClass.SAFETY,
        # True means the device accepts the built-in "public" community
        # string. Both printers measured ship with it enabled, which means
        # any host on the segment can read the management data with a
        # credential nobody has to guess.
        value_fn=lambda data, _info: data.snmp_public_allowed,
    ),
    HPPrinterBinarySensorDescription(
        key="bluetooth_beaconing",
        translation_key="bluetooth_beaconing",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data, _info: data.bluetooth_beaconing,
    ),
    HPPrinterBinarySensorDescription(
        key="auto_update_enabled",
        translation_key="auto_update_enabled",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data, _info: data.auto_update_enabled,
    ),
    # --- setup ---
    HPPrinterBinarySensorDescription(
        key="setup_incomplete",
        translation_key="setup_incomplete",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=BinarySensorDeviceClass.PROBLEM,
        # On means a first-time setup step is still outstanding. On the model
        # measured this is the alignment step, which is the same underlying
        # fact as a failed calibration and explains it.
        value_fn=lambda data, _info: data.setup_incomplete,
    ),
    HPPrinterBinarySensorDescription(
        key="firmware_update_failed",
        translation_key="firmware_update_failed",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda data, _info: (
            data.firmware_update_result.lower() == "failed"
            if data.firmware_update_result
            else None
        ),
    ),
    HPPrinterBinarySensorDescription(
        key="holo_enabled",
        translation_key="holo_enabled",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data, _info: data.holo_enabled,
    ),
    # --- LEDM exposure -------------------------------------------------
    # The same attack surface the CDP side reports as print services, read
    # from the LEDM spelling. Both printers measured answer raw printing on
    # port 9100, and neither redirects HTTP to HTTPS.
    HPPrinterBinarySensorDescription(
        key="port_9100_enabled",
        translation_key="port_9100_enabled",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=BinarySensorDeviceClass.SAFETY,
        # Raw printing: no driver, no job structure, no authentication.
        value_fn=lambda data, _info: data.port_9100_enabled,
    ),
    HPPrinterBinarySensorDescription(
        key="https_redirection",
        translation_key="https_redirection",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=BinarySensorDeviceClass.SAFETY,
        # Off means the printer's own web interface answers plain HTTP, and
        # the admin password crosses the network in the clear every time
        # someone opens it.
        value_fn=lambda data, _info: data.https_redirection_enabled,
    ),
    HPPrinterBinarySensorDescription(
        key="http_proxy_enabled",
        translation_key="http_proxy_enabled",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        device_class=BinarySensorDeviceClass.SAFETY,
        # A proxy is a decision the printer was configured with, and a printer
        # that reaches the network through one is worth seeing. It belongs here
        # rather than in the sensor table because it is a boolean, and
        # Home Assistant rejects a boolean sensor outright rather than
        # rendering it as something odd.
        value_fn=lambda data, info: info.http_proxy_enabled,
    ),
    HPPrinterBinarySensorDescription(
        key="duplexer_installed",
        translation_key="duplexer_installed",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        # Hardware, and it is not the same question as auto-duplex below: the
        # machine measured has an installed duplexer with ten thousand
        # double-sided sheets printed while its auto-duplex setting is off.
        value_fn=lambda data, info: info.duplexer_installed,
    ),
    HPPrinterBinarySensorDescription(
        key="auto_duplex_enabled",
        translation_key="auto_duplex_enabled",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data, info: info.auto_duplex_enabled,
    ),
    HPPrinterBinarySensorDescription(
        key="paper_low",
        translation_key="paper_low",
        device_class=BinarySensorDeviceClass.PROBLEM,
        # Off means "not low", and only when the printer actually reports a
        # level. A model with no paper sensor reports nothing, and the
        # entity is not created at all -- an always-off "paper is fine" would
        # be a worse answer than no entity, because it reads as a reading.
        value_fn=lambda data, _info: _paper_low(data),
    ),
    HPPrinterBinarySensorDescription(
        key="accepting_jobs",
        translation_key="accepting_jobs",
        # "Will it take a job right now" is a readiness question, not a
        # problem, so it is RUNNING rather than PROBLEM. It is also the only
        # such signal on a CDP model -- its status word is a state name, not
        # a decision.
        device_class=BinarySensorDeviceClass.RUNNING,
        value_fn=lambda data, _info: data.accepting_jobs,
    ),
    HPPrinterBinarySensorDescription(
        key="quiet_mode",
        translation_key="quiet_mode",
        entity_category=EntityCategory.DIAGNOSTIC,
        # Reported by both protocols under different names and in different
        # places: LEDM puts it in the static product configuration, CDP in
        # the per-poll print configuration. Either copy answers the question,
        # so the second is a fallback rather than a second entity.
        value_fn=lambda data, info: (
            data.quiet_mode if data.quiet_mode is not None else info.quiet_mode
        ),
    ),
    HPPrinterBinarySensorDescription(
        key="auto_jam_recovery",
        translation_key="auto_jam_recovery",
        entity_category=EntityCategory.DIAGNOSTIC,
        # Off means the device will not try to clear a jam by itself, so a
        # jam needs someone at the machine. A setting, not a fault.
        value_fn=lambda data, _info: data.auto_jam_recovery,
    ),
    HPPrinterBinarySensorDescription(
        key="paper_present",
        translation_key="paper_present",
        device_class=BinarySensorDeviceClass.RUNNING,
        # Presence only. LEDM reports no level, so this is the paper signal
        # available without IPP, and it says nothing about how much is left.
        value_fn=lambda data, _info: data.paper_present,
    ),
    HPPrinterBinarySensorDescription(
        key="low_ink_messaging",
        translation_key="low_ink_messaging",
        entity_category=EntityCategory.DIAGNOSTIC,
        # A model whose ink tanks have no level sensor keeps this enabled and
        # can never act on it, so the interesting answer is usually "on, and
        # still silent" -- which is why it is a sensor rather than a problem.
        value_fn=lambda data, _info: data.low_ink_messaging,
    ),
    HPPrinterBinarySensorDescription(
        key="firmware_fault",
        translation_key="firmware_fault",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        # The device keeps assert text from the last firmware crash until it
        # is cleared, so this reflects a recorded fault rather than a live
        # one. To catch new faults, trigger on the last event code changing.
        value_fn=lambda data, _info: bool(data.assert_text),
    ),
    HPPrinterBinarySensorDescription(
        key="genuine_supplies_only",
        translation_key="genuine_supplies_only",
        entity_category=EntityCategory.DIAGNOSTIC,
        # When enabled, the printer refuses non-HP cartridges. Worth watching:
        # a firmware update can turn it back on and stop a working printer.
        value_fn=lambda data, _info: data.genuine_supplies_only,
    ),
    HPPrinterBinarySensorDescription(
        key="admin_password_set",
        translation_key="admin_password_set",
        entity_category=EntityCategory.DIAGNOSTIC,
        # The embedded web server password gates writes only; LEDM reads stay
        # open regardless, which is why this integration needs no credentials.
        value_fn=lambda _data, info: info.password_set,
    ),
)


CONSUMABLE_BINARY_SENSORS: tuple[HPConsumableBinarySensorDescription, ...] = (
    HPConsumableBinarySensorDescription(
        key="problem",
        translation_key="cartridge_problem",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda c: (
            None
            if c.state is None
            else c.state.strip().lower() not in HEALTHY_CONSUMABLE_STATES
        ),
    ),
    HPConsumableBinarySensorDescription(
        key="genuine",
        translation_key="cartridge_genuine",
        entity_category=EntityCategory.DIAGNOSTIC,
        # HP labels third-party cartridges "clone" even when enforcement is
        # switched off, so this is reported regardless of whether it matters.
        value_fn=lambda c: c.is_genuine,
    ),
    HPConsumableBinarySensorDescription(
        key="refilled",
        translation_key="cartridge_refilled",
        entity_category=EntityCategory.DIAGNOSTIC,
        # Distinct from `genuine`: a part can be a genuine HP cartridge that
        # the supplier refilled. Read alone, "refilled: true" says nothing
        # about whether it is fake, and conflating the two is how a false
        # counterfeit alarm gets raised.
        value_fn=lambda c: c.is_refilled,
    ),
    HPConsumableBinarySensorDescription(
        key="previously_used",
        translation_key="cartridge_previously_used",
        entity_category=EntityCategory.DIAGNOSTIC,
        # A part the device has seen in service before. HP asks the owner to
        # acknowledge it; it says nothing about the part's origin.
        value_fn=lambda c: c.is_used,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HPPrinterConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up binary sensors for a printer."""
    coordinator = entry.runtime_data
    data = coordinator.data
    info = coordinator.product_info

    entities: list[BinarySensorEntity] = [
        HPPrinterBinarySensor(coordinator, description)
        for description in PRINTER_BINARY_SENSORS
        if description.value_fn(data, info) is not None
    ]

    entities.extend(
        HPConsumableBinarySensor(coordinator, description, code)
        for code, consumable in data.consumables.items()
        for description in CONSUMABLE_BINARY_SENSORS
        if description.value_fn(consumable) is not None
    )

    async_add_entities(entities)


class HPPrinterBinarySensor(HPPrinterEntity, BinarySensorEntity):
    """A printer-level binary sensor."""

    entity_description: HPPrinterBinarySensorDescription

    @property
    def is_on(self) -> bool | None:
        """Return the sensor state."""
        return self.entity_description.value_fn(
            self.coordinator.data, self.coordinator.product_info
        )


class HPConsumableBinarySensor(HPConsumableEntity, BinarySensorEntity):
    """A cartridge-level binary sensor."""

    entity_description: HPConsumableBinarySensorDescription

    @property
    def is_on(self) -> bool | None:
        """Return the sensor state."""
        if (consumable := self.consumable) is None:
            return None
        return self.entity_description.value_fn(consumable)
