"""Client for HP's LEDM ("Low End Data Model") XML interface.

HP does not publish a specification for LEDM. The endpoint map used here was
derived by reading a live device: /DevMgmt/DiscoveryTree.xml enumerates the
available resources, and each resource is exposed as a paired
<Resource>Cap.xml (capabilities, types, access modes) and <Resource>Dyn.xml
(current values). Everything below targets the Dyn documents.
"""

import asyncio
from datetime import datetime
import json
import logging
import ssl
from typing import Any
from xml.etree.ElementTree import Element

from aiohttp import ClientError, ClientSession, ClientTimeout
from defusedxml import ElementTree as DefusedET

from .const import (
    CDP_LEDM_INSTANT_INK,
    CDP_LEDM_PANEL,
    CDP_LEDM_QUIET_MODE,
    COLOR_NAMES,
    ENDPOINT_CONSUMABLE_CONFIG,
    ENDPOINT_IO_CONFIG,
    ENDPOINT_MEDIA_DYN,
    ENDPOINT_MEDIA_HANDLING,
    ENDPOINT_NET_APPS,
    ENDPOINT_PRINT_CONFIG,
    ENDPOINT_PRODUCT_CONFIG,
    ENDPOINT_PRODUCT_LOGS,
    ENDPOINT_PRODUCT_STATUS,
    ENDPOINT_PRODUCT_USAGE,
    ENDPOINT_SHOP_FOR_SUPPLIES,
    STATUS_OPTIONS,
)
from .models import (
    ActiveAlert,
    Consumable,
    EventLogEntry,
    JobEntry,
    NetworkHealth,
    PrinterData,
    ProductInfo,
    SubunitUsage,
)

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = ClientTimeout(total=20)


class HPPrinterError(Exception):
    """Base error for this integration."""


class HPPrinterConnectionError(HPPrinterError):
    """Raised when the printer cannot be reached."""


class HPPrinterParseError(HPPrinterError):
    """Raised when a response is not the LEDM document we expected."""


class HPPrinterNotSupportedError(HPPrinterError):
    """Raised when the host answers 404 for a resource.

    Distinct from a connection failure on purpose. A model that does not
    serve LEDM at all answers 404 for every endpoint, and treating that as
    "cannot connect" would report an online printer as offline. It is also
    the signal the protocol probe uses to fall through to CDP, so collapsing
    it into a connection error would make a supported printer look
    unconfigured.
    """


class HPPrinterWriteError(HPPrinterError):
    """Raised when the printer refuses a write request.

    Separate from the read-side errors on purpose. A write that the device
    rejects is not a connectivity problem and not a parse problem, and
    reporting it as either would tell the user the wrong thing to do: a
    connectivity error invites them to check the network, a parse error
    invites them to file a bug, and neither is what happened. The common
    causes are a wrong admin password, a maintenance cycle already running,
    and a model that does not offer the operation at all.
    """


def _localname(tag: str) -> str:
    """Strip the XML namespace from a tag."""
    return tag.rpartition("}")[2]


def _strip_namespaces(element: Element) -> Element:
    """Rewrite every tag in the tree to its local name.

    HP's documents use a different namespace prefix per schema, which makes
    ordinary ElementTree paths unreadable. Since local names are unique enough
    within a document, flattening is both safe and far easier to follow.
    """
    for node in element.iter():
        node.tag = _localname(node.tag)
    return element


def _find(root: Element, name: str) -> Element | None:
    """Return the first descendant with the given local name."""
    if _localname(root.tag) == name:
        return root
    return root.find(f".//{name}")


def _text(root: Element | None, *names: str) -> str | None:
    """Walk down by local name and return the leaf text.

    Each name is resolved as a descendant of the previous match, so
    _text(info, "Version", "Date") will not accidentally match the Date
    belonging to a sibling element such as LanguagePackVersion.
    """
    node = root
    for name in names:
        if node is None:
            return None
        node = _find(node, name)
    if node is None or node.text is None:
        return None
    value = node.text.strip()
    return value or None


def _int(root: Element | None, *names: str) -> int | None:
    """Return an int, or None when absent or non-numeric."""
    raw = _text(root, *names)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _float(root: Element | None, *names: str) -> float | None:
    """Return a float, or None when absent or non-numeric."""
    raw = _text(root, *names)
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _date(root: Element | None, *names: str) -> datetime | None:
    """Return a date, discarding HP's unset-clock placeholder.

    Devices without a real-time clock (non-fax models such as the M182nw)
    report 1976-01-01 rather than omitting the field.
    """
    raw = _text(root, *names)
    if raw is None:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(raw[: len(fmt) + 2].rstrip("Z"), fmt)
        except ValueError:
            continue
        if parsed.year <= 1976:
            return None
        return parsed
    return None


def _percent(value: float | None) -> float | None:
    """Discard the device's out-of-range sentinel for an unknown percentage.

    ProductUsageCap declares the raw level with min=-128, which is how the
    device says it does not know rather than a real reading.
    """
    if value is None or not 0 <= value <= 100:
        return None
    return value


def _sentinel(value: int | None) -> int | None:
    """Discard 127, which these wear counters use to mean unknown."""
    if value is None or value == 127:
        return None
    return value


def _enabled(value: str | None) -> bool | None:
    """Interpret HP's enabled/disabled and set/notSet string flags."""
    if value is None:
        return None
    return value.strip().lower() in ("enabled", "true", "set", "yes", "on")


class LEDMClient:
    """Read-only client for a printer's LEDM endpoints."""

    def __init__(
        self,
        session: ClientSession,
        host: str,
        port: int,
        use_ssl: bool,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        """Initialize the client.

        Printers serve a self-signed certificate and, at least on the models
        seen so far, only offer legacy static-RSA cipher suites such as
        AES128-GCM-SHA256. Python's default cipher list drops those, so a
        plain no-verify context is not enough -- the handshake fails before
        certificate checking is ever reached. Callers should pass a context
        built from a permissive cipher list.
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

    async def _fetch(self, endpoint: str) -> Element:
        """GET one LEDM document and return its namespace-stripped root."""
        url = f"{self.base_url}{endpoint}"
        try:
            async with self._session.get(
                url, timeout=REQUEST_TIMEOUT, ssl=self._ssl_context
            ) as response:
                if response.status == 404:
                    # Checked before raise_for_status: aiohttp folds this into
                    # ClientResponseError, which is a ClientError, and the
                    # handler below would report the printer as unreachable.
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
            root = DefusedET.fromstring(body)
        # defusedxml raises several distinct exception types.
        except Exception as err:
            raise HPPrinterParseError(f"Invalid XML from {endpoint}: {err}") from err

        return _strip_namespaces(root)

    async def _fetch_optional(self, endpoint: str) -> Element | None:
        """GET a document the device may not serve at all.

        Used for endpoints that are genuinely optional across models. A
        failure here must not fail the whole update: the printer answered,
        it simply has nothing to say about this resource.
        """
        try:
            return await self._fetch(endpoint)
        except HPPrinterError as error:
            _LOGGER.debug("Optional endpoint %s unavailable: %s", endpoint, error)
            return None

    async def async_get_product_info(self) -> ProductInfo:
        """Read static device information."""
        root = await self._fetch(ENDPOINT_PRODUCT_CONFIG)
        return self._parse_product_info(root)

    @staticmethod
    def _parse_product_info(root: Element) -> ProductInfo:
        """Build a ``ProductInfo`` from a parsed ``ProductConfigDyn`` root."""
        info = _find(root, "ProductInformation")
        if info is None:
            raise HPPrinterParseError("ProductConfigDyn missing ProductInformation")

        return ProductInfo(
            make_and_model=_text(info, "MakeAndModel"),
            make_and_model_family=_text(info, "MakeAndModelFamily"),
            serial_number=_text(info, "SerialNumber"),
            product_number=_text(info, "ProductNumber"),
            sku_identifier=_text(info, "SKUIdentifier"),
            manufactured_at=_date(info, "Manufacturer", "Date"),
            uuid=_text(info, "UUID"),
            service_id=_text(info, "ServiceID"),
            firmware_date=_text(info, "Version", "Date"),
            language_pack_version=_text(info, "LanguagePackVersion", "Revision"),
            password_set=_enabled(_text(info, "PasswordStatus")),
            duplex_unit=_text(info, "DuplexUnit"),
            # These live under ProductSettings rather than ProductInformation,
            # so they are read from the document root. AutoOffTime and
            # QuietPrintMode are siblings of PowerSaveTimeout, not children of
            # it, and looking for them inside the wrong element returns None
            # silently rather than raising.
            friendly_name=_text(root, "FriendlyName"),
            power_save=_text(info, "PowerSave"),
            power_save_timeout=_text(root, "PowerSaveTimeout"),
            shutdown_delay=_text(root, "ShutDownDelay"),
            auto_off_time=_text(root, "AutoOffTime"),
            quiet_mode=_enabled(_text(root, "QuietPrintMode")),
            **_parse_product_config(root),
        )

    async def async_get_data(self) -> PrinterData:
        """Fetch everything that changes, concurrently."""
        (
            status_doc,
            usage_doc,
            consumable_doc,
            logs_doc,
            io_doc,
            media_doc,
        ) = await asyncio.gather(
            self._fetch(ENDPOINT_PRODUCT_STATUS),
            self._fetch(ENDPOINT_PRODUCT_USAGE),
            self._fetch(ENDPOINT_CONSUMABLE_CONFIG),
            self._fetch(ENDPOINT_PRODUCT_LOGS),
            self._fetch_optional(ENDPOINT_IO_CONFIG),
            self._fetch_optional(ENDPOINT_MEDIA_HANDLING),
        )

        # ProductStatusDyn carries one <Status> per condition, and the
        # document does not order them; _parse_status picks the device state
        # out of them rather than assuming the first one is it.
        status, status_message = self._parse_status(status_doc)

        consumables = self._parse_consumables(consumable_doc, usage_doc)
        events, jobs, assert_text = self._parse_logs(logs_doc)

        # Second wave. Optional throughout, and kept out of the first gather
        # so that a model serving none of them still gets its counters on the
        # same tick.
        (
            print_config,
            media_dyn,
            net_apps,
            shop_for_supplies,
        ) = await asyncio.gather(
            self._fetch_optional(ENDPOINT_PRINT_CONFIG),
            self._fetch_optional(ENDPOINT_MEDIA_DYN),
            self._fetch_optional(ENDPOINT_NET_APPS),
            self._fetch_optional(ENDPOINT_SHOP_FOR_SUPPLIES),
        )

        # An LEDM printer also answers a handful of /cdm/ documents, and two
        # of them carry values LEDM does not expose anywhere: the quiet-print
        # flag and the control panel's language. Read on a model that has
        # them rather than reported absent, because "absent" would be a lie.
        quiet_mode, panel_language, instant_ink = await self._async_ledm_cdp()

        return PrinterData(
            status=status,
            status_message=status_message,
            consumables=consumables,
            printer=self._parse_subunit(usage_doc, "PrinterSubunit"),
            scanner=self._parse_subunit(usage_doc, "ScannerEngineSubunit"),
            scan=self._parse_subunit(usage_doc, "ScanApplicationSubunit"),
            copy=self._parse_subunit(usage_doc, "CopyApplicationSubunit"),
            events=events,
            jobs=jobs,
            assert_text=assert_text,
            genuine_supplies_only=_enabled(
                _text(consumable_doc, "GenuineHPSuppliesOnly")
            ),
            genuine_color_impressions=_int(usage_doc, "OriginalHPColorImpressions"),
            genuine_mono_impressions=_int(usage_doc, "OriginalHPMonochromeImpressions"),
            network=self._parse_network(io_doc),
            **_parse_marking_agent(usage_doc),
            **_parse_extra_counters(usage_doc),
            **_parse_quality_by_media(usage_doc),
            **_parse_media_handling(media_doc),
            **_parse_print_configuration(print_config),
            **_parse_current_media(media_dyn),
            **_parse_network_services(net_apps),
            **_parse_ledm_trays(media_doc),
            **_parse_ledm_exposure(net_apps),
            **_parse_ledm_jobs(usage_doc),
            **_parse_instant_ink(shop_for_supplies, instant_ink),
            # An LEDM printer reports its own live alerts, so they land on the
            # same field the CDP side fills. Without this the LEDM side would
            # report "no alerts" on a machine that publishes an AlertTable,
            # which is a different claim from having none.
            #
            # Its setup phase does NOT come here: OobePhase lives in the
            # product configuration, read on the slow cadence, so it lands on
            # ProductInfo and the sensor reads both.
            active_alerts=tuple(_parse_ledm_alerts(status_doc)),
            quiet_print_mode=quiet_mode,
            panel_language=panel_language,
        )

    async def _async_ledm_cdp(self) -> tuple[bool | None, str | None, str | None]:
        """Read the three CDP documents an LEDM printer also answers.

        This is not a fallback path. On the LEDM models measured, these are
        the *only* place the quiet-print flag, the panel language and the
        instant-ink status exist -- the XML side has no equivalent, so a
        client that only spoke LEDM would report all three as absent, which
        is a different statement from "the printer does not have them".

        Every request is optional and every failure is swallowed: a model
        that serves none of them costs three 404s on a slow cadence and
        nothing else.
        """
        results = await asyncio.gather(
            *(
                self._fetch_cdp_optional(path)
                for path in (
                    CDP_LEDM_QUIET_MODE,
                    CDP_LEDM_PANEL,
                    CDP_LEDM_INSTANT_INK,
                )
            )
        )
        quiet_doc, panel_doc, ink_doc = results

        def _flag(document: dict | None, key: str) -> bool | None:
            if not document or key not in document:
                return None
            return str(document[key]).lower() == "true"

        def _string(document: dict | None, key: str) -> str | None:
            """Read a value out of a JSON document.

            Deliberately not ``_text``: that one walks an ElementTree node and
            reaches for ``.tag``, so passing it a decoded document raises
            AttributeError on the first key. The mock-based tests never
            reached this line, because a mock is happy to be anything and only
            a real document from a real printer caught it.
            """
            if not document:
                return None
            value = document.get(key)
            if not isinstance(value, str):
                return None
            return value.strip() or None

        quiet = _flag(quiet_doc, "quietPrintModeEnabled")
        language = _string(panel_doc, "deviceLanguage")
        # An empty string means "not enrolled in the programme", which is a
        # real answer and not the same as the device not having the feature.
        return quiet, language, _string(ink_doc, "supplySubscriptionStatusCode")

    async def _fetch_cdp_optional(self, endpoint: str) -> dict | None:
        """GET one JSON document from a printer that otherwise speaks XML.

        The contract is "never raise". The caller has nothing to do with a
        failure here -- there is no retry worth making for a document the
        model may simply not serve -- so a transport error, a malformed body
        and a client with no session at all all have the same correct answer,
        which is None. Catching narrowly would let the third case escape and
        take the whole update down over a document that was optional in the
        first place.
        """
        if self._session is None:
            return None
        url = f"{self.base_url}{endpoint}"
        try:
            async with self._session.get(
                url, timeout=REQUEST_TIMEOUT, ssl=self._ssl_context
            ) as response:
                if response.status != 200:
                    return None
                body = await response.text()
        except Exception as err:  # noqa: BLE001 - the contract is "never raise"
            _LOGGER.debug("LEDM-side CDP probe %s unavailable: %s", endpoint, err)
            return None
        try:
            document = json.loads(body)
        except ValueError:
            return None
        return document if isinstance(document, dict) else None

    def _parse_network(self, io_doc: Element | None) -> NetworkHealth:
        """Parse the network adaptor's state and error counters.

        A device reports one IOAdaptorConfig per connectivity port -- USB as
        well as network -- and only one of them carries NetworkAdaptorConfig.
        The port type is read from that block rather than from the first
        match in the document, which on every capture seen so far is the USB
        port and would report a networked printer as connected by USB.
        """
        if io_doc is None:
            return NetworkHealth()

        adaptor = None
        port_type = None
        for candidate in io_doc.iter("IOAdaptorConfig"):
            if _find(candidate, "NetworkAdaptorConfig") is not None:
                adaptor = _find(candidate, "NetworkAdaptorConfig")
                port_type = _text(candidate, "DeviceConnectivityPortType")
                break

        stats = _find(io_doc, "NetworkStatistics")

        return NetworkHealth(
            port_type=port_type,
            # NetworkStatus is repeated per IP version; they agree in
            # practice, and the first is IPv4.
            status=_text(adaptor, "NetworkStatus"),
            link_mode=_text(adaptor, "SpeedDuplexNegotiationMode"),
            packets_received=_int(stats, "TotalPacketsReceived"),
            packets_transmitted=_int(stats, "TotalPacketsTransmitted"),
            bad_packets_received=_int(stats, "BadPacketsReceived"),
            framing_errors=_int(stats, "FramingErrorsReceived"),
            transmit_collisions=_int(stats, "TransmitCollisions"),
            transmit_late_collisions=_int(stats, "TransmitLateCollisions"),
            unsendable_packets=_int(stats, "UnsendablePackets"),
        )

    @staticmethod
    def _parse_status(status_doc: Element) -> tuple[str | None, str | None]:
        """Return ``(status, message)`` from a ``ProductStatusDyn`` document.

        The document carries one ``<Status>`` per condition and does not
        order them. On the captured Smart Tank the first entry is a supply
        alert -- ``genuineHP``, "ink tank filled" -- and the device state
        (``inPowerSave``) comes later, so taking the first element reported a
        consumable category as the printer's status. That value is not one the
        status sensor can render, so the entity was then never created at all,
        which presents as "this printer is unsupported" rather than as a bug.

        The device state is therefore the entry whose category the status
        sensor can display; a supply or alert category never can. Falling back
        to the first entry keeps a device that reports something entirely new
        reporting something, rather than reporting nothing.
        """
        if status_doc is None:
            return None, None
        nodes = list(status_doc.iter("Status"))
        if not nodes:
            return None, None
        known = set(STATUS_OPTIONS) | {"idle"}
        node = next(
            (
                candidate
                for candidate in nodes
                if (_text(candidate, "StatusCategory") or "").lower() in known
            ),
            nodes[0],
        )
        loc = node.find("LocString")
        message = loc.text.strip() if loc is not None and loc.text else None
        category = _text(node, "StatusCategory")
        return (category.lower() if category else None), message

    def _parse_subunit(self, usage_doc: Element, subunit: str) -> SubunitUsage:
        """Parse one usage subunit.

        Counter names repeat across subunits, so the search is scoped to the
        subunit element rather than the whole document.
        """
        node = _find(usage_doc, subunit)
        if node is None:
            return SubunitUsage()
        return SubunitUsage(
            total_impressions=_int(node, "TotalImpressions"),
            monochrome_impressions=_int(node, "MonochromeImpressions"),
            color_impressions=_int(node, "ColorImpressions"),
            simplex_sheets=_int(node, "SimplexSheets"),
            duplex_sheets=_int(node, "DuplexSheets"),
            jam_events=_int(node, "JamEvents"),
            mispick_events=_int(node, "MispickEvents"),
            scan_images=_int(node, "ScanImages"),
            adf_images=_int(node, "AdfImages"),
            flatbed_images=_int(node, "FlatbedImages"),
        )

    def _parse_consumables(
        self, consumable_doc: Element, usage_doc: Element
    ) -> dict[str, Consumable]:
        """Merge cartridge config with per-cartridge usage counters.

        ConsumableConfigDyn keys cartridges by ConsumableLabelCode (K/C/M/Y);
        ProductUsageDyn keys the same cartridges by MarkerColor. They are
        joined here so a cartridge is one object rather than two.
        """
        usage_by_code: dict[str, Element] = {}
        subunit = _find(usage_doc, "ConsumableSubunit")
        if subunit is not None:
            for node in subunit.iter("Consumable"):
                marker = _text(node, "MarkerColor")
                if marker is None:
                    continue
                code = next(
                    (c for c, name in COLOR_NAMES.items() if name == marker.lower()),
                    marker[:1].upper(),
                )
                usage_by_code[code] = node

        result: dict[str, Consumable] = {}
        for node in consumable_doc.iter("ConsumableInfo"):
            code = _text(node, "ConsumableLabelCode")
            if code is None:
                continue
            usage = usage_by_code.get(code)

            # ConsumableInfo carries the installed cartridge's fields as direct
            # children AND a PreviousCartridgeData subtree describing the one
            # that was removed. Five names appear in both -- notably
            # SerialNumber and Date -- so the subtree is detached before the
            # installed cartridge is read. Without this, lookups resolve by
            # document order, which happens to be correct on this firmware and
            # would silently report the wrong cartridge if that order changed.
            previous = _find(node, "PreviousCartridgeData")
            if previous is not None:
                node.remove(previous)

            life = _find(node, "ConsumableLifeState")
            result[code] = Consumable(
                label_code=code,
                color_name=COLOR_NAMES.get(code),
                consumable_type=_text(node, "ConsumableTypeEnum"),
                brand=_text(life, "Brand") if life is not None else None,
                state=_text(life, "ConsumableState") if life is not None else None,
                level_percent=_float(node, "ConsumablePercentageLevelRemaining"),
                pages_remaining=_int(usage, "EstimatedPagesRemaining"),
                total_impressions=_int(usage, "TotalImpressions"),
                station=_int(node, "ConsumableStation"),
                serial_number=_text(node, "SerialNumber"),
                part_number=_text(node, "ConsumableSelectibilityNumber"),
                max_capacity=_int(node, "Capacity", "MaxCapacity"),
                installed_at=_date(node, "Installation", "Date"),
                manufactured_at=_date(node, "Manufacturer", "Date"),
                warranty_expires_at=_date(node, "Warranty", "ExpirationDate"),
                counterfeit_refills=_int(
                    usage, "RefilledCount", "CounterfeitRefilledCount"
                ),
                genuine_refills=_int(usage, "RefilledCount", "GenuineRefilledCount"),
                family_name=_text(node, "ConsumableFamilyName"),
                raw_level_percent=_percent(
                    _float(usage, "ConsumableRawPercentageLevelRemaining")
                ),
                low_threshold_percent=_float(node, "ConsumableLowThreshold"),
                measured_state=_text(node, "MeasuredQuantityState"),
                previous_drum_life=_sentinel(_int(previous, "DrumLife")),
                previous_developer_life=_sentinel(_int(previous, "DeveloperLife")),
                previous_engine_toner_remaining=_sentinel(
                    _int(previous, "EngineTonerRemaining")
                ),
                # Note this is NOT how a third-party cartridge is detected:
                # clone chips report the genuine part number. Brand is what
                # exposes that.
                previous_part_number=_text(previous, "ProductNumber"),
                previous_serial_number=_text(previous, "SerialNumber"),
            )
        return result

    def _parse_logs(
        self, logs_doc: Element
    ) -> tuple[list[EventLogEntry], list[JobEntry], str | None]:
        """Parse the device event log and print job log.

        This is the diagnostic record HP's own tooling exposes only through a
        printed report; no other Home Assistant integration surfaces it. Codes
        are dotted families -- 13.x paper jams, 49.x firmware faults, 10.x
        supply-memory errors -- and the accompanying ErrorLog carries assert
        text when firmware has crashed.

        Both logs contain TotalImpressions, so each is parsed strictly within
        its own container to avoid cross-contamination.
        """
        events: list[EventLogEntry] = []
        event_log = _find(logs_doc, "EventLog")
        if event_log is not None:
            events.extend(
                EventLogEntry(
                    sequence=_int(node, "SequenceNumber"),
                    code=_text(node, "EventCode"),
                    impressions=_int(node, "TotalImpressions"),
                )
                for node in event_log.iter("Event")
            )
        events.sort(
            key=lambda e: e.sequence if e.sequence is not None else -1, reverse=True
        )

        jobs: list[JobEntry] = []
        job_list = _find(logs_doc, "JobList")
        if job_list is not None:
            jobs.extend(
                JobEntry(
                    application_id=_text(node, "DriverJobApplicationID"),
                    user_id=_text(node, "DriverJobUserID"),
                    name=_text(node, "DriverJobName"),
                    monochrome_impressions=_int(node, "MonochromeImpressions"),
                    color_impressions=_int(node, "ColorImpressions"),
                    total_impressions=_int(node, "TotalImpressions"),
                )
                for node in job_list.iter("JobEntry")
            )

        assert_text = _text(logs_doc, "ErrorLog")
        return events, jobs, assert_text

    async def async_validate(self) -> ProductInfo:
        """Confirm the host speaks LEDM and return its identity."""
        info = await self.async_get_product_info()
        if not info.serial_number:
            raise HPPrinterParseError("Device did not report a serial number")
        return info


def _parse_extra_counters(usage_doc: Element) -> dict[str, Any]:
    """Return the usage counters that appear exactly once in the document.

    Returned as a ``**kwargs`` fragment so ``async_get_data`` stays one
    readable call. Every value here is a single occurrence in the document;
    the per-media-type counters are handled by
    :func:`_parse_quality_by_media` instead, because reading the first
    occurrence of those would report one medium's share as if it were a
    total.
    """
    return {
        "panel_button_presses": _int(
            usage_doc, "UIButtonPressCounters", "ButtonPressCount"
        ),
        "scan_to_host_images": _int(usage_doc, "ScanToHostImages"),
        "photo_quality_pages": _int(usage_doc, "PhotoImpressions"),
        # The cartridge's own tamper flag. A device reports it once per
        # consumable slot; a non-zero value anywhere is the signal, so the
        # highest is what survives.
        "non_hp_flag_count": _max_int(usage_doc, "NonHPFlagCounter"),
    }


def _max_int(usage_doc: Element, name: str) -> int | None:
    """Return the largest value a repeated counter reports, or None.

    Several of these counters are emitted once per consumable slot. Taking
    the first, or the last, would depend on document order; taking the
    maximum answers the question the entity actually asks -- has this
    happened at all.
    """
    values = [
        int(node.text.strip())
        for node in usage_doc.iter(name)
        if node.text and node.text.strip().lstrip("-").isdigit()
    ]
    return max(values) if values else None


def _parse_quality_by_media(usage_doc: Element) -> dict[str, Any]:
    """Sum the per-media-type quality counters.

    ``UsageByQuality`` repeats Normal/Draft/Better once per media type, so a
    single occurrence is only that medium's share. Reading the first one
    produces a "normal quality pages" figure that is really just "pages
    printed on plain paper" -- which is why this sums instead.

    A sum of zero is returned as 0 rather than None: the document listed the
    block, so the device did report the counters and they are genuinely zero.
    """
    totals = {"normal": 0, "better": 0, "draft": 0}
    seen = False
    for block in usage_doc.iter("UsageByQuality"):
        seen = True
        for key, tag in (
            ("normal", "NormalImpressions"),
            ("better", "BetterImpressions"),
            ("draft", "DraftImpressions"),
        ):
            value = _int(block, tag)
            if value is not None:
                totals[key] += value
    if not seen:
        return {
            "normal_quality_pages": None,
            "better_quality_pages": None,
            "draft_quality_pages": None,
        }
    return {
        "normal_quality_pages": totals["normal"],
        "better_quality_pages": totals["better"],
        "draft_quality_pages": totals["draft"],
    }


def _parse_marking_agent(usage_doc: Element) -> dict[str, Any]:
    """Return the ink-draw counters from ``ProductUsageDyn``.

    Split out as a ``**kwargs`` fragment so ``async_get_data`` stays a single
    readable call. The two values answer a question nothing else can: the
    captured Smart Tank reports 1208 ml drawn against 0 ml ever shipped in a
    cartridge, which is the arithmetic behind "these pages came from bottled
    ink".
    """
    return {
        "marking_agent_used_ml": _float(
            usage_doc, "CumulativeMarkingAgentUsed", "ValueFloat"
        ),
        "marking_agent_inserted_ml": _float(
            usage_doc, "CumulativeHPMarkingAgentInserted", "ValueFloat"
        ),
        "panel_cancel_presses": _int(usage_doc, "TotalFrontPanelCancelPresses"),
    }


def _parse_media_handling(media_doc: Element | None) -> dict[str, Any]:
    """Return paper presence and the tray list from ``MediaHandlingDyn``.

    Presence is the only paper signal this document carries -- no level, no
    capacity -- which is why the percentage has to come from IPP.

    ``MediaState`` is read per tray rather than from the document root: a
    multifunction device reports an automatic document feeder alongside the
    main tray, and an ADF that is empty is normal rather than a paper-out.
    """
    if media_doc is None:
        return {"paper_present": None, "input_trays": ()}

    trays: list[tuple[str, str | None]] = []
    for tray in media_doc.iter("InputTray"):
        name = _text(tray, "InputBin")
        if name is None:
            continue
        trays.append((name, _text(tray, "MediaState")))

    if not trays:
        return {"paper_present": None, "input_trays": ()}

    main_states = [
        state
        for name, state in trays
        if "adf" not in name.lower() and "document" not in name.lower()
    ]
    states = main_states or [state for _, state in trays if state is not None]
    present = any(state.strip().lower() == "present" for state in states)
    return {
        "paper_present": present if states else None,
        "input_trays": tuple(name for name, _ in trays),
    }


def as_diagnostics(data: Any) -> Any:
    """Best-effort conversion of dataclasses to plain types for diagnostics."""
    if hasattr(data, "__dataclass_fields__"):
        return {
            name: as_diagnostics(getattr(data, name))
            for name in data.__dataclass_fields__
        }
    if isinstance(data, dict):
        return {key: as_diagnostics(value) for key, value in data.items()}
    if isinstance(data, list):
        return [as_diagnostics(item) for item in data]
    if isinstance(data, datetime):
        return data.isoformat()
    return data


def _parse_ledm_alerts(status_doc: Element | None) -> list[ActiveAlert]:
    """Build the alerts the device is raising right now.

    LEDM keeps them in an ``AlertTable``, one ``Alert`` per entry, and the
    vocabulary is its own: ``Info`` where CDP says ``information``. The case is
    folded so the two protocols present the same state under the same name,
    and a word this function has not seen before is passed through lowercased
    rather than dropped -- an alert whose severity is unknown is still an
    alert, and hiding it would make the count wrong.
    """
    if status_doc is None:
        return []
    table = _find(status_doc, "AlertTable")
    if table is None:
        return []
    alerts: list[ActiveAlert] = []
    for entry in table.iter("Alert"):
        severity = _text(entry, "Severity")
        category = _text(entry, "ProductStatusAlertID")
        if severity is None and category is None:
            continue
        alerts.append(
            ActiveAlert(
                category=category,
                severity=severity.strip().lower() if severity else None,
                priority=_int(entry, "AlertPriority"),
                sequence=_int(entry, "SequenceNumber"),
            )
        )
    return alerts


def _parse_ledm_jobs(usage_doc: Element | None) -> dict[str, Any]:
    """Return the job counters from the printer subunit.

    Read from ``PrinterSubunit`` rather than the document root: the usage
    document repeats the same counter names under every subunit, and the
    scanner's ``JobCount`` is a different thing from the printer's.
    """
    if usage_doc is None:
        return {}
    subunit = _find(usage_doc, "PrinterSubunit")
    if subunit is None:
        return {}
    return {
        "print_job_count": _int(subunit, "JobCount"),
        "job_successes": _int(subunit, "SuccessCount"),
        "job_failures": _int(subunit, "FailureCount"),
        "job_cancelled": _int(subunit, "CancelledCount"),
        "job_skipped": _int(subunit, "SkippedCount"),
        "network_printed_pages": _int(subunit, "NetworkImpressions"),
        "wireless_printed_pages": _int(subunit, "WirelessNetworkImpressions"),
        "subscription_printed_pages": _int(subunit, "SubscriptionImpressions"),
        "ews_access_count": _int(subunit, "EWSAccessCount"),
    }


def _parse_product_config(config_doc: Element | None) -> dict[str, Any]:
    """Return the hardware, identity and exposure facts from the product config.

    Every field here is read from the subtree its capability document points
    at, which is not always where the name suggests. ``Sides`` is the
    instructive one: it looks like a printing setting and is in fact a
    descriptor of a memory module, and the Cap document is the only thing that
    says so. ``Duplex`` is the other: it reads "disabled" on a machine with an
    installed duplexer that has printed ten thousand double-sided sheets,
    because it is the auto-duplex *setting* while ``DuplexUnit`` is the
    hardware. Surfacing either without reading both would be wrong.

    These live on ProductInfo rather than PrinterData because the document is
    already read there on the slow cadence, and none of them changes between
    polls.
    """
    if config_doc is None:
        return {
            "available_memory_kb": None,
            "total_memory_kb": None,
            "country_region": None,
            "device_language": None,
            "product_derivative_number": None,
            "setup_phase": None,
            "duplexer_installed": None,
            "auto_duplex_enabled": None,
            "failed_attempts_remaining": None,
        }
    information = _find(config_doc, "ProductInformation")
    settings = _find(config_doc, "ProductSettings")
    memory = _find(config_doc, "Memory")
    # Failed sign-in attempts live under a RegionInformation block inside
    # ProductInformation, not beside the product identity fields. Reading them
    # from ProductInformation directly returns None silently, which is how
    # three of these fields came back empty on the first run.
    region = (
        _find(information, "RegionInformation") if information is not None else None
    )
    # And the device's own language is one level down again.
    language = _find(settings, "ProductLanguage") if settings is not None else None

    # The out-of-box setup phase lives here, not in the status document. It
    # says setupComplete on the machine measured, which is why its printhead
    # alignment is not the pending-step failure the CDP model reports.
    phase = _text(config_doc, "OobePhase")
    normalized = phase.strip().lower() if phase is not None else ""
    setup_phase = None
    if phase is not None:
        if "complete" in normalized:
            setup_phase = "complete"
        elif "inprogress" in normalized or "in_progress" in normalized:
            setup_phase = "inProgress"
        elif "pending" in normalized or "action" in normalized:
            setup_phase = "actionPending"
        else:
            setup_phase = "idle"

    duplexer = _text(information, "DuplexUnit")
    return {
        "available_memory_kb": _int(memory, "AvailableMemory")
        if memory is not None
        else None,
        "total_memory_kb": _int(memory, "TotalMemory") if memory is not None else None,
        "country_region": _text(settings, "CountryAndRegionName"),
        "device_language": _text(language, "DeviceLanguage"),
        "product_derivative_number": _text(information, "ProductDerivativeNumber"),
        "setup_phase": setup_phase,
        # "Installed" is the word the device uses for fitted hardware; anything
        # else, including an absent field, is not an installed duplexer.
        "duplexer_installed": (
            duplexer.strip().lower() == "installed" if duplexer is not None else None
        ),
        "auto_duplex_enabled": _enabled(_text(settings, "Duplex")),
        "failed_attempts_remaining": _int(region, "FailedAttemptsRemaining"),
    }


def _parse_ledm_trays(media_doc: Element | None) -> dict[str, Any]:
    """Return the tray and bin counts, and which ones are the defaults.

    The per-tray list is already exposed by ``_parse_media_handling``; what
    was missing is the shape -- how many trays and bins the machine has, which
    is what tells you a single-tray model from a two-tray one without reading
    the list.
    """
    if media_doc is None:
        return {
            "input_tray_count": None,
            "output_bin_count": None,
            "default_input_tray": None,
            "default_output_bin": None,
        }
    return {
        "input_tray_count": _int(media_doc, "NumOfInputTrays"),
        "output_bin_count": _int(media_doc, "NumOfOutputBins"),
        "default_input_tray": _text(media_doc, "DefaultInputTray"),
        "default_output_bin": _text(media_doc, "DefaultOutputBin"),
    }


def _parse_ledm_exposure(net_doc: Element | None) -> dict[str, Any]:
    """Return which network services are switched on, as flags.

    The LEDM spelling of what the CDP side calls print services, and the
    answer on the machine measured is the same one: raw printing on port 9100
    is on, and HTTPS redirection is off, so the web interface answers plain
    HTTP. Both are things a user can only find by opening the printer's own
    settings page.

    ``WebScan`` is read from the document root and not from
    ``WebServicesConfig``: the two exist side by side and disagree, with
    ``WSScan`` reading "enabled" while ``WebScan`` reads "disabled" on the same
    printer. The second is the setting a user would go and change.
    """
    if net_doc is None:
        return {
            "port_9100_enabled": None,
            "direct_print_enabled": None,
            "https_redirection_enabled": None,
            "web_scan_enabled": None,
            "llmnr_enabled": None,
        }
    return {
        "port_9100_enabled": _enabled(_text(net_doc, "Port9100PrintingSupport")),
        "direct_print_enabled": _enabled(_text(net_doc, "DirectPrint")),
        "https_redirection_enabled": _enabled(_text(net_doc, "HTTPSRedirection")),
        "web_scan_enabled": _enabled(_text(net_doc, "WebScan")),
        "llmnr_enabled": _enabled(_text(net_doc, "LLMNR")),
    }


def _parse_print_configuration(config_doc: Element | None) -> dict[str, Any]:
    """Return the print settings the device is configured with.

    These are *settings*, not measurements: ``PrintQuality`` says what the
    machine is set to, not what came out of it. Exposed because "why is my
    output worse than it used to be" is often a resolution someone changed,
    and nothing else here would show it.
    """
    if config_doc is None:
        return {
            "print_quality": None,
            "resolution_setting": None,
            "default_copies": None,
            "default_orientation": None,
            "borderless_printing": None,
        }
    return {
        "print_quality": _text(config_doc, "PrintQuality"),
        "resolution_setting": _text(config_doc, "ResolutionSetting"),
        "default_copies": _int(config_doc, "DefaultPrintCopies"),
        # Portrait or Landscape: the orientation a job gets when the driver
        # says nothing. A settings value, not a measurement of what came out,
        # and it lives here rather than in the product config.
        "default_orientation": _text(config_doc, "DefaultPDLInterpreterOrientation"),
        "borderless_printing": _enabled(_text(config_doc, "BorderlessPrinting")),
    }


def _parse_current_media(media_doc: Element | None) -> dict[str, Any]:
    """Return the media the printer is currently set to.

    The name is kept verbatim (``iso_a4_210x297mm``) rather than mapped to
    something friendlier: the device's own vocabulary is the only one that
    matches what the printer's own web page and the paper loaded in the tray
    will call it, and a guessed label would drift from both.
    """
    if media_doc is None:
        return {"current_media_type": None, "current_media_size": None}
    return {
        "current_media_type": _text(media_doc, "MediaType"),
        "current_media_size": _text(media_doc, "MediaSizeName"),
    }


def _parse_network_services(net_doc: Element | None) -> dict[str, Any]:
    """Return which discovery and name-resolution services are switched on.

    Kept because each one is a way for something else on the network to find
    the printer: mDNS advertises it continuously, WS-Discovery answers on
    demand, and both are on by default on the model measured. The SNMP flags
    come from the same document and are the more serious of the two, because
    the community string is the device's own default and is not a secret.
    """
    if net_doc is None:
        return {
            "snmp_enabled": None,
            "snmp_public_allowed": None,
            "print_services": (),
        }
    snmp = _find(net_doc, "SNMPConfigWithVersion")
    web = _find(net_doc, "WebServicesConfig")
    dns_sd = _find(net_doc, "DNSSDConfig")

    enabled: list[str] = []
    if dns_sd is not None and _enabled(_text(dns_sd, "MDNSSupport")):
        enabled.append("mdns")
    if web is not None:
        for tag, name in (
            ("WSDiscovery", "ws-discovery"),
            ("WSPrint", "ws-print"),
            ("WSScan", "ws-scan"),
        ):
            if _enabled(_text(web, tag)):
                enabled.append(name)

    return {
        "snmp_enabled": _enabled(_text(snmp, "SNMP")) if snmp is not None else None,
        # Not a boolean and not read through _enabled: the device's own
        # NetAppsCap declares exactly two legal values, ``publicAllowed`` and
        # ``publicNotAllowed``. Neither contains "enabled", so a generic
        # on/off test reports a printer that permits the default community
        # string as having it switched off -- which is the opposite of the
        # exposure worth warning about.
        "snmp_public_allowed": _snmp_public_allowed(
            _text(snmp, "GetCommunityNameConfig")
        )
        if snmp is not None
        else None,
        "print_services": tuple(sorted(enabled)),
    }


def _snmp_public_allowed(value: str | None) -> bool | None:
    """Return whether the device accepts the built-in public community.

    The enumeration is the device's own, read from NetAppsCap rather than
    guessed: ``publicAllowed`` and ``publicNotAllowed`` are the only two
    values declared. Anything else is reported as unknown rather than
    defaulted to False, because defaulting a security field to the safe-looking
    answer is the one way this can be quietly wrong.
    """
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized == "publicallowed":
        return True
    if normalized == "publicnotallowed":
        return False
    return None


def _parse_instant_ink(
    supplies_doc: Element | None, status_code: str | None
) -> dict[str, Any]:
    """Return the instant-ink status and the full model string.

    Two documents, because neither is complete alone. The CDP document
    carries the enrolment status that LEDM does not have at all -- an empty
    string there means "never enrolled", which is an answer. The LEDM
    supplies document carries a model string that appends the SKU and region
    code to the model name, which the identity document does not include and
    which is what distinguishes two otherwise identical-looking machines.

    The two are kept apart on purpose. Folding the model string in as a
    fallback for the status would report "Smart Tank 750 series:28B72A:0" as
    an enrolment state, which is a category error rather than a small
    imprecision.
    """
    model_string = None
    if supplies_doc is not None:
        model_string = _text(supplies_doc, "GloballyUniqueDeviceModelID")
    return {
        "instant_ink_status": status_code or None,
        "full_model_string": model_string,
    }
