"""Tests for the minimal IPP client and the protocol probe.

The IPP client exists for one attribute, so these tests are narrow on
purpose: build the request, decode a response, split the tray bag, and
refuse to invent a level. A general IPP library would be the right answer
for anything beyond this.
"""

import struct
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import ClientResponseError
import pytest

from custom_components.hp_printers.api import (
    HPPrinterConnectionError,
    HPPrinterNotSupportedError,
    HPPrinterParseError,
)
from custom_components.hp_printers.api_ipp import (
    OP_GET_PRINTER_ATTRIBUTES,
    IPPClient,
    _as_int,
    _decode_value,
    _parse_attributes,
    _parse_tray,
    build_get_printer_attributes,
)
from custom_components.hp_printers.client import async_build_client
from custom_components.hp_printers.models import ProductInfo

# The response below was captured from a real printer. It is a genuine
# printer-attributes group including a printer-input-tray bag, trimmed to the
# attributes this client decodes.
TRAY_BAG = (
    "type=sheetFeedAutoNonRemovableTray;dimunit=micrometers;mediafeed=297000;"
    "maxcapacity=100;level=72;unit=percent;status=0;name=Tray 1;"
)


def _response(attributes: bytes, status: int = 0) -> bytes:
    """Wrap attribute bytes in a minimal IPP response envelope."""
    return struct.pack(">BBHI", 2, 0, status, 1) + b"\x04" + attributes + b"\x03"


def _attr(value_tag: int, name: str, value: bytes) -> bytes:
    raw_name = name.encode()
    return (
        bytes([value_tag])
        + struct.pack(">H", len(raw_name))
        + raw_name
        + struct.pack(">H", len(value))
        + value
    )


# --------------------------------------------------------------- request


def test_request_uses_the_operation_code_the_device_answers() -> None:
    """The request carries 0x000B, not the spec's 0x000A for this operation.

    RFC 8011 assigns 0x000A to Get-Printer-Attributes, but the consumer
    printers measured return ``successful-ok`` with **no** attributes group for
    it, and the full set for 0x000B. Sending 0x000A here would produce an
    empty result with no error to explain it, so the deviation is deliberate
    and pinned by this test.
    """
    assert OP_GET_PRINTER_ATTRIBUTES == 0x000B
    body = build_get_printer_attributes("ipp://printer.local:631/ipp/print")
    # version 2.0, status 0, then the operation id in the third 16-bit field
    assert int.from_bytes(body[2:4], "big") == OP_GET_PRINTER_ATTRIBUTES
    assert b"printer-uri" in body
    assert b"/ipp/print" in body


def test_request_starts_with_the_operation_group_tag() -> None:
    """Without the group tag the first value is read as a group header."""
    body = build_get_printer_attributes("ipp://h:631/ipp/print")
    # 8 bytes of header, then 0x01 = operation-attributes-group-tag
    assert body[8] == 0x01


# -------------------------------------------------------------- decoding


def test_tray_bag_is_split_into_fields() -> None:
    """The tray bag decodes into the fields the paper sensor needs."""
    fields = _parse_tray(TRAY_BAG)
    assert fields["level"] == "72"
    assert fields["maxcapacity"] == "100"
    assert fields["unit"] == "percent"
    assert fields["name"] == "Tray 1"


def test_tray_bag_tolerates_a_missing_separator() -> None:
    """A bag without a trailing separator, or an empty one, still parses."""
    assert _parse_tray("type=x;name=Tray 1")["name"] == "Tray 1"
    assert _parse_tray("") == {}


def test_attributes_decode_from_a_real_shape() -> None:
    """A realistic response decodes: text, bag, and integer attributes."""
    payload = _response(
        _attr(0x41, "printer-name", b"HPF3EC0B")
        + _attr(0x35, "printer-input-tray", TRAY_BAG.encode())
        + _attr(0x21, "printer-state", (3).to_bytes(4, "big"))
    )
    attributes = _parse_attributes(payload)
    assert attributes["printer-name"] == ["HPF3EC0B"]
    assert attributes["printer-state"] == [3]
    assert _parse_tray(attributes["printer-input-tray"][0])["level"] == "72"


def test_repeated_values_share_one_attribute_name() -> None:
    """IPP encodes a second value with a zero-length name."""
    second = struct.pack(">H", 3) + b"abc"
    payload = _response(
        _attr(0x41, "marker-names", b"K") + b"\x41" + struct.pack(">H", 0) + second
    )
    attributes = _parse_attributes(payload)
    assert attributes["marker-names"] == ["K", "abc"]


def test_short_response_is_rejected() -> None:
    """A truncated response is a parse error, not an empty result."""
    with pytest.raises(HPPrinterParseError):
        _parse_attributes(b"\x02\x00")


def test_non_success_status_is_rejected() -> None:
    """A non-zero IPP status is surfaced rather than treated as empty."""
    with pytest.raises(HPPrinterParseError):
        _parse_attributes(_response(b"", status=0x0406))


def test_unknown_tag_degrades_instead_of_raising() -> None:
    """An invented tag should cost one attribute, not the whole update."""
    assert _decode_value(0x7E, b"\x01\x02") is not None


def test_negative_level_is_dropped() -> None:
    """-2 is the device's 'I do not know' sentinel, not a level of minus two."""
    assert _as_int("-2") is None
    assert _as_int("72") == 72
    assert _as_int(None) is None
    assert _as_int("lots") is None


# ----------------------------------------------------------------- client


def _stub_session(status: int = 200, body: bytes = b"") -> MagicMock:
    response = MagicMock()
    response.status = status
    response.raise_for_status = MagicMock()
    if status >= 400:
        # aiohttp raises ClientResponseError, and the client catches the
        # ClientError family. A bare Exception would escape past both the
        # 404 branch and the connection-error branch and the test would pass
        # for the wrong reason.
        response.raise_for_status.side_effect = ClientResponseError(
            request_info=MagicMock(real_url="http://h/ipp/print"),
            history=(),
            status=status,
        )
    response.read = AsyncMock(return_value=body)
    response.text = AsyncMock(return_value=body.decode("utf-8", "replace"))

    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=response)
    context.__aexit__ = AsyncMock(return_value=False)

    session = MagicMock()
    session.post = MagicMock(return_value=context)
    return session


async def test_paper_level_is_read_from_the_tray_bag() -> None:
    """The reported tray level, capacity and unit come back as numbers."""
    payload = _response(_attr(0x35, "printer-input-tray", TRAY_BAG.encode()))
    client = IPPClient(_stub_session(body=payload), "printer.local", 631)

    trays = await client.async_get_paper_level()

    assert len(trays) == 1
    assert trays[0]["name"] == "Tray 1"
    assert trays[0]["level"] == 72
    assert trays[0]["max_capacity"] == 100
    assert trays[0]["unit"] == "percent"


async def test_absent_level_is_none_not_zero() -> None:
    """A device that does not measure paper is not saying the tray is empty."""
    bag = "type=sheetFeedAutoNonRemovableTray;maxcapacity=-2;level=-2;unit=percent;"
    payload = _response(_attr(0x35, "printer-input-tray", bag.encode()))
    client = IPPClient(_stub_session(body=payload), "printer.local", 631)

    trays = await client.async_get_paper_level()

    assert trays[0]["level"] is None
    assert trays[0]["max_capacity"] is None


async def test_no_tray_attribute_yields_no_trays() -> None:
    """A printer with no tray attribute yields nothing, not a zero."""
    payload = _response(_attr(0x41, "printer-name", b"HPF3EC0B"))
    client = IPPClient(_stub_session(body=payload), "printer.local", 631)
    assert await client.async_get_paper_level() == []


async def test_post_failure_becomes_a_connection_error() -> None:
    """A transport failure is reported as unreachable, not as no paper."""
    client = IPPClient(_stub_session(status=500), "printer.local", 631)
    with pytest.raises(HPPrinterConnectionError):
        await client.async_get_paper_level()


# -------------------------------------------------------- protocol probe


def _client(valid: ProductInfo | Exception):
    client = MagicMock()
    client.async_validate = AsyncMock(
        side_effect=valid if isinstance(valid, Exception) else None
    )
    client.async_validate.return_value = None if isinstance(valid, Exception) else valid
    return client


async def test_probe_prefers_ledm() -> None:
    """A LEDM printer costs one request: the first protocol answers."""
    info = ProductInfo(serial_number="SN-1", make_and_model="M182nw")
    built: list[str] = []

    def factory(name):
        def _make(*_a, **_kw):
            built.append(name)
            return _client(info)

        return _make

    with (
        patch("custom_components.hp_printers.client.LEDMClient", factory("ledm")),
        patch("custom_components.hp_printers.client.CDPClient", factory("cdp")),
    ):
        client, returned = await async_build_client(MagicMock(), "h", 80, False)

    assert built == ["ledm"], "CDP must not be probed once LEDM answers"
    assert client is not None
    assert returned.serial_number == "SN-1"


async def test_probe_falls_through_to_cdp_on_404() -> None:
    """The case this client was written for: no LEDM layer at all."""
    info = ProductInfo(serial_number="SN-2", make_and_model="Smart Tank 580-590")
    built: list[str] = []
    made: dict[str, MagicMock] = {}

    def factory(name, error=None):
        def _make(*_a, **_kw):
            built.append(name)
            made[name] = _client(error) if error else _client(info)
            return made[name]

        return _make

    with (
        patch(
            "custom_components.hp_printers.client.LEDMClient",
            factory("ledm", HPPrinterNotSupportedError("404")),
        ),
        patch("custom_components.hp_printers.client.CDPClient", factory("cdp")),
    ):
        client, returned = await async_build_client(MagicMock(), "h", 443, True)

    assert built == ["ledm", "cdp"]
    # Identity of the returned client, not its class: the classes are patched
    # out here, so isinstance would be asserting that the mock is a mock.
    assert client is made["cdp"]
    assert returned.serial_number == "SN-2"


async def test_probe_reports_unreachable_rather_than_unsupported() -> None:
    """An offline printer must not be reported as an unsupported one.

    The distinction drives the config-flow message, and "cannot connect" is
    the honest one when nothing answered.
    """
    error = HPPrinterConnectionError("timeout")
    with (
        patch(
            "custom_components.hp_printers.client.LEDMClient",
            lambda *_a, **_kw: _client(error),
        ),
        patch(
            "custom_components.hp_printers.client.CDPClient",
            lambda *_a, **_kw: _client(error),
        ),
        pytest.raises(HPPrinterConnectionError),
    ):
        await async_build_client(MagicMock(), "h", 80, False)


async def test_probe_reports_a_web_server_that_is_neither() -> None:
    """Every probe answered 404: a server is there, just not one we speak."""
    error = HPPrinterNotSupportedError("404")
    with (
        patch(
            "custom_components.hp_printers.client.LEDMClient",
            lambda *_a, **_kw: _client(error),
        ),
        patch(
            "custom_components.hp_printers.client.CDPClient",
            lambda *_a, **_kw: _client(error),
        ),
        pytest.raises(HPPrinterParseError),
    ):
        await async_build_client(MagicMock(), "h", 80, False)
