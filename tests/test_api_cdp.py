"""Replay a real anonymized CDP capture against the CDP parser.

Mirrors ``test_api_fixtures.py`` for the JSON interface. The fixture
directory holds a capture from a model that answers 404 to every LEDM path
and serves these documents instead -- which is the case this client exists
for, so the fixture doubles as the record of that.

The fixture is optional: a checkout without it still passes.

To add one:

    ./.venv/bin/python scripts/capture_cdp.py --host <your-printer> --https
    ./.venv/bin/python scripts/anonymize_cdp.py scripts/captures/<dir>/
    cp -r scripts/captures/<dir>-anon tests/fixtures/<short-name>/
"""

import json
from pathlib import Path

from custom_components.hp_printers.api_cdp import CDPClient, _status
from custom_components.hp_printers.const import (
    CDP_DEVICE_SERVICE_COUNTERS,
    CDP_DEVICE_USAGE,
    CDP_EVENTS,
    CDP_IDENTITY,
    CDP_PRINT_STATUS,
    CDP_SCAN_STATUS,
    CDP_SUPPLIES,
    CDP_SYSTEM_STATISTICS,
)

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
CDP_MODEL_DIR = FIXTURES_DIR / "st580_590"


def _load(name: str) -> dict | None:
    """Return a captured document, or ``None`` when the fixture is absent."""
    path = CDP_MODEL_DIR / name
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _have_fixtures() -> bool:
    return CDP_MODEL_DIR.is_dir()


def test_cdp_fixture_presence_is_explicit() -> None:
    """Guard the skip below: say so when there is nothing to replay."""
    if not _have_fixtures():
        return
    assert _load("cdm_system_v1_identity.json") is not None


def test_identity_parses_and_is_anonymized() -> None:
    """Identity yields a serial the config flow can key an entry on."""
    document = _load("cdm_system_v1_identity.json")
    if document is None:
        return

    info = CDPClient._parse_product_info(document)  # noqa: SLF001

    # The integration refuses to set up without a serial, so the anonymizer
    # must have replaced it rather than dropped it.
    assert info.serial_number
    assert info.make_and_model
    assert info.uuid
    # The install date is the field LEDM's ProductConfigDyn does not carry for
    # this model, and the reason the CDP client exists at all.
    assert info.installed_at is not None
    assert info.installed_at.year > 1976


def test_printed_pages_is_the_split_not_the_engine_total() -> None:
    """The exposed total is monochrome + color, never the engine lifetime count.

    This is the distinction that is easy to get wrong and expensive to get
    wrong: the engine total counts every page the engine ever handled,
    including jams, retries, copies, scans and calibration, and HP's own usage
    page states it is never reset.
    """
    document = _load("cdm_deviceUsage_v1_lifetimeCounters.json")
    if document is None:
        return

    service = _load("cdm_deviceUsage_v1_serviceCounters.json") or {}
    printer = CDPClient._parse_printer_usage(document, service)  # noqa: SLF001

    raw = document["printUsage"]["impressions"]
    assert printer.total_impressions == raw["monochrome"] + raw["color"]
    if raw["total"] != printer.total_impressions:
        # Recorded rather than asserted: on the captured model these differ,
        # and that difference is exactly why the engine total is not exposed.
        assert raw["total"] > printer.total_impressions


def test_supplies_separate_printheads_from_ink_tanks() -> None:
    """A slot typed ``inkCartridge`` on this model is a printhead, not ink.

    The two are presented by the device in the same list and the only way to
    tell them apart is the slot's own ``supplyType`` plus the absence of a
    level on the printhead. Conflating them is what makes a printhead's
    remaining-life percentage get displayed as ink left in a tank.
    """
    document = _load("cdm_supply_v1_suppliesPublic.json")
    if document is None:
        return

    consumables = CDPClient._parse_consumables(document)  # noqa: SLF001

    assert consumables, "capture should contain at least one slot"
    printheads = [
        c for c in consumables.values() if c.consumable_type == "inkCartridge"
    ]
    tanks = [c for c in consumables.values() if c.consumable_type == "inkTank"]

    assert printheads, "expected the printhead slots this client was written for"
    assert tanks, "expected the refillable tanks"
    for head in printheads:
        assert head.is_genuine_reported is not None
        assert head.serial_number
    for tank in tanks:
        assert tank.level_percent is not None


def test_events_carry_severity() -> None:
    """The CDP event log reports a severity the LEDM one does not."""
    document = _load("cdm_diagnostic_v1_systemEvents.json")
    if document is None:
        return

    events = CDPClient._parse_events(document)  # noqa: SLF001

    assert events
    assert all(event.code for event in events)
    assert any(event.severity for event in events)


def test_status_is_folded_onto_the_integration_option_list() -> None:
    """A CDP status word maps onto a value the status sensor accepts.

    The CDP print service says ``idle`` where the EWS page says ``ready``,
    and the scan service capitalises ``Idle``. An unmapped value would make
    the status sensor read ``None`` on every poll.
    """
    document = _load("cdm_print_v2_status.json")
    if document is None:
        return

    assert _status(document["printerState"]) is not None
    assert _status("Idle") == "idle"
    assert _status("ready") == "ready"
    assert _status(None) is None


def test_optional_endpoints_absent_do_not_raise() -> None:
    """A document the model does not serve parses to nothing, not an error."""
    assert CDPClient._parse_events(None) == []  # noqa: SLF001
    assert CDPClient._parse_events({}) == []  # noqa: SLF001
    assert CDPClient._parse_events({"events": {}}) == []  # noqa: SLF001


def test_negative_counters_are_dropped() -> None:
    """A firmware counter can report a negative value; it is not a count.

    The captured model reports ``scanUsage.sendImages`` as -16, which is a
    firmware fault rather than a reading. Surfacing it as a sensor value
    would look like a real number, so it is discarded at the parse boundary.
    """
    usage = {"printUsage": {}, "scanUsage": {"totalImages": -16}}
    scanner = CDPClient._parse_scan_usage(usage)  # noqa: SLF001
    assert scanner.scan_images is None


def test_partial_impressions_yield_no_total() -> None:
    """A half-reported split must not be added up into a plausible total."""
    service = {"hardwareEvents": {}}
    usage = {"printUsage": {"impressions": {"monochrome": 100}}}
    printer = CDPClient._parse_printer_usage(usage, service)  # noqa: SLF001
    assert printer.total_impressions is None
    assert printer.monochrome_impressions == 100


def test_scanner_counters_come_from_scan_usage() -> None:
    """The scan service's status document carries no counts.

    ``/cdm/scan/v1/status`` has ``scannerError`` and ``scannerState`` and
    nothing else, so reading totals out of it yields nothing -- which is why
    the counts come from ``scanUsage`` instead.
    """
    scan_status = _load("cdm_scan_v1_status.json")
    if scan_status is None:
        return
    assert "totalImages" not in scan_status

    usage = _load("cdm_deviceUsage_v1_lifetimeCounters.json")
    if usage is None:
        return
    scanner = CDPClient._parse_scan_usage(usage)  # noqa: SLF001
    assert scanner.scan_images is not None
    assert scanner.flatbed_images is not None


def test_endpoint_constants_match_the_capture() -> None:
    """Every endpoint the client reads exists in the capture.

    A wrong version segment returns 404 and is indistinguishable from a model
    that does not serve the feature, so a typo here would read as absence.
    """
    if not _have_fixtures():
        return
    for endpoint in (
        CDP_IDENTITY,
        CDP_SYSTEM_STATISTICS,
        CDP_DEVICE_USAGE,
        CDP_DEVICE_SERVICE_COUNTERS,
        CDP_SUPPLIES,
        CDP_PRINT_STATUS,
        CDP_SCAN_STATUS,
        CDP_EVENTS,
    ):
        name = endpoint.lstrip("/").replace("/", "_") + ".json"
        assert (CDP_MODEL_DIR / name).exists(), f"{endpoint} missing from capture"
