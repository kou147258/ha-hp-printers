"""Sensor platform for the HP Printers integration."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType

from .const import STATUS_OPTIONS
from .coordinator import HPPrinterConfigEntry
from .entity import HPConsumableEntity, HPPrinterEntity, HPSubunitEntity
from .models import Consumable, NetworkHealth, PrinterData, ProductInfo

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class HPPrinterSensorDescription(SensorEntityDescription):
    """Describes a printer-level sensor."""

    value_fn: Callable[[PrinterData, ProductInfo], StateType | date]
    attrs_fn: Callable[[PrinterData], dict[str, Any]] | None = None
    # When set, the entity is attached to a sub-device rather than the printer.
    subunit: str | None = None


@dataclass(frozen=True, kw_only=True)
class HPConsumableSensorDescription(SensorEntityDescription):
    """Describes a cartridge-level sensor."""

    value_fn: Callable[[Consumable], StateType | date]
    attrs_fn: Callable[[Consumable], dict[str, Any]] | None = None


def _counter(
    key: str,
    translation_key: str,
    getter: Callable[[PrinterData], Any],
    subunit: str | None = None,
):
    """Build a monotonic page-counter description."""
    return HPPrinterSensorDescription(
        key=key,
        translation_key=translation_key,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda data, _info: getter(data),
        subunit=subunit,
    )


def _network_counter(
    key: str,
    translation_key: str,
    getter: Callable[[NetworkHealth], int | None],
):
    """Build a network counter, off by default.

    These are packet counts rather than page counts, and diagnostic because
    they mean nothing to someone who is not chasing a connectivity problem.
    The unit itself comes from the translations, like every other unit here.
    """
    return HPPrinterSensorDescription(
        key=key,
        translation_key=translation_key,
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data, _info: getter(data.network),
    )


def _iso_date(value: str | None) -> date | None:
    """Return the date part of an ISO timestamp, or None.

    The device reports these as full ISO timestamps with a ``Z`` suffix; only
    the date is exposed, matching the other manufacture-date sensors here.
    A value that is not a date at all is dropped rather than passed through
    as a string a DATE-class sensor cannot render.
    """
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _paper_value(data: PrinterData) -> float | int | None:
    """Return the main tray's level in the unit the device reported.

    A tray that counts sheets keeps sheets; one that measures a percentage
    keeps percent. Converting both to a single number would mean either
    comparing a sheet count against a percentage threshold or guessing a
    tray capacity, and either way the sensor would be lying about what it
    is reading.
    """
    tray = data.main_paper_tray
    if tray is None:
        return None
    if tray.is_percent:
        return tray.level_percent
    return tray.level


def _worst_alert(data: PrinterData) -> str | None:
    """Return the severity of the most urgent alert the device is raising.

    The device already orders its alerts by its own priority, and a document
    with no alerts is not the same as one whose worst alert is
    ``information``. So the first entry is taken as-is rather than ranked
    again here: a second ranking scheme would disagree with the device's at
    some boundary, and the device is the one that has to act on it.
    """
    if not data.active_alerts:
        return None
    return data.active_alerts[0].severity


PRINTER_SENSORS: tuple[HPPrinterSensorDescription, ...] = (
    HPPrinterSensorDescription(
        key="status",
        translation_key="status",
        device_class=SensorDeviceClass.ENUM,
        options=STATUS_OPTIONS,
        value_fn=lambda data, _info: (
            data.status if data.status in STATUS_OPTIONS else None
        ),
        attrs_fn=lambda data: {
            "raw_status": data.status,
            "message": data.status_message,
        },
    ),
    # --- paper ---
    # Read over IPP; LEDM's media handling has no level and CDP has none at
    # all, so this is the only paper figure either model can produce.
    HPPrinterSensorDescription(
        key="paper_level",
        translation_key="paper_level",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data, _info: _paper_value(data),
        attrs_fn=lambda data: (
            {
                "tray": tray.name,
                "tray_type": tray.type,
                "unit": tray.unit,
                "max_capacity": tray.max_capacity,
            }
            if (tray := data.main_paper_tray) is not None
            else None
        ),
    ),
    # --- printer counters ---
    _counter(
        "printer_total_pages",
        "printer_total_pages",
        lambda d: d.printer.total_impressions,
    ),
    _counter(
        "printer_mono_pages",
        "printer_mono_pages",
        lambda d: d.printer.monochrome_impressions,
    ),
    _counter(
        "printer_color_pages",
        "printer_color_pages",
        lambda d: d.printer.color_impressions,
    ),
    _counter(
        "printer_simplex_sheets",
        "printer_simplex_sheets",
        lambda d: d.printer.simplex_sheets,
    ),
    _counter(
        "printer_duplex_sheets",
        "printer_duplex_sheets",
        lambda d: d.printer.duplex_sheets,
    ),
    _counter("printer_jams", "printer_jams", lambda d: d.printer.jam_events),
    _counter(
        "printer_mispicks", "printer_mispicks", lambda d: d.printer.mispick_events
    ),
    # --- scanner counters ---
    _counter(
        "scanner_images", "scanner_images", lambda d: d.scanner.scan_images, "scanner"
    ),
    _counter(
        "scanner_adf_images",
        "scanner_adf_images",
        lambda d: d.scanner.adf_images,
        "scanner",
    ),
    _counter(
        "scanner_flatbed_images",
        "scanner_flatbed_images",
        lambda d: d.scanner.flatbed_images,
        "scanner",
    ),
    _counter(
        "scanner_duplex_sheets",
        "scanner_duplex_sheets",
        lambda d: d.scanner.duplex_sheets,
        "scanner",
    ),
    # --- scan-job counters ---
    # These belong to the scanner sub-device but read the scan application,
    # not the engine: the engine total includes copies, these do not.
    _counter(
        "scan_job_pages",
        "scan_job_pages",
        lambda d: d.scan.scan_images,
        "scanner",
    ),
    _counter(
        "scan_job_adf_pages",
        "scan_job_adf_pages",
        lambda d: d.scan.adf_images,
        "scanner",
    ),
    _counter(
        "scan_job_flatbed_pages",
        "scan_job_flatbed_pages",
        lambda d: d.scan.flatbed_images,
        "scanner",
    ),
    _counter(
        "scan_job_duplex_sheets",
        "scan_job_duplex_sheets",
        lambda d: d.scan.duplex_sheets,
        "scanner",
    ),
    _counter("scanner_jams", "scanner_jams", lambda d: d.scanner.jam_events, "scanner"),
    _counter(
        "scanner_mispicks",
        "scanner_mispicks",
        lambda d: d.scanner.mispick_events,
        "scanner",
    ),
    # --- copy counters ---
    _counter(
        "copy_total_pages",
        "copy_total_pages",
        lambda d: d.copy.total_impressions,
        "copy",
    ),
    _counter(
        "copy_mono_pages",
        "copy_mono_pages",
        lambda d: d.copy.monochrome_impressions,
        "copy",
    ),
    _counter(
        "copy_color_pages",
        "copy_color_pages",
        lambda d: d.copy.color_impressions,
        "copy",
    ),
    _counter(
        "copy_adf_pages",
        "copy_adf_pages",
        lambda d: d.copy.adf_images,
        "copy",
    ),
    _counter(
        "copy_flatbed_pages",
        "copy_flatbed_pages",
        lambda d: d.copy.flatbed_images,
        "copy",
    ),
    # --- paper, ink drawn, and device lifecycle ---
    # The ink figure is the only counter that shows where the ink came from:
    # the captured Smart Tank reports 1208 ml drawn against 0 ml ever shipped
    # in a cartridge, which is the arithmetic behind refilled ink.
    HPPrinterSensorDescription(
        key="marking_agent_used",
        translation_key="marking_agent_used",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda data, _info: data.marking_agent_used_ml,
        attrs_fn=lambda data: {
            "shipped_with_cartridge_ml": data.marking_agent_inserted_ml,
        },
    ),
    _counter(
        "panel_cancel_presses",
        "panel_cancel_presses",
        lambda d: d.panel_cancel_presses,
    ),
    _counter("power_cycles", "power_cycles", lambda d: d.power_cycles),
    HPPrinterSensorDescription(
        key="installed_at",
        translation_key="printer_installed_at",
        device_class=SensorDeviceClass.DATE,
        entity_category=EntityCategory.DIAGNOSTIC,
        # Only the CDP identity document carries this; a LEDM model has no
        # equivalent, so the entity is simply not created there.
        value_fn=lambda _data, info: (
            info.installed_at.date() if info.installed_at else None
        ),
    ),
    HPPrinterSensorDescription(
        key="scanner_status",
        translation_key="scanner_status",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data, _info: data.scanner_status,
        attrs_fn=lambda data: {"scanner_error": data.scanner_error},
    ),
    # --- the settings and counters a device reports but used to be ignored ---
    HPPrinterSensorDescription(
        key="auto_off_time",
        translation_key="auto_off_time",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        # Free text the firmware chooses ("never", "2minutes"). Not converted
        # to minutes: the accepted spellings are not an enumeration, and a
        # converted value would claim precision the device never offered.
        value_fn=lambda _data, info: info.auto_off_time,
    ),
    _counter(
        "panel_button_presses",
        "panel_button_presses",
        lambda d: d.panel_button_presses,
    ),
    _counter(
        "scan_to_host_images", "scan_to_host_images", lambda d: d.scan_to_host_images
    ),
    _counter("non_hp_flag_count", "non_hp_flag_count", lambda d: d.non_hp_flag_count),
    # The three below are sums across the per-media-type blocks the device
    # repeats, not the single value it reports for any one of them. The
    # translations say so, because "normal quality pages" otherwise reads as
    # a total the device never stated.
    _counter(
        "normal_quality_pages", "normal_quality_pages", lambda d: d.normal_quality_pages
    ),
    _counter(
        "better_quality_pages", "better_quality_pages", lambda d: d.better_quality_pages
    ),
    _counter(
        "draft_quality_pages", "draft_quality_pages", lambda d: d.draft_quality_pages
    ),
    _counter(
        "photo_quality_pages", "photo_quality_pages", lambda d: d.photo_quality_pages
    ),
    # How the last printhead alignment went. A failure here is a real fault
    # that a page counter never shows: the printer is online and prints, and
    # the output is skewed or banded.
    HPPrinterSensorDescription(
        key="calibration_result",
        translation_key="calibration_result",
        device_class=SensorDeviceClass.ENUM,
        options=["passed", "failed", "cancelled", "unknown"],
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data, _info: (
            data.calibration_last_result
            if data.calibration_last_result in ("passed", "failed", "cancelled")
            else "unknown"
        ),
        attrs_fn=lambda data: {
            "status": data.calibration_status,
            "failure_reason": data.calibration_failure_reason,
        },
    ),
    # --- diagnostics: firmware and the device event log ---
    HPPrinterSensorDescription(
        key="firmware_date",
        translation_key="firmware_date",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda _data, info: info.firmware_date,
    ),
    HPPrinterSensorDescription(
        key="manufactured_at",
        translation_key="printer_manufactured_at",
        device_class=SensorDeviceClass.DATE,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda _data, info: (
            info.manufactured_at.date() if info.manufactured_at else None
        ),
    ),
    _counter(
        "genuine_color_pages",
        "genuine_color_pages",
        lambda d: d.genuine_color_impressions,
    ),
    _counter(
        "genuine_mono_pages",
        "genuine_mono_pages",
        lambda d: d.genuine_mono_impressions,
    ),
    HPPrinterSensorDescription(
        key="power_save_timeout",
        translation_key="power_save_timeout",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda _data, info: info.power_save_timeout,
    ),
    HPPrinterSensorDescription(
        key="language_pack_version",
        translation_key="language_pack_version",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda _data, info: info.language_pack_version,
    ),
    HPPrinterSensorDescription(
        key="last_event_code",
        translation_key="last_event_code",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data, _info: data.last_event.code if data.last_event else None,
        # The whole log is attached so a fault history is one click away.
        # Codes are dotted families: 10.x supply memory, 13.x paper jams,
        # 41.x media mismatch, 49.x firmware faults.
        attrs_fn=lambda data: {
            "events": [
                {
                    "sequence": event.sequence,
                    "code": event.code,
                    # LEDM's event log carries no severity; CDP's does. The
                    # key is left out rather than guessed from the code.
                    **({"severity": event.severity} if event.severity else {}),
                    "at_page": event.impressions,
                }
                for event in data.events
            ],
            "assert_text": data.assert_text,
        },
    ),
    HPPrinterSensorDescription(
        key="last_event_page",
        translation_key="last_event_page",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data, _info: (
            data.last_event.impressions if data.last_event else None
        ),
    ),
    # --- setup progress ---
    # The single most actionable reading in this group, and the one that
    # explains a calibration failure: the device carries a first-time setup
    # checklist and marks each step, and on the model measured the alignment
    # step is the one still pending. "failed" and "never done" look identical
    # from the result field alone and call for opposite responses.
    HPPrinterSensorDescription(
        key="setup_state",
        translation_key="setup_state",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorDeviceClass.ENUM,
        options=["idle", "actionPending", "inProgress", "complete"],
        # The two protocols put this in different documents -- CDP in
        # deviceSetup, read every poll; LEDM in the product configuration, read
        # on the slow cadence. One entity reading both is what stops the LEDM
        # side reporting "not supported" for something it does publish.
        value_fn=lambda data, info: data.setup_operation_state or info.setup_phase,
        attrs_fn=lambda data: {
            "pending_steps": list(data.setup_pending_steps),
            "setup_complete": not data.setup_pending_steps,
        },
    ),
    # --- alerts currently raised ---
    # The event log is a record of what happened; this is what the machine is
    # saying right now. A clean log with a live alert is a printer that is
    # fine and is complaining, which no combination of the existing counters
    # would show.
    HPPrinterSensorDescription(
        key="active_alert_count",
        translation_key="active_alert_count",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data, _info: len(data.active_alerts) or None,
        attrs_fn=lambda data: {
            "alerts": [
                {
                    "category": alert.category,
                    "severity": alert.severity,
                    "priority": alert.priority,
                }
                for alert in data.active_alerts
            ],
            "categories": sorted(
                {a.category for a in data.active_alerts if a.category}
            ),
        },
    ),
    HPPrinterSensorDescription(
        key="active_alert_worst",
        translation_key="active_alert_worst",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorDeviceClass.ENUM,
        # The device orders its own alerts by priority, so the first is the
        # one it considers most urgent. The severity words are its own
        # vocabulary, taken from alert/v1/capabilities.
        options=["information", "warning", "error", "critical"],
        value_fn=lambda data, _info: _worst_alert(data),
    ),
    # --- firmware ---
    # The firmware build date was the only version marker before this, and it
    # says nothing about whether an update succeeded. On the model measured
    # auto-update is on, no update is available, and every attempt in the
    # history failed -- a combination the date cannot show.
    HPPrinterSensorDescription(
        key="firmware_update_result",
        translation_key="firmware_update_result",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorDeviceClass.ENUM,
        options=["succeeded", "failed", "cancelled", "inProgress", "unknown"],
        value_fn=lambda data, _info: data.firmware_update_result,
    ),
    HPPrinterSensorDescription(
        key="firmware_update_available",
        translation_key="firmware_update_available",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data, _info: data.firmware_update_available or None,
    ),
    # --- certificate ---
    # Issued for ten years with nothing to warn when it runs out. On that day
    # HTTPS access to the printer's own web interface stops working and the
    # reason is not obvious from the symptom.
    HPPrinterSensorDescription(
        key="certificate_expires",
        translation_key="certificate_expires",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda data, _info: data.certificate_expires,
        attrs_fn=lambda data: {"valid_from": data.certificate_valid_from},
    ),
    # --- network, per interface ---
    # The LEDM side reports one aggregate set of counters. Split by interface
    # is what tells a printer working over Wi-Fi from one whose cable is
    # unplugged: both report a small number, and only the split shows which
    # port is live.
    HPPrinterSensorDescription(
        key="adapter_errors",
        translation_key="adapter_errors",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data, _info: (
            max((a.error_total for a in data.adapter_stats), default=0)
            if data.adapter_stats
            else None
        ),
        attrs_fn=lambda data: {
            adapter.name: {
                "received_bytes": adapter.received_bytes,
                "transmitted_packets": adapter.transmitted_packets,
                "received_unicast": adapter.received_unicast,
                "received_multicast": adapter.received_multicast,
                "errors": adapter.error_total,
            }
            for adapter in data.adapter_stats
        },
    ),
    HPPrinterSensorDescription(
        key="internet_diagnostics",
        translation_key="internet_diagnostics",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorDeviceClass.ENUM,
        options=["connected", "disconnected", "unknown", "notTested"],
        value_fn=lambda data, _info: data.internet_diagnostics_result,
    ),
    # --- printer mechanics and consumables ---
    HPPrinterSensorDescription(
        key="carriage_status",
        translation_key="carriage_status",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorDeviceClass.ENUM,
        options=["ok", "notOk", "unknown"],
        value_fn=lambda data, _info: data.carriage_status,
    ),
    HPPrinterSensorDescription(
        key="cartridge_changes",
        translation_key="cartridge_changes",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda data, _info: data.cartridge_changes,
    ),
    HPPrinterSensorDescription(
        key="region_reset_remaining",
        translation_key="region_reset_remaining",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data, _info: data.region_reset_remaining,
    ),
    HPPrinterSensorDescription(
        key="service_id",
        translation_key="service_id",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data, _info: data.service_id,
    ),
    HPPrinterSensorDescription(
        key="print_services",
        translation_key="print_services",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data, _info: ", ".join(data.print_services) or None,
    ),
    HPPrinterSensorDescription(
        key="full_model_string",
        translation_key="full_model_string",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data, _info: data.full_model_string,
    ),
    # --- LEDM print configuration ---
    # Settings, not measurements: this says how the machine is configured,
    # not what came out of it. Worth having because "the output got worse"
    # is often a resolution someone changed, and nothing else shows it.
    HPPrinterSensorDescription(
        key="print_quality",
        translation_key="print_quality",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data, _info: data.print_quality,
    ),
    HPPrinterSensorDescription(
        key="resolution_setting",
        translation_key="resolution_setting",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data, _info: data.resolution_setting,
    ),
    HPPrinterSensorDescription(
        key="default_copies",
        translation_key="default_copies",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data, _info: data.default_copies,
    ),
    HPPrinterSensorDescription(
        key="current_media",
        translation_key="current_media",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data, _info: (
            f"{data.current_media_type} / {data.current_media_size}"
            if data.current_media_type or data.current_media_size
            else None
        ),
    ),
    HPPrinterSensorDescription(
        key="panel_language",
        translation_key="panel_language",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data, _info: data.panel_language,
    ),
    HPPrinterSensorDescription(
        key="instant_ink_status",
        translation_key="instant_ink_status",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data, _info: data.instant_ink_status,
    ),
    # --- LEDM: jobs ------------------------------------------------------
    # A whole category the integration had no part of. The outcome counters
    # are per subunit and are read from the printer's own, because the usage
    # document repeats the same names under every subunit and the scanner's
    # JobCount is a different thing from the printer's.
    HPPrinterSensorDescription(
        key="print_jobs",
        translation_key="print_jobs",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda data, _info: data.print_job_count,
    ),
    HPPrinterSensorDescription(
        key="job_failures",
        translation_key="job_failures",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda data, _info: data.job_failures,
    ),
    HPPrinterSensorDescription(
        key="job_successes",
        translation_key="job_successes",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda data, _info: data.job_successes,
    ),
    HPPrinterSensorDescription(
        key="job_cancelled",
        translation_key="job_cancelled",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda data, _info: data.job_cancelled,
    ),
    HPPrinterSensorDescription(
        key="job_skipped",
        translation_key="job_skipped",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda data, _info: data.job_skipped,
    ),
    # How pages reached the printer. The split is the point: it answers
    # whether the wireless path is actually being used, which the single
    # printed-page total cannot.
    HPPrinterSensorDescription(
        key="network_printed_pages",
        translation_key="network_printed_pages",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda data, _info: data.network_printed_pages,
    ),
    HPPrinterSensorDescription(
        key="wireless_printed_pages",
        translation_key="wireless_printed_pages",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda data, _info: data.wireless_printed_pages,
    ),
    HPPrinterSensorDescription(
        key="ews_accesses",
        translation_key="ews_accesses",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_registry_enabled_default=False,
        value_fn=lambda data, _info: data.ews_access_count,
    ),
    # --- LEDM: hardware and exposure -------------------------------------
    HPPrinterSensorDescription(
        key="memory_available",
        translation_key="memory_available",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        device_class=SensorDeviceClass.DATA_SIZE,
        # The unit lives in the translations, not here: a translated unit is
        # ignored while the description also defines one, and the translation
        # carries KiB for both of these.
        value_fn=lambda data, info: info.available_memory_kb,
    ),
    HPPrinterSensorDescription(
        key="memory_total",
        translation_key="memory_total",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        device_class=SensorDeviceClass.DATA_SIZE,
        value_fn=lambda data, info: info.total_memory_kb,
    ),
    HPPrinterSensorDescription(
        key="input_trays",
        translation_key="input_trays",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data, _info: data.input_tray_count,
    ),
    HPPrinterSensorDescription(
        key="output_bins",
        translation_key="output_bins",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data, _info: data.output_bin_count,
    ),
    HPPrinterSensorDescription(
        key="country_region",
        translation_key="country_region",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data, info: info.country_region,
    ),
    HPPrinterSensorDescription(
        key="device_language",
        translation_key="device_language",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data, info: info.device_language,
    ),
    HPPrinterSensorDescription(
        key="default_orientation",
        translation_key="default_orientation",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        state_class=SensorDeviceClass.ENUM,
        options=["Portrait", "Landscape"],
        value_fn=lambda data, _info: data.default_orientation,
    ),
    HPPrinterSensorDescription(
        key="failed_attempts_remaining",
        translation_key="failed_attempts_remaining",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        state_class=SensorStateClass.MEASUREMENT,
        # A password-guessing budget, and the reason a factory-default admin
        # password is worth changing before anything else on this list.
        value_fn=lambda data, info: info.failed_attempts_remaining,
    ),
    # --- diagnostics: network health ---
    # The one entity of this group that is on by default: a single number to
    # alert on, with the individual counters attached. A rising value means
    # the link is degrading -- a marginal cable, a failing switch port -- and
    # nothing else the printer exposes says so.
    HPPrinterSensorDescription(
        key="network_errors",
        translation_key="network_errors",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda data, _info: data.network.total_errors,
        attrs_fn=lambda data: {
            **data.network.error_counts,
            "port_type": data.network.port_type,
            "link_mode": data.network.link_mode,
        },
    ),
    HPPrinterSensorDescription(
        key="network_link_mode",
        translation_key="network_link_mode",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data, _info: data.network.link_mode,
        attrs_fn=lambda data: {"port_type": data.network.port_type},
    ),
    _network_counter(
        "network_bad_packets", "network_bad_packets", lambda n: n.bad_packets_received
    ),
    _network_counter(
        "network_framing_errors", "network_framing_errors", lambda n: n.framing_errors
    ),
    _network_counter(
        "network_transmit_collisions",
        "network_transmit_collisions",
        lambda n: n.transmit_collisions,
    ),
    _network_counter(
        "network_late_collisions",
        "network_late_collisions",
        lambda n: n.transmit_late_collisions,
    ),
    _network_counter(
        "network_unsendable_packets",
        "network_unsendable_packets",
        lambda n: n.unsendable_packets,
    ),
    _network_counter(
        "network_packets_received",
        "network_packets_received",
        lambda n: n.packets_received,
    ),
    _network_counter(
        "network_packets_transmitted",
        "network_packets_transmitted",
        lambda n: n.packets_transmitted,
    ),
    HPPrinterSensorDescription(
        key="last_job_source",
        translation_key="last_job_source",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data, _info: (
            data.last_job.application_id if data.last_job else None
        ),
        attrs_fn=lambda data: {
            "user": data.last_job.user_id if data.last_job else None,
            "name": data.last_job.name if data.last_job else None,
            "pages": data.last_job.total_impressions if data.last_job else None,
        },
    ),
)


CONSUMABLE_SENSORS: tuple[HPConsumableSensorDescription, ...] = (
    HPConsumableSensorDescription(
        key="level",
        translation_key="cartridge_level",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda c: c.level_percent,
        # Station is the physical slot and type is what the slot holds. Both
        # are fixed for the life of the cartridge, so they ride along as
        # attributes rather than becoming sensors that never change.
        attrs_fn=lambda c: {
            "station": c.station,
            "consumable_type": c.consumable_type,
        },
    ),
    HPConsumableSensorDescription(
        key="pages_remaining",
        translation_key="cartridge_pages_remaining",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda c: c.pages_remaining,
    ),
    HPConsumableSensorDescription(
        key="pages_printed",
        translation_key="cartridge_pages_printed",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda c: c.total_impressions,
    ),
    HPConsumableSensorDescription(
        key="raw_level",
        translation_key="cartridge_raw_level",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda c: c.raw_level_percent,
    ),
    HPConsumableSensorDescription(
        key="low_threshold",
        translation_key="cartridge_low_threshold",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        # The manufacturer's own low point, so a low-toner automation can use
        # a real threshold rather than a guessed one.
        value_fn=lambda c: c.low_threshold_percent,
    ),
    # The following describe the cartridge previously REMOVED from this slot,
    # not the one installed. They are useful for judging how worn a cartridge
    # was when it was swapped, but they must never be read as current state,
    # so they are named explicitly and disabled by default.
    HPConsumableSensorDescription(
        key="previous_developer_life",
        translation_key="cartridge_previous_developer_life",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda c: c.previous_developer_life,
    ),
    HPConsumableSensorDescription(
        key="previous_drum_life",
        translation_key="cartridge_previous_drum_life",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda c: c.previous_drum_life,
    ),
    HPConsumableSensorDescription(
        key="previous_part_number",
        translation_key="cartridge_previous_part_number",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda c: c.previous_part_number,
    ),
    HPConsumableSensorDescription(
        key="brand",
        translation_key="cartridge_brand",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: c.brand,
    ),
    HPConsumableSensorDescription(
        key="part_number",
        translation_key="cartridge_part_number",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda c: c.part_number,
    ),
    HPConsumableSensorDescription(
        key="manufactured_at",
        translation_key="cartridge_manufactured_at",
        device_class=SensorDeviceClass.DATE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: c.manufactured_at.date() if c.manufactured_at else None,
    ),
    HPConsumableSensorDescription(
        key="installed_at",
        translation_key="cartridge_installed_at",
        device_class=SensorDeviceClass.DATE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: c.installed_at.date() if c.installed_at else None,
    ),
    # Refill counts come from the cartridge's own chip. A non-zero counterfeit
    # count is how the device says it has seen a refill it could not
    # authenticate -- it does not by itself mean the cartridge is bad.
    HPConsumableSensorDescription(
        key="counterfeit_refills",
        translation_key="cartridge_counterfeit_refills",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda c: c.counterfeit_refills,
    ),
    HPConsumableSensorDescription(
        key="genuine_refills",
        translation_key="cartridge_genuine_refills",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda c: c.genuine_refills,
    ),
    # The supplier's own manufacture date. On the CDP models a cartridge is a
    # printhead rather than an ink container, and this is the field that dates
    # it -- ConsumableConfigDyn's Manufacturer/Date is absent for those.
    HPConsumableSensorDescription(
        key="supplier_manufacture_date",
        translation_key="cartridge_supplier_manufacture_date",
        device_class=SensorDeviceClass.DATE,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda c: _iso_date(c.manufacture_date),
    ),
    # Why the device says what it says about this slot. ``usedConsumableInfo``
    # is the explanation for a slot flagged as previously used, and it is NOT
    # the same claim as "not genuine" -- that distinction is exactly the one
    # that gets misread.
    HPConsumableSensorDescription(
        key="state_reason",
        translation_key="cartridge_state_reason",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda c: ", ".join(c.state_reasons) or None,
    ),
    HPConsumableSensorDescription(
        key="warranty_expires_at",
        translation_key="cartridge_warranty_expires_at",
        device_class=SensorDeviceClass.DATE,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda c: (
            c.warranty_expires_at.date() if c.warranty_expires_at else None
        ),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HPPrinterConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up sensors for a printer."""
    coordinator = entry.runtime_data
    data = coordinator.data
    info = coordinator.product_info

    entities: list[SensorEntity] = [
        HPSubunitSensor(
            coordinator,
            description,
            description.subunit,
        )
        if description.subunit
        else HPPrinterSensor(coordinator, description)
        for description in PRINTER_SENSORS
        # Not every model populates every subunit -- a printer with no ADF or
        # no fax simply omits those counters. Skip them rather than creating
        # entities that can only ever be unknown.
        if description.value_fn(data, info) is not None
    ]

    entities.extend(
        HPConsumableSensor(coordinator, description, code)
        for code, consumable in data.consumables.items()
        for description in CONSUMABLE_SENSORS
        if description.value_fn(consumable) is not None
    )

    async_add_entities(entities)


class HPPrinterSensor(HPPrinterEntity, SensorEntity):
    """A printer-level sensor."""

    entity_description: HPPrinterSensorDescription

    @property
    def native_value(self) -> StateType | date:
        """Return the sensor value."""
        return self.entity_description.value_fn(
            self.coordinator.data, self.coordinator.product_info
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return supplementary detail, where the sensor defines any."""
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.coordinator.data)


class HPSubunitSensor(HPSubunitEntity, SensorEntity):
    """A sensor belonging to a scanner or copier sub-device."""

    entity_description: HPPrinterSensorDescription

    @property
    def native_value(self) -> StateType | date:
        """Return the sensor value."""
        return self.entity_description.value_fn(
            self.coordinator.data, self.coordinator.product_info
        )


class HPConsumableSensor(HPConsumableEntity, SensorEntity):
    """A cartridge-level sensor."""

    entity_description: HPConsumableSensorDescription

    @property
    def native_value(self) -> StateType | date:
        """Return the sensor value."""
        if (consumable := self.consumable) is None:
            return None
        return self.entity_description.value_fn(consumable)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return supplementary detail, where the sensor defines any."""
        if self.entity_description.attrs_fn is None:
            return None
        if (consumable := self.consumable) is None:
            return None
        return self.entity_description.attrs_fn(consumable)
