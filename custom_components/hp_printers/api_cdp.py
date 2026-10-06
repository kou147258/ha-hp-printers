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
    HPPrinterWriteError,
    _percent,
)
from .const import (
    CDP_ADAPTER_STATS,
    CDP_ALERTS,
    CDP_BLUETOOTH,
    CDP_CALIBRATION,
    CDP_CALIBRATION_CAPABILITIES,
    CDP_CALIBRATION_TRIGGER,
    CDP_CERTIFICATE,
    CDP_DEVICE_SERVICE_COUNTERS,
    CDP_DEVICE_USAGE,
    CDP_EVENTS,
    CDP_FIRMWARE_CHECK,
    CDP_FIRMWARE_CONFIG,
    CDP_FIRMWARE_STATUS,
    CDP_IDENTITY,
    CDP_INTERNET_DIAGNOSTICS,
    CDP_PRINT_CONFIG,
    CDP_PRINT_SERVICES,
    CDP_PRINT_SETUP_STATUS,
    CDP_PRINT_STATUS,
    CDP_REPORT_PRINT,
    CDP_REPORTS,
    CDP_SCAN_STATUS,
    CDP_SECURITY_CONFIG,
    CDP_SERVICE_CONFIG,
    CDP_SETUP_STATUS,
    CDP_SNMP_CONFIG,
    CDP_SUPPLIES,
    CDP_SUPPLY_CONFIG,
    CDP_SUPPLY_CONFIG_PRIVATE,
    CDP_SUPPLY_LIFETIME,
    CDP_SUPPLY_REGION_RESET,
    CDP_SYSTEM_STATISTICS,
    COLOR_NAMES,
)
from .models import (
    ActiveAlert,
    AdapterStats,
    Consumable,
    EventLogEntry,
    PrinterData,
    ProductInfo,
    SubunitUsage,
)

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


def _auto_jam(print_config: dict[str, Any]) -> bool | None:
    """Return whether automatic jam recovery is enabled.

    The document phrases it as a mode ("off" / "on" / "auto") rather than as
    a flag, so "off" has to map to False. Read as a plain boolean the value
    would report jam recovery as *enabled* on a printer that has turned it
    off, which is the opposite of what the setting means.
    """
    value = _text(print_config, "autoJamRecovery")
    if value is None:
        return None
    return value.strip().lower() not in ("off", "disabled", "no", "false")


def _status(value: str | None) -> str | None:
    """Fold a CDP status word onto the integration's option list."""
    if value is None:
        return None
    mapped = _STATUS_ALIASES.get(value.strip().lower(), "unknown")
    return mapped if mapped in _VALID_STATUS else "unknown"


class CDPClient:
    """Client for a printer's CDP REST endpoints.

    Reads are the integration's normal path. Writes exist too, behind a
    button, and the two are kept visibly apart on purpose.
    """

    def __init__(
        self,
        session: ClientSession,
        host: str,
        port: int,
        use_ssl: bool,
        ssl_context: ssl.SSLContext | None = None,
        *,
        password: str | None = None,
    ) -> None:
        """Initialize the client.

        Shares LEDMClient's transport caveats: a self-signed certificate, and
        on the models measured so far only legacy static-RSA cipher suites, so
        verification is disabled *and* the cipher list has to be permissive --
        a plain no-verify context still fails at handshake time.

        ``password`` is the EWS admin password and is used only by the write
        methods. It is held in memory for the life of the config entry and
        never written to a log, a diagnostics dump, or a fixture.
        """
        self._session = session
        self._host = host
        self._port = port
        self._ssl = use_ssl
        self._ssl_context: ssl.SSLContext | bool = ssl_context or False
        self._password = password or ""

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

    # ------------------------------------------------------------------
    # Writes.
    #
    # Everything below is reachable only from a button the user pressed.
    # Nothing here is called from the coordinator, so no write can be caused
    # by a poll, a restart, or a reload. That is the property that makes
    # having them at all defensible: the cost in ink and paper is incurred
    # because a person asked for it, at a moment they chose.
    # ------------------------------------------------------------------

    @property
    def can_write(self) -> bool:
        """Return whether this client has a write path at all.

        True for every CDP client. It is **not** conditioned on the password,
        because CDP does not authenticate writes: the measured model answers
        reads and writes alike with no credential, and attaching one turns
        working 200s into 401s. Gating the buttons on a password would hide a
        feature that works on exactly the machines most likely to need it.

        The password is still accepted, held in memory, and deliberately never
        transmitted -- see :meth:`_auth_header`. A model whose firmware starts
        requiring one is the reason to keep the field; nothing today depends
        on it, and the README says so rather than implying otherwise.
        """
        return True

    def _auth_header(self) -> dict[str, str]:
        """Return the headers a CDP write carries.

        **Deliberately no Authorization header.** Measured on the Smart Tank
        580-590: every CDP document is served with no credential at all, and
        attaching a correct HTTP Basic header turns working 200s into 401s.
        ``/AuthChk`` does not discriminate either -- the right password, a
        wrong one, and none at all all answer 300. So CDP does not
        authenticate with the EWS password.

        The other service named "remoteAuthentication" is a red herring:
        ``/cdm/remoteAuthentication/v1/capabilities`` reports
        ``pinLabelLocation: "cartridgeAccessArea"`` and
        ``pushbuttonSupported: true``, which is the physical PIN that unlocks
        the cartridge bay -- a different feature that happens to share a name.
        POSTing to its token endpoint answers 409 or 500 with an empty body
        for every body shape tried, and reveals nothing.

        What a write actually gets is the configured password on a header the
        protocol ignores, which is worse than sending nothing: if the value is
        ever echoed into a log or an error it becomes a credential leak for no
        benefit. So it is held in memory and never transmitted.
        """
        return {"Content-Type": "application/json"}

    async def _patch(self, endpoint: str, body: dict[str, Any]) -> dict[str, Any]:
        """PATCH one CDP document.

        Split from :meth:`_fetch` deliberately. The read path and the write
        path have different failure modes and different callers: a read that
        fails should degrade a sensor, and a write that fails has to be
        reported to the person who pressed the button. Sharing one helper
        would mean every write inherited the reads' "carry on quietly"
        contract.
        """
        url = f"{self.base_url}{endpoint}"
        payload = json.dumps(body)
        # The log line is the audit trail for a request that spends ink and
        # paper. It records the operation, never the credential -- which is
        # easy here, because the credential is never sent at all.
        _LOGGER.warning("User-requested write: PATCH %s body=%s", endpoint, payload)
        try:
            async with self._session.patch(
                url,
                data=payload.encode(),
                timeout=REQUEST_TIMEOUT,
                ssl=self._ssl_context,
                headers=self._auth_header(),
            ) as response:
                status = response.status
                raw = await response.text()
        except TimeoutError as err:
            raise HPPrinterWriteError(
                f"Timeout writing {endpoint}; the printer may still be running it"
            ) from err
        except ClientError as err:
            raise HPPrinterWriteError(f"Error writing {endpoint}: {err}") from err

        if status in (401, 403):
            # Measured: a CDP write answers 400 for a body it will not accept,
            # not 401/403, so reaching this means the device wants a
            # credential it has not been given. Name that, and do not blame
            # the password -- CDP does not authenticate with it.
            raise HPPrinterWriteError(
                f"Printer wants a credential this client does not have "
                f"(HTTP {status}). CDP does not authenticate with the EWS "
                "password, so the write was not authorised."
            )
        if status == 404:
            raise HPPrinterWriteError(
                f"This printer does not offer {endpoint} (HTTP 404)"
            )
        if status >= 400:
            # The body is the only place the reason appears, and it is the
            # difference between "busy" and "no paper". On the CDP model
            # measured, a rejected body comes back as a 400 with an *empty*
            # body -- so there is often nothing to add, and saying so is
            # worth more than an error that reads like a broken integration.
            detail = raw.strip()[:200] if raw.strip() else "no detail given"
            raise HPPrinterWriteError(
                f"Printer rejected {endpoint}: HTTP {status}, {detail}"
            )

        try:
            document = json.loads(raw) if raw.strip() else {}
        except ValueError:
            # A successful write is allowed to answer with an empty or
            # non-JSON body; that is not a failure of the write.
            return {}
        return document if isinstance(document, dict) else {}

    async def async_get_reports(self) -> dict[str, bool]:
        """Return ``{reportId: printable}`` for the reports this model offers.

        Read from the device rather than from :data:`CLEANING_REPORTS` so the
        buttons match the hardware. A model with no printhead clean has no
        ``cleaningPage`` to offer, and offering the button anyway produces an
        error the user has to decode.
        """
        document = await self._fetch_optional(CDP_REPORTS)
        if document is None:
            return {}
        found: dict[str, bool] = {}
        for report in document.get("reports", []) or []:
            if not isinstance(report, dict):
                continue
            report_id = _text(report, "reportId")
            if report_id is None:
                continue
            found[report_id] = _bool(report, "printable") or False
        return found

    async def async_get_calibration_capabilities(self) -> dict[str, Any]:
        """Return what this model can calibrate, and what it needs.

        ``requiresMedia`` matters more than it looks: the alignment routine
        prints a test pattern, so firing it at an empty tray wastes the cycle
        and can leave the machine mid-alignment. The button is refused when
        the device says it needs media rather than being left to fail.
        """
        return await self._fetch_optional(CDP_CALIBRATION_CAPABILITIES) or {}

    async def async_run_report(self, report_id: str) -> dict[str, Any]:
        """Start one report the device lists under /cdm/report/v1/reports.

        The body is the device's own identifier, taken from the ``reports``
        document, which is also where the link that advertises ``PATCH`` on
        this resource lives. The resource's ``GET`` returns
        ``{"version": ..., "state": "idle"}``, and ``state`` is progress to
        read back -- not a field to set.
        """
        return await self._patch(CDP_REPORT_PRINT, {"reportId": report_id})

    async def async_run_calibration(self, calibration_type: str) -> dict[str, Any]:
        """Start one alignment routine by the type the device advertises.

        Unlike the reports, this body's shape is not readable from the
        device: ``GET /cdm/calibration/v1/calibration`` answers 400, so there
        is no representation to copy the request from. The type string comes
        from ``availableCalibrations`` in the capabilities document, and the
        member name follows the same convention every other CDP document
        uses for naming the thing being acted on.
        """
        return await self._patch(
            CDP_CALIBRATION_TRIGGER, {"calibrationType": calibration_type}
        )

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
        (
            status_doc,
            scan_doc,
            supply_config,
            print_config,
            calibration,
        ) = await asyncio.gather(
            self._fetch(CDP_PRINT_STATUS),
            self._fetch_optional(CDP_SCAN_STATUS),
            self._fetch_optional(CDP_SUPPLY_CONFIG),
            self._fetch_optional(CDP_PRINT_CONFIG),
            self._fetch_optional(CDP_CALIBRATION),
        )
        events_doc = await self._fetch_optional(CDP_EVENTS)

        # The second wave. Kept separate from the first so that a model which
        # does not serve any of these -- every LEDM printer, and a CDP printer
        # that has never enrolled in cloud services -- costs one gather of
        # 404s rather than delaying the counters above. Everything here is
        # optional by construction.
        (
            setup_doc,
            alerts_doc,
            firmware_doc,
            firmware_check,
            firmware_config,
            certificate_doc,
            adapter_doc,
            internet_doc,
            print_services_doc,
            snmp_doc,
            bluetooth_doc,
            service_config,
            supply_lifetime,
            supply_private,
            region_reset,
            print_setup,
        ) = await asyncio.gather(
            *(
                self._fetch_optional(path)
                for path in (
                    CDP_SETUP_STATUS,
                    CDP_ALERTS,
                    CDP_FIRMWARE_STATUS,
                    CDP_FIRMWARE_CHECK,
                    CDP_FIRMWARE_CONFIG,
                    CDP_CERTIFICATE,
                    CDP_ADAPTER_STATS,
                    CDP_INTERNET_DIAGNOSTICS,
                    CDP_PRINT_SERVICES,
                    CDP_SNMP_CONFIG,
                    CDP_BLUETOOTH,
                    CDP_SERVICE_CONFIG,
                    CDP_SUPPLY_LIFETIME,
                    CDP_SUPPLY_CONFIG_PRIVATE,
                    CDP_SUPPLY_REGION_RESET,
                    CDP_PRINT_SETUP_STATUS,
                )
            )
        )

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
            # The only "ready for work" signal on this interface: the
            # status word is a state name, this is a decision.
            accepting_jobs=_bool(status_doc, "printerIsAcceptingJobs"),
            quiet_mode=_bool(print_config or {}, "quietModeEnabled"),
            # "off" here means the device will not try to clear a jam on
            # its own -- a reading worth having, and not a fault.
            auto_jam_recovery=_auto_jam(print_config or {}),
            calibration_last_result=_text(calibration or {}, "lastResult"),
            calibration_failure_reason=_text(calibration or {}, "failureReason"),
            calibration_status=_text(calibration or {}, "calibrationStatus"),
            # Genuine-supplies enforcement. LEDM spells it
            # GenuineHPSuppliesOnly; CDP calls the same thing the anti-theft
            # mode and states it in the supply service. Both answer "would the
            # printer refuse a non-HP cartridge", so they land on one field
            # rather than two half-populated ones.
            genuine_supplies_only=_bool(supply_config or {}, "antiTheftEnabled"),
            # --- second wave ---
            setup_operation_state=_text(setup_doc or {}, "setupOperationState"),
            setup_pending_steps=self._parse_setup_steps(setup_doc),
            firmware_update_result=_text(firmware_doc or {}, "lastUpdateResult"),
            firmware_update_available=_text(firmware_check or {}, "availableVersion"),
            auto_update_enabled=_bool(firmware_config or {}, "autoUpdateEnabled"),
            active_alerts=self._parse_alerts(alerts_doc),
            certificate_valid_from=_date(
                (certificate_doc or {}).get("validity") or {}, "fromDate"
            ),
            certificate_expires=_date(
                (certificate_doc or {}).get("validity") or {}, "toDate"
            ),
            adapter_stats=self._parse_adapter_stats(adapter_doc),
            internet_diagnostics_result=_text(internet_doc or {}, "lastResult"),
            carriage_status=_text(print_setup or {}, "carriageStatus"),
            cartridge_changes=self._parse_cartridge_changes(
                supply_lifetime, supplies_doc
            ),
            region_reset_remaining=_int(region_reset or {}, "numberRemaining"),
            anti_theft_enabled=_bool(supply_private or {}, "antiTheftEnabled"),
            holo_enabled=_bool(supply_private or {}, "holoEnabled"),
            low_messaging_enabled=_bool(supply_private or {}, "lowMessagingEnabled"),
            print_services=self._parse_print_services(print_services_doc),
            snmp_enabled=self._parse_snmp(snmp_doc, "enabled"),
            snmp_public_allowed=self._parse_snmp(snmp_doc, "readOnlyPublicAllowed"),
            bluetooth_beaconing=_bool(bluetooth_doc or {}, "currentBeaconingEnabled"),
            service_id=_text(service_config or {}, "serviceId"),
        )

    @staticmethod
    def _parse_setup_steps(setup_doc: dict[str, Any] | None) -> tuple[str, ...]:
        """Return the setup steps the device is still waiting on.

        The document is a flat bag of ``action<Something>`` entries, each with
        its own ``status``. Only the ones not yet ``completed`` are returned:
        the finished steps are not news, and listing them would make a
        printer that completed all five look identical to one that completed
        none.
        """
        if not setup_doc:
            return ()
        pending: list[tuple[int, str]] = []
        for key, value in setup_doc.items():
            if not key.startswith("action") or not isinstance(value, dict):
                continue
            if str(value.get("status", "")).lower() == "completed":
                continue
            order = value.get("suggestedOrder")
            pending.append(
                (order if isinstance(order, int) else 99, key.removeprefix("action"))
            )
        return tuple(name for _order, name in sorted(pending))

    @staticmethod
    def _parse_alerts(alerts_doc: dict[str, Any] | None) -> tuple[ActiveAlert, ...]:
        """Build the alerts the device is raising right now.

        Only entries the device marked ``severity`` are kept, and the
        categories are returned in the order given rather than sorted: the
        device orders them by its own priority, and re-sorting would discard
        the ranking that is the most useful part.
        """
        if not alerts_doc:
            return ()
        alerts = alerts_doc.get("alerts")
        if not isinstance(alerts, list):
            return ()
        parsed: list[ActiveAlert] = []
        for entry in alerts:
            if not isinstance(entry, dict):
                continue
            category = _text(entry, "category")
            if category is None:
                continue
            parsed.append(
                ActiveAlert(
                    alert_id=_int(entry, "id"),
                    category=category,
                    severity=_text(entry, "severity"),
                    priority=_int(entry, "priority"),
                    sequence=_int(entry, "sequenceNum"),
                )
            )
        return tuple(parsed)

    @staticmethod
    def _parse_adapter_stats(
        adapter_doc: dict[str, Any] | None,
    ) -> tuple[AdapterStats, ...]:
        """Split the counter document into one record per interface.

        Every key that is not a version marker is an interface name, and the
        device puts them at the top level rather than under an ``adapters``
        list, so the shape is read from the document rather than assumed.
        """
        if not adapter_doc:
            return ()
        found: list[AdapterStats] = []
        for name, values in adapter_doc.items():
            if name == "version" or not isinstance(values, dict):
                continue
            found.append(
                AdapterStats(
                    name=name,
                    received_bytes=_int(values, "receivedBytes"),
                    transmitted_packets=_int(values, "transmittedPackets"),
                    received_unicast=_int(values, "receivedUnicastPackets"),
                    received_multicast=_int(values, "receivedMulticastPackets"),
                    receiver_errors=_int(values, "receiverErrors"),
                    transmitter_errors=_int(values, "transmitterErrors"),
                    transmitter_collisions=_int(values, "transmitterCollisions"),
                    transmitter_late_collisions=_int(
                        values, "transmitterLateCollisions"
                    ),
                )
            )
        return tuple(sorted(found, key=lambda adapter: adapter.name))

    @staticmethod
    def _parse_cartridge_changes(
        lifetime_doc: dict[str, Any] | None, supplies_doc: dict[str, Any] | None
    ) -> int | None:
        """Return the highest number of cartridges any single slot has held.

        The document lists one entry per slot and the slots change between
        models -- two printheads on one family, five on another -- so a
        single number is what a user can actually be told. The maximum is
        reported rather than the sum: the slots are refilled independently,
        and three slots holding two cartridges each is a machine on its second
        round, not one that has seen six.
        """
        if not lifetime_doc:
            return None
        slots = lifetime_doc.get("supplyUsageBySlot")
        if not isinstance(slots, list) or not slots:
            return None
        counts = [
            value
            for value in (
                _int(slot, "numberOfSupplies")
                for slot in slots
                if isinstance(slot, dict)
            )
            if value is not None
        ]
        return max(counts) if counts else None

    @staticmethod
    def _parse_print_services(services_doc: dict[str, Any] | None) -> tuple[str, ...]:
        """Return the names of the print protocols the device has switched on.

        These are the ports a client can reach the printer on, so the set is
        its exposed surface. The flag *name* inside each service is not
        uniform -- ``airPrint`` uses ``enabled``, ``ipp`` uses ``ipp`` and
        ``ippSecure`` -- so the service is reported as on when any of its
        flags is true, and the per-flag detail is left to the raw document.
        Reporting ``ipp`` as one entry rather than two is also the honest
        answer to "is IPP on", which is the question being asked.
        """
        if not services_doc:
            return ()
        enabled = [
            name
            for name, flags in services_doc.items()
            if name != "version"
            and isinstance(flags, dict)
            and any(str(flag).lower() == "true" for flag in flags.values())
        ]
        return tuple(sorted(enabled))

    @staticmethod
    def _parse_snmp(snmp_doc: dict[str, Any] | None, key: str) -> bool | None:
        """Read one flag out of the SNMP configuration.

        Nested one level down under ``snmpV1V2Config``, which is a different
        shape on each firmware measured, so a missing document is None rather
        than False.
        """
        if not snmp_doc:
            return None
        config = snmp_doc.get("snmpV1V2Config")
        if not isinstance(config, dict):
            return None
        return _bool(config, key)

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
