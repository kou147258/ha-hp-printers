"""Client for HP's CDP ("Common Data Platform") JSON interface.

Not every HP model serves LEDM. Newer consumer models -- Smart Tank 580-590
measured 2026-10-05 -- answer ``/DevMgmt/DiscoveryTree.xml`` with 404 and
expose the same facts through a REST surface at
``/cdm/<service>/<version>/<resource>`` returning JSON.

HP publishes no specification for either interface. The endpoint map below was
derived by reading a live device rather than guessed: the paths come out of
the printer's own ``/webApps/*/*.js`` and out of ``resourcePath`` fields in
its own alert payloads, and the 24 endpoints that exist were confirmed by
enumerating 1500 service/version/resource combinations.

The client deliberately mirrors :class:`.api.LEDMClient`'s three methods and
returns the same dataclasses, so nothing above the client layer -- coordinator,
entities, config flow -- has to know which protocol a printer speaks.
"""

import asyncio
from dataclasses import replace
from datetime import datetime
import json
import logging
import ssl
from typing import Any

from aiohttp import ClientError, ClientSession, ClientTimeout

from .api import (
    HPPrinterConnectionError,
    HPPrinterError,
    HPPrinterNotSupportedError,
    HPPrinterParseError,
    _percent,
)
from .const import (
    CDP_DEVICE_SERVICE_COUNTERS,
    CDP_DEVICE_USAGE,
    CDP_EVENTS,
    CDP_IDENTITY,
    CDP_PRINT_STATUS,
    CDP_SCAN_STATUS,
    CDP_SECURITY_CONFIG,
    CDP_SUPPLIES,
    CDP_SUPPLY_CONFIG,
    CDP_SYSTEM_STATISTICS,
    COLOR_NAMES,
)
from .models import Consumable, EventLogEntry, PrinterData, ProductInfo, SubunitUsage

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = ClientTimeout(total=20)

# The CDP print engine says "idle" where the EWS page says "ready", and
# capitalises "Idle" on the scan service. Both fold onto the single "ready"
# option the status sensor declares, so the two protocols present the same
# physical state under the same name.
#
# Anything unrecognised folds to "unknown" rather than passing through: a
# value outside the declared options does not read as "unknown" to the
# entity, it makes the entity refuse to be created at all.
_STATUS_ALIASES = {
    "idle": "ready",
    "ready": "ready",
    "inpowersave": "inpowersave",
    "printing": "processing",
    "processing": "processing",
    "scanning": "scanning",
    "copying": "copying",
    "off": "off",
    "initializing": "initializing",
    "cancelling": "cancelling",
    "shuttingdown": "shuttingdown",
    "trayempty": "trayempty",
    "outofpaper": "outofpaper",
    "papermisfeed": "papermisfeed",
    "nomediainstalled": "nomediainstalled",
    "closedoorcover": "closedoorcover",
    "unknown": "unknown",
}

# Mirrors const.STATUS_OPTIONS rather than importing it: this module already
# imports from const, and a test asserts the two stay in step.
_VALID_STATUS = frozenset(
    {
        "cancelling",
        "closedoorcover",
        "copying",
        "inpowersave",
        "initializing",
        "nomediainstalled",
        "off",
        "outofpaper",
        "papermisfeed",
        "processing",
        "ready",
        "scanning",
        "shuttingdown",
        "trayempty",
        "unknown",
    }
)


def _text(document: dict[str, Any], key: str) -> str | None:
    """Return a stripped string field, or None when absent or empty."""
    value = document.get(key)
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _int(source: Any, key: str) -> int | None:
    """Return an int field, or None when absent or non-numeric.

    CDP reports counters as JSON numbers, but a firmware quirk observed on the
    Smart Tank 580-590 puts a negative value in ``scanUsage.sendImages``. A
    caller asking for a count has to be able to tell that apart from "the
    device does not report this", so the sentinel is dropped here rather than
    reaching an entity as a plausible-looking number.
    """
    if not isinstance(source, dict):
        return None
    value = source.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value < 0:
        return None
    return int(value)


def _bool(document: dict[str, Any], key: str) -> bool | None:
    """Interpret the string flags CDP uses for booleans."""
    value = _text(document, key)
    if value is None:
        return None
    return value.lower() in ("enabled", "true", "set", "yes", "on")


def _date(document: dict[str, Any], key: str) -> datetime | None:
    """Parse an install-date string, discarding an unset-clock placeholder.

    The identity document reports e.g. ``3/14/2025 12:00:00 AM`` in the
    documented US format. A device with no real-time clock substitutes
    1976-01-01, which is dropped for the same reason the LEDM side drops it.
    """
    raw = _text(document, key)
    if raw is None:
        return None
    for fmt in ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(raw[: len(fmt) + 6].strip(), fmt)
        except ValueError:
            continue
        if parsed.year <= 1976:
            return None
        return parsed
    return None


def _status(value: str | None) -> str | None:
    """Fold a CDP status word onto the integration's option list."""
    if value is None:
        return None
    mapped = _STATUS_ALIASES.get(value.strip().lower(), "unknown")
    return mapped if mapped in _VALID_STATUS else "unknown"


class CDPClient:
    """Read-only client for a printer's CDP REST endpoints."""

    def __init__(
        self,
        session: ClientSession,
        host: str,
        port: int,
        use_ssl: bool,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        """Initialize the client.

        Shares LEDMClient's transport caveats: a self-signed certificate, and
        on the models measured so far only legacy static-RSA cipher suites, so
        verification is disabled *and* the cipher list has to be permissive --
        a plain no-verify context still fails at handshake time.
        """
        self._session = session
        self._host = host
        self._port = port
        self._ssl = use_ssl
        self._ssl_context: ssl.SSLContext | bool = ssl_context or False

    @property
    def host(self) -> str:
        """Return the configured host."""
        return self._host

    @property
    def base_url(self) -> str:
        """Return the printer's base URL."""
        scheme = "https" if self._ssl else "http"
        return f"{scheme}://{self._host}:{self._port}"

    async def _fetch(self, endpoint: str) -> dict[str, Any]:
        """GET one CDP document and return the decoded object."""
        url = f"{self.base_url}{endpoint}"
        try:
            async with self._session.get(
                url, timeout=REQUEST_TIMEOUT, ssl=self._ssl_context
            ) as response:
                if response.status == 404:
                    raise HPPrinterNotSupportedError(f"404 from {endpoint}")
                response.raise_for_status()
                body = await response.text()
        except HPPrinterError:
            raise
        except TimeoutError as err:
            raise HPPrinterConnectionError(f"Timeout fetching {endpoint}") from err
        except ClientError as err:
            raise HPPrinterConnectionError(f"Error fetching {endpoint}: {err}") from err

        try:
            document = json.loads(body)
        except ValueError as err:
            raise HPPrinterParseError(f"Invalid JSON from {endpoint}: {err}") from err

        if not isinstance(document, dict):
            raise HPPrinterParseError(f"Unexpected JSON shape from {endpoint}")
        return document

    async def _fetch_optional(self, endpoint: str) -> dict[str, Any] | None:
        """GET a document the model may not serve at all.

        Same contract as the LEDM side: a device that answers but has nothing
        to say about a resource must not fail the whole update.
        """
        try:
            return await self._fetch(endpoint)
        except HPPrinterError as error:
            _LOGGER.debug("Optional CDP endpoint %s unavailable: %s", endpoint, error)
            return None

    async def async_get_product_info(self) -> ProductInfo:
        """Read static device information.

        The security document is folded in here rather than in
        ``async_get_data`` because it is static: the admin password is set
        once and never changes on its own, and a config entry keeps this
        object for the whole life of the printer.
        """
        document, security = await asyncio.gather(
            self._fetch(CDP_IDENTITY),
            self._fetch_optional(CDP_SECURITY_CONFIG),
        )
        info = self._parse_product_info(document)
        if security is None:
            return info
        return replace(
            info,
            # As on LEDM this gates writes only; every read this client makes
            # stays open either way, which is why the integration needs no
            # credentials.
            password_set=_bool(security, "passwordSet"),
        )

    @staticmethod
    def _parse_product_info(document: dict[str, Any]) -> ProductInfo:
        """Build a ``ProductInfo`` from the identity document.

        ``makeAndModel`` is a nested object; its ``name`` is what a person
        would call the model, and ``family`` is the product line. The install
        date lives here too, which LEDM's ProductConfigDyn does not carry for
        this model.
        """
        model = document.get("makeAndModel")
        model_name = model.get("name") if isinstance(model, dict) else None
        model_family = model.get("family") if isinstance(model, dict) else None
        if isinstance(model, str):
            # A bare string rather than the nested object. Not seen on either
            # measured model, but handling it costs three lines and a crash
            # here would take the whole update down.
            model_name, model_family = model.strip() or None, None
        else:
            if not isinstance(model_name, str):
                model_name = _text(model, "base") if isinstance(model, dict) else None
            if not isinstance(model_family, str):
                model_family = None

        return ProductInfo(
            make_and_model=model_name,
            make_and_model_family=model_family,
            serial_number=_text(document, "serialNumber"),
            product_number=_text(document, "productNumber"),
            sku_identifier=_text(document, "skuIdentifier"),
            uuid=_text(document, "deviceUuid"),
            service_id=_text(document, "serviceId"),
            # CDP's firmwareDateCode is a compact build stamp rather than LEDM's
            # ISO revision date. It is the only firmware version marker this
            # interface offers, so it is carried as the same field.
            firmware_date=_text(document, "firmwareDateCode"),
            installed_at=_date(document, "installDate"),
        )

    async def async_get_data(self) -> PrinterData:
        """Fetch everything that changes, concurrently."""
        # The identity document is deliberately not fetched here: static
        # product info comes from async_get_product_info on the slow cadence,
        # and re-reading it on every poll would put a second request for the
        # same document on the wire for no gain.
        statistics, usage_doc, service_doc, supplies_doc = await asyncio.gather(
            self._fetch_optional(CDP_SYSTEM_STATISTICS),
            self._fetch(CDP_DEVICE_USAGE),
            self._fetch(CDP_DEVICE_SERVICE_COUNTERS),
            self._fetch(CDP_SUPPLIES),
        )
        status_doc, scan_doc, supply_config = await asyncio.gather(
            self._fetch(CDP_PRINT_STATUS),
            self._fetch_optional(CDP_SCAN_STATUS),
            self._fetch_optional(CDP_SUPPLY_CONFIG),
        )
        events_doc = await self._fetch_optional(CDP_EVENTS)

        return PrinterData(
            status=_status(_text(status_doc, "printerState")),
            status_message=self._state_message(status_doc),
            consumables=self._parse_consumables(supplies_doc),
            printer=self._parse_printer_usage(usage_doc, service_doc),
            scanner=self._parse_scan_usage(usage_doc),
            copy=self._parse_copy_usage(usage_doc),
            events=self._parse_events(events_doc),
            scanner_status=_status(_text(scan_doc or {}, "scannerState")),
            scanner_error=_text(scan_doc or {}, "scannerError"),
            power_cycles=_int(statistics or {}, "powerCycleCount"),
            low_ink_messaging=_bool(supply_config or {}, "lowMessagingEnabled"),
            # Genuine-supplies enforcement. LEDM spells it
            # GenuineHPSuppliesOnly; CDP calls the same thing the anti-theft
            # mode and states it in the supply service. Both answer "would the
            # printer refuse a non-HP cartridge", so they land on one field
            # rather than two half-populated ones.
            genuine_supplies_only=_bool(supply_config or {}, "antiTheftEnabled"),
        )

    @staticmethod
    def _state_message(status_doc: dict[str, Any]) -> str | None:
        """Join the printer's state reasons into a display string."""
        reasons = status_doc.get("printerStateReasons")
        if isinstance(reasons, list):
            joined = ", ".join(str(reason) for reason in reasons if reason)
            return joined or None
        return None

    @staticmethod
    def _parse_consumables(supplies_doc: dict[str, Any]) -> dict[str, Consumable]:
        """Build consumables from the supply service's public document.

        Keyed by ``supplyColorCode`` rather than by slot: the code is what
        identifies a colour across models, while slot numbering is an internal
        detail that differs between the two families of printer this
        integration serves.

        Note the two kinds of slot the document mixes. A slot of type
        ``inkCartridge`` is a **printhead** on the models measured so far, not
        an ink container -- HP's own web UI files these under "printhead" and
        the same document reports ``isRefilled``/``isUsed`` for it rather than a
        volume. A slot of type ``inkTank`` is the refillable reservoir and is
        the only slot that ever carries a level. Both are kept, because both
        are hardware the user can act on, but they are labelled by what the
        device says they are.
        """
        result: dict[str, Consumable] = {}
        for entry in supplies_doc.get("suppliesList") or []:
            if not isinstance(entry, dict):
                continue
            code = _text(entry, "supplyColorCode")
            if code is None:
                continue
            reasons = entry.get("stateReasons")
            result[code] = Consumable(
                label_code=code,
                color_name=COLOR_NAMES.get(code),
                consumable_type=_text(entry, "supplyType"),
                brand=_text(entry, "brand"),
                state=_text(entry, "supplyState"),
                level_percent=_percent(entry.get("percentLifeDisplay")),
                serial_number=_text(entry, "serialNumber"),
                part_number=_text(entry, "selectabilityNumber"),
                station=_int(entry, "slot"),
                manufacture_date=_text(entry, "manufactureDate"),
                is_genuine_reported=_bool(entry, "isGenuineHP"),
                is_refilled=_bool(entry, "isRefilled"),
                is_used=_bool(entry, "isUsed"),
                state_reasons=tuple(str(reason) for reason in reasons if reason)
                if isinstance(reasons, list)
                else (),
                is_trial=_bool(entry, "isTrial"),
                is_setup=_bool(entry, "isSetup"),
            )
        return result

    @staticmethod
    def _parse_printer_usage(
        usage_doc: dict[str, Any], service_doc: dict[str, Any]
    ) -> SubunitUsage:
        """Build the printer counters.

        Two different totals live in this document and they are not
        interchangeable. ``printUsage.impressions.total`` is the **engine page
        count**, which HP's own usage page states is never reset and counts
        every page the engine has ever handled -- including jams, internal
        retries, copies, scans and calibration. ``monochrome + color`` is the
        **user's printed pages**, and that is what this integration exposes as
        the total. Recording the engine count as the printed total was a real
        error worth naming in a comment.
        """
        print_usage = usage_doc.get("printUsage")
        print_usage = print_usage if isinstance(print_usage, dict) else {}
        impressions = print_usage.get("impressions")
        impressions = impressions if isinstance(impressions, dict) else {}
        sheets = print_usage.get("sheets")
        sheets = sheets if isinstance(sheets, dict) else {}
        hardware = service_doc.get("hardwareEvents")
        hardware = hardware if isinstance(hardware, dict) else {}

        return SubunitUsage(
            # The user's pages, not the engine lifetime count. See the docstring.
            total_impressions=_user_pages(impressions),
            monochrome_impressions=_int(impressions, "monochrome"),
            color_impressions=_int(impressions, "color"),
            simplex_sheets=_int(sheets, "simplex"),
            duplex_sheets=_int(sheets, "duplex"),
            jam_events=_int(hardware, "jamEventCount"),
            mispick_events=_int(hardware, "mispickEventCount"),
        )

    @staticmethod
    def _parse_scan_usage(usage_doc: dict[str, Any]) -> SubunitUsage:
        """Build the scanner counters.

        ``scanUsage`` is the whole of what this interface says about the
        scanner: an image total, a flatbed subtotal, and an image count
        attributed to copy jobs. It carries **no ADF/flatbed split for scan
        jobs** the way LEDM's ``ScanApplicationSubunit`` does, so the scan-job
        subunit is left empty rather than filled with a number that means
        something else.
        """
        scan_usage = usage_doc.get("scanUsage")
        scan_usage = scan_usage if isinstance(scan_usage, dict) else {}
        return SubunitUsage(
            scan_images=_int(scan_usage, "totalImages"),
            flatbed_images=_int(scan_usage, "flatbedImages"),
        )

    @staticmethod
    def _parse_copy_usage(usage_doc: dict[str, Any]) -> SubunitUsage:
        """Build the copier counters."""
        print_usage = usage_doc.get("printUsage")
        print_usage = print_usage if isinstance(print_usage, dict) else {}
        copies = print_usage.get("copyImpressions")
        copies = copies if isinstance(copies, dict) else {}
        scan_usage = usage_doc.get("scanUsage")
        scan_usage = scan_usage if isinstance(scan_usage, dict) else {}
        return SubunitUsage(
            total_impressions=_int(copies, "total"),
            monochrome_impressions=_int(copies, "monochrome"),
            color_impressions=_int(copies, "color"),
        )

    @staticmethod
    def _parse_events(events_doc: dict[str, Any] | None) -> list[EventLogEntry]:
        """Build the event log.

        This document is a **rolling window of the most recent 15 events**
        and it is cleared on reboot -- it is not a history. Entries carry no
        sequence number, so they are returned in the order the device gave
        them, which is newest first, and the parser does not pretend to sort
        what it cannot order.
        """
        if not events_doc:
            return []
        events = (events_doc.get("events") or {}).get("events")
        if not isinstance(events, list):
            return []
        return [
            EventLogEntry(
                code=_text(entry, "eventCode"),
                severity=_text(entry, "severity"),
            )
            for entry in events
            if isinstance(entry, dict)
        ]

    async def async_validate(self) -> ProductInfo:
        """Confirm the host speaks CDP and return its identity."""
        info = await self.async_get_product_info()
        if not info.serial_number:
            raise HPPrinterParseError("Device did not report a serial number")
        return info


def _user_pages(impressions: dict[str, Any]) -> int | None:
    """Return monochrome + color when both are present.

    The engine total and the user-page total are separate fields and the
    device only ever reports the split; adding the two halves is what HP's own
    usage page shows as "pages printed". Returns None when either half is
    missing, so a partial document does not produce a plausible-looking but
    wrong total.
    """
    monochrome = _int(impressions, "monochrome")
    colour = _int(impressions, "color")
    if monochrome is None or colour is None:
        return None
    return monochrome + colour
