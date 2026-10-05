"""Minimal IPP (RFC 8010/8011) client, for one thing: how much paper is left.

Why this exists
---------------
Neither vendor interface reports a paper *level*. LEDM's MediaHandlingDyn
gives ``TrayState``/``MediaState`` -- present or absent, with no quantity --
and the CDP media configuration has no level field at all. The only place a
percentage appears is IPP's ``printer-input-tray``, which on the models
measured reported e.g. ``maxcapacity=100;level=48;unit=percent``.

So this is deliberately not a general IPP client. It implements exactly one
operation, decodes exactly one attribute, and is used through
:meth:`IPPClient.async_get_paper_level`. Anything that needs more should use
a real IPP library rather than grow this.

A firmware quirk worth writing down
-----------------------------------
RFC 8011 assigns ``0x000A`` to Get-Printer-Attributes and ``0x000B`` to
Get-Printer-Supported-Values. On both consumer printers measured 2026-10-05
the responses are the other way round:

- ``0x000A`` -> ``successful-ok`` with **no** printer-attributes group
- ``0x000B`` -> ``successful-ok`` with the **full** 157/159 attributes

Both still speak HTTP 200 and both report ``successful-ok``, so an
implementation that trusts the spec gets an empty answer and no error to
explain it. This client therefore sends the operation code that the device
answers, with the spec's name kept in a comment so the deviation is visible
rather than looking like a typo. The upstream fixture in
``printer-doctor``/pdoc/ipp.py came from the same measurement.
"""

import logging
import ssl
import struct

from aiohttp import ClientError, ClientSession, ClientTimeout

from .api import HPPrinterConnectionError, HPPrinterError, HPPrinterParseError

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = ClientTimeout(total=20)

# See the module docstring: this is Get-Printer-Supported-Values by the RFC,
# and it is the code these printers answer with the full attribute set.
OP_GET_PRINTER_ATTRIBUTES = 0x000B

# Only these tags can carry a printer-input-tray value, and the value is a
# bag of key=value pairs, not a number. Keeping the tag table minimal means an
# unknown tag from the device is skipped rather than crashing the decode.
TAG_END = 0x03
TAG_TEXT_WITH_LANGUAGE = 0x35
TAG_NAME_WITH_LANGUAGE = 0x36
TAG_TEXT_WITHOUT_LANGUAGE = 0x41
TAG_NAME_WITHOUT_LANGUAGE = 0x42
TAG_KEYWORD = 0x44
TAG_URI = 0x45
TAG_URISCHEME = 0x46
TAG_CHARSET = 0x47
TAG_NATURAL_LANGUAGE = 0x48
TAG_MIMETYPE = 0x49
TAG_INTEGER = 0x21
TAG_BOOLEAN = 0x22
TAG_ENUM = 0x23
TAG_OCTETSTRING = 0x30
TAG_RESOLUTION = 0x32
TAG_RANGE_OF_INTEGER = 0x33

_TEXTISH = frozenset(
    {
        TAG_TEXT_WITH_LANGUAGE,
        TAG_NAME_WITH_LANGUAGE,
        TAG_TEXT_WITHOUT_LANGUAGE,
        TAG_NAME_WITHOUT_LANGUAGE,
        TAG_KEYWORD,
        TAG_URI,
        TAG_URISCHEME,
        TAG_CHARSET,
        TAG_NATURAL_LANGUAGE,
        TAG_MIMETYPE,
    }
)


def _add_attribute(buf: bytearray, value_tag: int, name: str, value: object) -> None:
    """Append one IPP attribute to a request buffer."""
    raw_name = name.encode("utf-8")
    if isinstance(value, bool):
        raw_value = b"\x01" if value else b"\x00"
    elif isinstance(value, int):
        raw_value = struct.pack(">i", value)
    else:
        raw_value = str(value).encode("utf-8")
    buf.append(value_tag)
    buf.extend(struct.pack(">H", len(raw_name)))
    buf.extend(raw_name)
    buf.extend(struct.pack(">H", len(raw_value)))
    buf.extend(raw_value)


def build_get_printer_attributes(printer_uri: str, request_id: int = 1) -> bytes:
    """Build the operation-attributes group for a printer-attributes request."""
    body = bytearray()
    body += struct.pack(">BBHI", 2, 0, OP_GET_PRINTER_ATTRIBUTES, request_id)
    body.append(0x01)  # operation-attributes-group-tag
    _add_attribute(body, TAG_CHARSET, "attributes-charset", "utf-8")
    _add_attribute(body, TAG_NATURAL_LANGUAGE, "attributes-natural-language", "en-us")
    _add_attribute(body, TAG_URI, "printer-uri", printer_uri)
    body.append(TAG_END)
    return bytes(body)


# Values carried as a 4-byte two's-complement integer.
_INTEGRAL = frozenset({TAG_INTEGER, TAG_ENUM})


def _decode_value(value_tag: int, raw: bytes) -> object:
    """Decode one attribute value, conservatively.

    An unknown tag becomes the raw bytes rather than raising: a firmware that
    invents a tag should cost us one attribute, not the whole update.
    """
    if value_tag in _TEXTISH or value_tag == TAG_OCTETSTRING:
        return raw.decode("utf-8", "replace")
    if value_tag in _INTEGRAL:
        if len(raw) == 4:
            return struct.unpack(">i", raw)[0]
        return None
    if value_tag == TAG_BOOLEAN:
        return bool(raw[0]) if raw else None
    if value_tag == TAG_RESOLUTION and len(raw) >= 9:
        return {
            "x": struct.unpack(">i", raw[0:4])[0],
            "y": struct.unpack(">i", raw[4:8])[0],
        }
    if value_tag == TAG_RANGE_OF_INTEGER and len(raw) >= 8:
        return [
            struct.unpack(">i", raw[0:4])[0],
            struct.unpack(">i", raw[4:8])[0],
        ]
    return raw


def _parse_attributes(data: bytes) -> dict[str, list[object]]:
    """Return the printer-attributes group as ``{name: [values]}``.

    A zero-length name is IPP's way of adding another value to the previous
    attribute; reading those as a new attribute is how a marker list silently
    loses a colour.
    """
    if len(data) < 8:
        raise HPPrinterParseError(f"IPP response too short ({len(data)} bytes)")
    major, _minor, status, _request_id = struct.unpack(">BBHI", data[0:8])
    if major not in (1, 2):
        raise HPPrinterParseError(f"Unexpected IPP version {major}.{_minor}")

    if status != 0x0000:
        raise HPPrinterParseError(f"IPP status 0x{status:04x}")

    attributes: dict[str, list[object]] = {}
    group_tag: int | None = None
    index = 8
    total = len(data)
    while index < total:
        tag = data[index]
        index += 1
        if tag == TAG_END:
            break
        if tag < 0x10:
            group_tag = tag
            continue
        if tag == 0x7F:  # extension tag, followed by a 4-byte tag number
            index += 4
        if index + 2 > total:
            break
        name_length = struct.unpack(">H", data[index : index + 2])[0]
        index += 2
        if name_length == 0:
            if index + 2 > total:
                break
            value_length = struct.unpack(">H", data[index : index + 2])[0]
            index += 2
            raw = data[index : index + value_length]
            index += value_length
            if attributes:
                last = next(reversed(attributes))
                attributes[last].append(_decode_value(tag, raw))
            continue
        name = data[index : index + name_length].decode("utf-8", "replace")
        index += name_length
        if index + 2 > total:
            break
        value_length = struct.unpack(">H", data[index : index + 2])[0]
        index += 2
        raw = data[index : index + value_length]
        index += value_length
        if group_tag == 0x04:  # printer-attributes-group-tag
            attributes.setdefault(name, []).append(_decode_value(tag, raw))
    return attributes


def _parse_tray(value: str) -> dict[str, str]:
    """Split an IPP tray bag into its key/value pairs.

    The value is ``type=...;maxcapacity=100;level=48;unit=percent;name=Tray 1;``
    -- semicolon separated, trailing separator, and a value that may itself
    contain no escapes. Splitting on the separator and then on the first
    ``=`` is enough for this shape.
    """
    fields: dict[str, str] = {}
    for part in value.split(";"):
        if not part:
            continue
        key, separator, raw = part.partition("=")
        if separator:
            fields[key.strip()] = raw.strip()
    return fields


class IPPClient:
    """Read-only client for the single IPP attribute this integration wants."""

    def __init__(
        self,
        session: ClientSession,
        host: str,
        port: int = 631,
        use_ssl: bool = False,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        """Initialize the client.

        The port is independent of the vendor web server's: the web UI answers
        on 80/443 while IPP answers on 631, and a printer configured for IPPS
        on that port serves the same attribute set.
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

    async def async_get_paper_level(self) -> list[dict[str, object]]:
        """Return one record per input tray the device describes.

        Each record carries the tray ``name``, its ``level`` and
        ``maxcapacity`` when the device reports them, and the raw ``type`` so
        a caller can tell a main tray from an automatic document feeder.
        An absent ``level`` is reported as ``None`` rather than 0: a device
        that does not measure paper is not saying the tray is empty, and
        "no paper" is the one reading that must never be inferred.
        """
        scheme = "ipps" if self._ssl else "ipp"
        printer_uri = f"{scheme}://{self._host}:{self._port}/ipp/print"
        url = (
            f"{'https' if self._ssl else 'http'}://{self._host}:{self._port}/ipp/print"
        )
        body = build_get_printer_attributes(printer_uri)

        try:
            async with self._session.post(
                url,
                data=body,
                timeout=REQUEST_TIMEOUT,
                ssl=self._ssl_context,
                headers={"Content-Type": "application/ipp"},
            ) as response:
                response.raise_for_status()
                payload = await response.read()
        except TimeoutError as err:
            raise HPPrinterConnectionError("Timeout fetching IPP attributes") from err
        except ClientError as err:
            raise HPPrinterConnectionError(
                f"Error fetching IPP attributes: {err}"
            ) from err

        attributes = _parse_attributes(payload)

        trays: list[dict[str, object]] = []
        for value in attributes.get("printer-input-tray", []):
            if not isinstance(value, str):
                continue
            fields = _parse_tray(value)
            if not fields:
                continue
            trays.append(
                {
                    "name": fields.get("name"),
                    "type": fields.get("type"),
                    "level": _as_int(fields.get("level")),
                    "max_capacity": _as_int(fields.get("maxcapacity")),
                    "unit": fields.get("unit"),
                }
            )
        return trays


def _as_int(value: str | None) -> int | None:
    """Parse a tray integer, returning None for absent or unknown values.

    ``-2`` is the device's "I do not know" sentinel and shows up on every
    tray of a model with no paper sensor, so it is discarded here rather than
    reaching a sensor as a level.
    """
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    if parsed < 0:
        return None
    return parsed


async def async_probe_paper(
    session: ClientSession, host: str, port: int = 631
) -> list[dict[str, object]]:
    """Convenience wrapper used by tests and the capture script."""
    client = IPPClient(session, host, port)
    return await client.async_get_paper_level()


__all__ = [
    "HPPrinterError",
    "IPPClient",
    "async_probe_paper",
    "build_get_printer_attributes",
]
