"""Where an alignment currently is.

The maintenance button added earlier can only say the request was accepted,
and on a semi-automatic model that is most of the job. The printer prints a
pattern, then waits for the user to put it on the scanner glass, and it says
so in a document this integration captured but never read::

    GET /Calibration/State
    <CalibrationState xmlns=".../cnx/markingagentcalibration/2009/04/08"
    >ScanRequested</CalibrationState>

Without it, a printer sitting on ``ScanRequested`` looks idle while the user
is being told the alignment is running. That is the specific gap this closes.
"""

from pathlib import Path
from unittest.mock import AsyncMock

from defusedxml import ElementTree as DefusedET
import pytest

from custom_components.hp_printers.api import (
    LEDMClient,
    _parse_ledm_calibration_state,
    _strip_namespaces,
)
from custom_components.hp_printers.const import (
    ENDPOINT_CALIBRATION_STATE,
    ENDPOINT_CONSUMABLE_CONFIG,
    ENDPOINT_PRODUCT_LOGS,
    ENDPOINT_PRODUCT_STATUS,
    ENDPOINT_PRODUCT_USAGE,
)
from custom_components.hp_printers.models import PrinterData
from custom_components.hp_printers.sensor import PRINTER_SENSORS

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "st750-ledm"

CAL_NS = "http://www.hp.com/schemas/imaging/con/cnx/markingagentcalibration/2009/04/08"

# Enough of the four required documents for a refresh to complete. The test
# below is about one optional document, and the point of it is that the
# refresh *stands* -- which cannot be shown on a client that raises before it
# gets there.
_MINIMAL = {
    "status": "<ProductStatusDyn><Status><StatusCategory>Ready</StatusCategory>"
    "</Status></ProductStatusDyn>",
    "usage": "<ProductUsageDyn><PrinterSubunit><TotalImpressions>1</TotalImpressions>"
    "</PrinterSubunit></ProductUsageDyn>",
    "consumable": "<ConsumableConfigDyn><ConsumableInfo><ConsumableLabelCode>K"
    "</ConsumableLabelCode></ConsumableInfo></ConsumableConfigDyn>",
    "logs": "<ProductLogsDyn><EventLog/></ProductLogsDyn>",
}

_REQUIRED_ENDPOINTS = {
    ENDPOINT_PRODUCT_STATUS: "status",
    ENDPOINT_PRODUCT_USAGE: "usage",
    ENDPOINT_CONSUMABLE_CONFIG: "consumable",
    ENDPOINT_PRODUCT_LOGS: "logs",
}


def _strip(text: str):
    return _strip_namespaces(DefusedET.fromstring(text))


def _state_document():
    path = FIXTURES / "Calibration_State.xml"
    if not path.exists():
        pytest.skip("calibration state not captured")
    return _strip_namespaces(DefusedET.fromstring(path.read_text(encoding="utf-8")))


# ------------------------------------------------------------- the real document


def test_the_state_is_read_from_the_captured_document() -> None:
    """The state the machine was actually sitting in, verbatim.

    Pinned to the exact token rather than to "some non-empty string": this is
    a device vocabulary, and a parser that returned the element name instead
    of its text would satisfy a looser test and tell the user nothing.

    The value has already been seen twice on one machine -- ``ScanRequested``
    while an alignment was waiting for the printed pattern, and
    ``CalibrationRequired`` once it had run. The captured one is the second,
    which is why the token is read and not matched against a list.
    """
    assert _parse_ledm_calibration_state(_state_document()) == "CalibrationRequired"


def test_the_document_is_the_one_the_manifest_declares() -> None:
    """The root element is ``CalibrationState`` in the calibration namespace.

    Asserted on the raw file rather than the parsed tree, because
    ``_strip_namespaces`` throws the namespace away and this is exactly the
    detail that cannot be reconstructed afterwards.
    """
    text = (FIXTURES / "Calibration_State.xml").read_text(encoding="utf-8")
    assert CAL_NS in text
    assert "CalibrationState" in text


# ---------------------------------------------------------------- the None paths


def test_a_printer_without_the_interface_reports_nothing() -> None:
    """Absent is not an empty string, and neither is an error."""
    assert _parse_ledm_calibration_state(None) is None


def test_a_document_that_is_not_a_state_document_is_ignored() -> None:
    """A 200 on a different resource is not a calibration state.

    The check is on the root element's name, not on the shape of its content,
    so a model that serves something else at this path yields nothing rather
    than a sentence being reported as a machine state.
    """
    other = _strip_namespaces(
        DefusedET.fromstring("<SomethingElse>ready</SomethingElse>")
    )

    assert _parse_ledm_calibration_state(other) is None


def test_an_empty_state_element_reports_nothing() -> None:
    """Rather than a blank state that reads as "somewhere in between"."""
    empty = _strip_namespaces(
        DefusedET.fromstring(f'<CalibrationState xmlns="{CAL_NS}" />')
    )

    assert _parse_ledm_calibration_state(empty) is None


def test_whitespace_only_content_reports_nothing() -> None:
    """The document is pretty-printed, so a blank body arrives with newlines."""
    blank = _strip_namespaces(
        DefusedET.fromstring(
            f'<CalibrationState xmlns="{CAL_NS}">\n  \n</CalibrationState>'
        )
    )

    assert _parse_ledm_calibration_state(blank) is None


# -------------------------------------------------------------- the read itself


async def test_the_state_is_requested_and_lands_in_the_data() -> None:
    """Proven through the public refresh, not by calling a private helper.

    Two things have to be true and only one of them is a parser question: the
    document has to be *requested* at all, and its value has to reach
    ``PrinterData``. A test that only calls the parse function passes happily
    on a build where nothing ever fetches the endpoint -- and "the document is
    fetched but nothing reads it" is exactly the half that was missing.
    """
    requested: list[str] = []

    async def fake_fetch(endpoint: str):
        requested.append(endpoint)
        if endpoint == ENDPOINT_CALIBRATION_STATE:
            return _strip(
                f'<CalibrationState xmlns="{CAL_NS}">Printing</CalibrationState>'
            )
        if endpoint in _REQUIRED_ENDPOINTS:
            return _strip(_MINIMAL[_REQUIRED_ENDPOINTS[endpoint]])
        return None

    client = LEDMClient(None, "printer.local", 443, True, False, password=None)
    client._fetch = AsyncMock(side_effect=fake_fetch)  # noqa: SLF001
    client._fetch_optional = AsyncMock(side_effect=fake_fetch)  # noqa: SLF001

    data = await client.async_get_data()

    assert ENDPOINT_CALIBRATION_STATE in requested
    assert data.calibration_state == "Printing"


async def test_a_printer_without_the_interface_still_refreshes() -> None:
    """Optional means optional: no state, and the rest of the update stands.

    Without this, adding the document would mean a firmware that does not
    serve it takes every other entity down with it.
    """

    async def fake_fetch(endpoint: str):
        if endpoint in _REQUIRED_ENDPOINTS:
            return _strip(_MINIMAL[_REQUIRED_ENDPOINTS[endpoint]])
        return None

    client = LEDMClient(None, "printer.local", 443, True, False, password=None)
    client._fetch = AsyncMock(side_effect=fake_fetch)  # noqa: SLF001
    client._fetch_optional = AsyncMock(side_effect=fake_fetch)  # noqa: SLF001

    data = await client.async_get_data()

    assert data.calibration_state is None
    assert data.printer is not None or data.status is not None, (
        "the refresh completed; the point is that it completed at all"
    )


# ------------------------------------------------------------- what HA shows


def test_the_sensor_appears_only_when_there_is_a_state() -> None:
    """Creation is driven by the same rule as every other sensor here.

    A model that serves no calibration interface gets no entity rather than an
    always-unknown one, which would read as a reading.
    """
    description = next(d for d in PRINTER_SENSORS if d.key == "calibration_state")

    assert description.value_fn(PrinterData(calibration_state="ScanRequested"), None)
    assert description.value_fn(PrinterData(), None) is None


def test_the_raw_token_is_passed_through_untouched() -> None:
    """No options list, so an unknown state is shown rather than dropped.

    A friendly mapping would have to invent vocabulary, and a state this
    integration has not been taught about is exactly the one a user needs to
    see: it is the one that would otherwise be silently rendered as "no
    problem" on a printer that is stuck.
    """
    description = next(d for d in PRINTER_SENSORS if d.key == "calibration_state")
    data = PrinterData(calibration_state="AStateNobodyHasSeen")

    assert description.value_fn(data, None) == "AStateNobodyHasSeen"
    assert not description.options, "an options list would drop the unknown state"
