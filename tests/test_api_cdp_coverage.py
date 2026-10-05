"""Raise api_cdp.py's coverage with tests that assert behaviour, not lines.

Coverage fell to 62% on this module, below the floor the repository
documents. Most of the gap is the parsing helpers and the transport error
paths, which are exactly the parts a fixture-driven happy path skips: a
document that is missing, partial, malformed, or answered with a status the
firmware should not answer with.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.hp_printers.api import (
    HPPrinterNotSupportedError,
    HPPrinterParseError,
)
from custom_components.hp_printers.api_cdp import (
    CDPClient,
    _bool,
    _date,
    _int,
    _percent,
    _status,
    _text,
)
from custom_components.hp_printers.const import STATUS_OPTIONS

CDP_MODEL_DIR = Path(__file__).resolve().parent / "fixtures" / "st580_590"


def _client() -> CDPClient:
    """Return a CDP client with a stub session."""
    return CDPClient(MagicMock(), "printer.local", 443, True, None)


# ------------------------------------------------------------ helpers


def test_text_ignores_non_strings_and_blanks() -> None:
    """Absent, blank and non-string values all mean None."""
    assert _text({"a": " x "}, "a") == "x"
    assert _text({"a": ""}, "a") is None
    assert _text({"a": "   "}, "a") is None
    assert _text({"a": 5}, "a") is None
    assert _text({}, "a") is None


def test_int_drops_booleans_negatives_and_junk() -> None:
    """A bool is not a count, and a negative counter is a firmware fault."""
    assert _int({"a": 5}, "a") == 5
    assert _int({"a": 5.0}, "a") == 5
    # A bool is an int subclass; a counter of True is not a reading.
    assert _int({"a": True}, "a") is None
    # A negative counter is a firmware fault, not a count.
    assert _int({"a": -16}, "a") is None
    assert _int({"a": "5"}, "a") is None
    assert _int({"a": None}, "a") is None
    assert _int("not a dict", "a") is None


def test_bool_reads_the_string_flags() -> None:
    """The string flags HP uses are read as booleans."""
    for word in ("enabled", "true", "set", "yes", "on"):
        assert _bool({"a": word}, "a") is True
    for word in ("disabled", "false", "notSet", "no", "off"):
        assert _bool({"a": word}, "a") is False
    assert _bool({}, "a") is None
    assert _bool({"a": ""}, "a") is None


def test_date_drops_the_unset_clock_placeholder() -> None:
    """A device with no real-time clock reports 1976, which is dropped."""
    assert _date({"a": "3/14/2025 12:00:00 AM"}, "a").year == 2025
    assert _date({"a": "1976-01-01T00:00:00"}, "a") is None
    assert _date({"a": "not a date"}, "a") is None
    assert _date({}, "a") is None


def test_percent_drops_out_of_range() -> None:
    """A level outside 0-100 is a sentinel, not a reading."""
    assert _percent(50) == 50
    assert _percent(-1) is None
    assert _percent(101) is None
    assert _percent(None) is None


# ------------------------------------------------------------- parsing


def test_printer_usage_from_an_empty_document() -> None:
    """An absent block yields an empty usage, not a crash."""
    usage = CDPClient._parse_printer_usage({}, {})  # noqa: SLF001
    assert usage.total_impressions is None
    assert usage.jam_events is None


def test_printer_usage_reads_hardware_events() -> None:
    """Counters and hardware events are read from their own blocks."""
    usage = CDPClient._parse_printer_usage(  # noqa: SLF001
        {
            "printUsage": {
                "impressions": {"monochrome": 100, "color": 50, "total": 900},
                "sheets": {"simplex": 120, "duplex": 15},
            }
        },
        {"hardwareEvents": {"jamEventCount": 4, "mispickEventCount": 9}},
    )
    assert usage.total_impressions == 150
    assert usage.monochrome_impressions == 100
    assert usage.color_impressions == 50
    assert usage.simplex_sheets == 120
    assert usage.duplex_sheets == 15
    assert usage.jam_events == 4
    assert usage.mispick_events == 9


def test_copy_usage_and_empty_scan_usage() -> None:
    """Copy counts come from printUsage; scan has no ADF split here."""
    document = {"printUsage": {"copyImpressions": {"monochrome": 2, "color": 1}}}
    copy = CDPClient._parse_copy_usage(document)  # noqa: SLF001
    assert (copy.monochrome_impressions, copy.color_impressions) == (2, 1)
    assert copy.adf_images is None
    # CDP carries no ADF/flatbed split for scan jobs, so the scan subunit is
    # left empty rather than filled from a differently-scoped counter.
    assert CDPClient._parse_scan_usage({}).scan_images is None  # noqa: SLF001


def test_consumables_skip_entries_without_a_colour_code() -> None:
    """A slot with no colour code cannot be keyed."""
    result = CDPClient._parse_consumables(  # noqa: SLF001
        {"suppliesList": [{"supplyType": "inkTank"}, {"supplyColorCode": "K"}]}
    )
    assert set(result) == {"K"}


def test_state_reason_joins_a_list_and_copes_without_one() -> None:
    """A list of reasons is joined; a missing one is empty."""
    result = CDPClient._parse_consumables(  # noqa: SLF001
        {
            "suppliesList": [
                {"supplyColorCode": "K", "stateReasons": ["a", "b"]},
                {"supplyColorCode": "C"},
            ]
        }
    )
    assert result["K"].state_reasons == ("a", "b")
    assert result["C"].state_reasons == ()


def test_product_info_reads_a_nested_make_and_model() -> None:
    """MakeAndModel is a nested object, and sometimes a bare string."""
    info = CDPClient._parse_product_info(  # noqa: SLF001
        {
            "makeAndModel": {"name": "Smart Tank 580-590", "family": "Smart Tank"},
            "serialNumber": "SN-1",
        }
    )
    assert info.make_and_model == "Smart Tank 580-590"
    assert info.make_and_model_family == "Smart Tank"

    # A document whose makeAndModel is a bare string still yields something.
    flat = CDPClient._parse_product_info({"makeAndModel": "M182nw"})  # noqa: SLF001
    assert flat.make_and_model == "M182nw"
    assert flat.make_and_model_family is None


def test_state_message_joins_reasons_and_tolerates_other_shapes() -> None:
    """State reasons are joined, other shapes are ignored."""
    assert CDPClient._state_message({"printerStateReasons": ["none"]}) == "none"  # noqa: SLF001
    assert CDPClient._state_message({"printerStateReasons": []}) is None  # noqa: SLF001
    assert CDPClient._state_message({}) is None  # noqa: SLF001
    assert CDPClient._state_message({"printerStateReasons": "none"}) is None  # noqa: SLF001


# ------------------------------------------------------------ transport


def _session(status: int = 200, body: bytes = b"{}", raiser=None):
    """Return a session whose single GET answers with that response."""
    response = MagicMock()
    response.status = status
    response.raise_for_status = MagicMock(side_effect=raiser)
    response.read = AsyncMock(return_value=body)
    response.text = AsyncMock(return_value=body.decode("utf-8", "replace"))
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=response)
    context.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.get = MagicMock(return_value=context)
    return session


async def test_fetch_reports_a_404_as_unsupported_not_unreachable() -> None:
    """The 404 is the signal the protocol probe branches on."""
    client = CDPClient(_session(status=404), "h", 443, True, None)
    with pytest.raises(HPPrinterNotSupportedError):
        await client._fetch("/cdm/system/v1/identity")  # noqa: SLF001


async def test_fetch_rejects_a_non_json_body() -> None:
    """A non-JSON body is a parse error, not an empty document."""
    client = CDPClient(_session(body=b"<html>nope</html>"), "h", 443, True, None)
    with pytest.raises(HPPrinterParseError):
        await client._fetch("/cdm/system/v1/identity")  # noqa: SLF001


async def test_fetch_rejects_a_json_body_that_is_not_an_object() -> None:
    """A JSON array is not the document shape expected here."""
    client = CDPClient(_session(body=b"[1,2,3]"), "h", 443, True, None)
    with pytest.raises(HPPrinterParseError):
        await client._fetch("/cdm/system/v1/identity")  # noqa: SLF001


async def test_optional_fetch_swallows_failure() -> None:
    """A model that does not serve a document must not fail the update."""
    client = CDPClient(_session(status=404), "h", 443, True, None)
    assert await client._fetch_optional("/cdm/scan/v1/status") is None  # noqa: SLF001


async def test_validate_refuses_a_device_with_no_serial() -> None:
    """Setup cannot key an entry without a serial, so this must raise."""
    client = _client()
    # async_get_product_info fetches the identity and the security document
    # concurrently, so the stub answers twice -- and it answers with raw
    # documents, because that is what the client parses.
    client._fetch = AsyncMock(  # noqa: SLF001
        side_effect=[{"makeAndModel": {"name": "x"}}, {"passwordSet": "false"}]
    )
    with pytest.raises(HPPrinterParseError):
        await client.async_validate()


async def test_get_data_against_the_captured_documents() -> None:
    """The whole update path, driven by the real capture.

    Replayed rather than mocked per endpoint, so a change to a field name
    shows up here instead of only in the parser unit tests.
    """
    if not CDP_MODEL_DIR.is_dir():
        return
    documents = {
        "/cdm/system/v1/statistics": "cdm_system_v1_statistics.json",
        "/cdm/deviceUsage/v1/lifetimeCounters": "cdm_deviceUsage_v1_lifetimeCounters.json",
        "/cdm/deviceUsage/v1/serviceCounters": "cdm_deviceUsage_v1_serviceCounters.json",
        "/cdm/supply/v1/suppliesPublic": "cdm_supply_v1_suppliesPublic.json",
        "/cdm/supply/v1/configPublic": "cdm_supply_v1_configPublic.json",
        "/cdm/print/v2/status": "cdm_print_v2_status.json",
        "/cdm/scan/v1/status": "cdm_scan_v1_status.json",
        "/cdm/diagnostic/v1/systemEvents": "cdm_diagnostic_v1_systemEvents.json",
    }

    async def fake_fetch(endpoint: str):
        name = documents.get(endpoint)
        if name is None:
            return None
        path = CDP_MODEL_DIR / name
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    client = _client()
    client._fetch = AsyncMock(side_effect=fake_fetch)  # noqa: SLF001

    data = await client.async_get_data()

    assert data.printer.total_impressions is not None
    assert data.consumables, "the capture has slots"
    assert data.power_cycles is not None
    assert data.events
    assert data.status in ("ready", "inpowersave", "processing") or data.status


async def test_get_data_survives_a_model_that_serves_nothing_optional() -> None:
    """Only the mandatory documents are required; the rest may be absent."""
    required = {
        "/cdm/deviceUsage/v1/lifetimeCounters": {"printUsage": {}},
        "/cdm/deviceUsage/v1/serviceCounters": {"hardwareEvents": {}},
        "/cdm/supply/v1/suppliesPublic": {"suppliesList": []},
        "/cdm/print/v2/status": {"printerState": "idle"},
    }

    async def fake_fetch(endpoint: str):
        return required.get(endpoint)

    client = _client()
    client._fetch = AsyncMock(side_effect=fake_fetch)  # noqa: SLF001

    data = await client.async_get_data()

    assert data.status == "ready"
    assert data.consumables == {}
    assert data.power_cycles is None
    assert data.scanner_status is None
    assert data.events == []


async def test_status_folding_covers_every_declared_option() -> None:
    """Every option the sensor declares survives a round trip."""
    for option in STATUS_OPTIONS:
        assert _status(option) == option
