"""The LEDM internal-print write path.

Reachable by exactly one URL, and invisible on both of the obvious ones: the
capability document is not in DiscoveryTree.xml, the page that uses it lives
at a path that answers 403, and the resource itself answers 404 to a GET with
an empty body. A client that probes with GET concludes the interface does not
exist, which is what happened here for long enough to be written down as a
conclusion.

Everything asserted here is the shape the printer's own web application uses,
read out of the code it ships to its browser:

    GET  /DevMgmt/InternalPrintCap.xml          -> the job types on offer
    POST /DevMgmt/InternalPrintDyn.xml          -> XML body naming one
"""

import base64
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from aiohttp import ClientError
from defusedxml import ElementTree as DefusedET
import pytest

from custom_components.hp_printers.api import (
    HPPrinterWriteError,
    LEDMClient,
    _strip_namespaces,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# Not a real credential and not a guess at one: it only has to survive the
# base64 round trip and come back out the other side.
TEST_PASSWORD = "not-a-real-password"

# The job types the model measured returns. Eleven of the nineteen it
# publishes are wired to a button; the rest are print-only pages.
INTERNAL_JOBS = (
    "cleaningPage",
    "cleaningPageLevel2",
    "cleaningPageLevel3",
    "ribSmearCleaningPage",
    "pqDiagnosticsPage",
)


def _client(session: MagicMock, password: str | None = TEST_PASSWORD) -> LEDMClient:
    return LEDMClient(session, "printer.local", 443, True, False, password=password)


def _session(
    status: int = 200, body: str = "", headers: dict | None = None
) -> MagicMock:
    response = MagicMock()
    response.status = status
    response.text = AsyncMock(return_value=body)
    # A real dict rather than a mocked headers object: the client only ever
    # calls .get() on it, and a MagicMock with a side_effect turned out to
    # bind its default arguments in a way that made the fallback value a
    # string instead of an empty mapping.
    response.headers = dict(headers or {})
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=response)
    context.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.post = MagicMock(return_value=context)
    return session


def _cap_session() -> MagicMock:
    """A session whose GET returns the real capability document."""

    path = FIXTURES / "st750-ledm" / "DevMgmt_InternalPrintCap.xml"
    document = (
        _strip_namespaces(DefusedET.fromstring(path.read_text(encoding="utf-8")))
        if path.exists()
        else None
    )

    if document is None:
        pytest.skip("real capability document not captured")

    get_response = MagicMock()
    get_response.status = 200
    get_response.text = AsyncMock(
        return_value=__import__("defusedxml").ElementTree.tostring(
            document, encoding="unicode"
        )
    )
    get_context = MagicMock()
    get_context.__aenter__ = AsyncMock(return_value=get_response)
    get_context.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.get = MagicMock(return_value=get_context)
    return session


# ------------------------------------------------------------- reading the list


async def test_the_job_list_comes_from_the_device() -> None:
    """Not a constant: a model with fewer jobs gets fewer buttons."""
    client = _client(_cap_session())

    jobs = await client.async_get_internal_jobs()

    assert "cleaningPage" in jobs
    assert "ribSmearCleaningPage" in jobs
    assert "pqDiagnosticsPage" in jobs


async def test_a_printer_with_no_capability_document_reports_nothing() -> None:
    """Absent is not empty, and neither is an error.

    A model whose firmware does not ship the maintenance interface answers
    404 here, and it gets no buttons rather than a setup failure.
    """
    session = MagicMock()
    get_response = MagicMock()
    get_response.status = 404
    get_response.text = AsyncMock(return_value="")
    get_context = MagicMock()
    get_context.__aenter__ = AsyncMock(return_value=get_response)
    get_context.__aexit__ = AsyncMock(return_value=False)
    session.get = MagicMock(return_value=get_context)

    client = _client(session)

    assert await client.async_get_internal_jobs() == ()


# ----------------------------------------------------------------- the write


async def test_the_body_is_xml_naming_the_job() -> None:
    """POST, not PUT, and a single element rather than a JSON object.

    CDP's report and calibration are JSON PATCHes on different paths; this is
    an XML POST on a third. One description table serving both protocols only
    works because the identifier fields are kept apart.
    """
    session = _session(
        status=200, body="<j:Job/>", headers={"Location": "/DevMgmt/J/1"}
    )
    client = _client(session)
    client.async_get_internal_jobs = AsyncMock(return_value=INTERNAL_JOBS)

    await client.async_run_internal_job("cleaningPage")

    session.post.assert_called_once()
    kwargs = session.post.call_args.kwargs
    body = kwargs["data"].decode()
    assert "cleaningPage" in body
    assert "JobType" in body
    assert kwargs["headers"]["Content-Type"].startswith("text/xml")


async def test_the_write_carries_the_admin_password() -> None:
    """The opposite arrangement to CDP, where sending one breaks the reads.

    Worth pinning because getting it backwards is silent on one side and a
    401 on the other.
    """

    session = _session()
    client = _client(session, password=TEST_PASSWORD)
    client.async_get_internal_jobs = AsyncMock(return_value=INTERNAL_JOBS)

    await client.async_run_internal_job("cleaningPage")

    auth = session.post.call_args.kwargs["headers"]["Authorization"]
    assert (
        auth == "Basic " + base64.b64encode(b"admin:" + TEST_PASSWORD.encode()).decode()
    )


async def test_a_job_the_device_does_not_offer_is_refused_before_the_post() -> None:
    """Said here rather than left to whatever the device answers.

    The capability document is the same list the printer's own page checks
    before it draws a button, so refusing here is the same decision at the
    same moment -- and with a message the user can act on.
    """
    session = _session()
    client = _client(session)
    client.async_get_internal_jobs = AsyncMock(return_value=INTERNAL_JOBS)

    with pytest.raises(HPPrinterWriteError, match="does not offer"):
        await client.async_run_internal_job("someJobThisModelLacks")

    session.post.assert_not_called()


async def test_an_empty_job_type_is_refused() -> None:
    """Rather than posted as an empty element and left to fail."""
    client = _client(_session())

    with pytest.raises(HPPrinterWriteError, match="No job type"):
        await client.async_run_internal_job("")


async def test_a_refused_job_keeps_the_device_s_own_reason() -> None:
    """The body is the only place the reason appears."""
    client = _client(_session(status=409, body="printer busy"))
    client.async_get_internal_jobs = AsyncMock(return_value=INTERNAL_JOBS)

    with pytest.raises(HPPrinterWriteError) as caught:
        await client.async_run_internal_job("cleaningPage")

    assert "printer busy" in str(caught.value)


async def test_a_refusal_with_no_body_says_so() -> None:
    """An error that says only "request failed" reads like a broken integration."""
    client = _client(_session(status=500, body=""))
    client.async_get_internal_jobs = AsyncMock(return_value=INTERNAL_JOBS)

    with pytest.raises(HPPrinterWriteError, match="no detail given"):
        await client.async_run_internal_job("cleaningPage")


async def test_a_timeout_says_the_printer_may_still_be_running_it() -> None:
    """A clean takes minutes, so a client timeout is not a cancellation."""
    session = MagicMock()
    context = MagicMock()
    context.__aenter__ = AsyncMock(side_effect=TimeoutError)
    context.__aexit__ = AsyncMock(return_value=False)
    session.post = MagicMock(return_value=context)

    client = _client(session)
    client.async_get_internal_jobs = AsyncMock(return_value=INTERNAL_JOBS)

    with pytest.raises(HPPrinterWriteError, match="may still be running"):
        await client.async_run_internal_job("cleaningPage")


async def test_a_transport_failure_is_reported_as_a_write_failure() -> None:
    """So it is distinguishable from a read that failed to refresh."""

    session = MagicMock()
    context = MagicMock()
    context.__aenter__ = AsyncMock(side_effect=ClientError("reset"))
    context.__aexit__ = AsyncMock(return_value=False)
    session.post = MagicMock(return_value=context)

    client = _client(session)
    client.async_get_internal_jobs = AsyncMock(return_value=INTERNAL_JOBS)

    with pytest.raises(HPPrinterWriteError, match="reset"):
        await client.async_run_internal_job("cleaningPage")


def test_the_capability_document_is_captured_as_a_fixture() -> None:
    """The test above reads the real document; this is the thing it reads.

    A test that replays a fixture nobody captured would pass on a machine
    that has never seen a printer, which is not the same as passing.
    """
    path = FIXTURES / "st750-ledm" / "DevMgmt_InternalPrintCap.xml"
    if not path.exists():
        pytest.skip("capability document not captured")
    text = path.read_text(encoding="utf-8")
    assert "cleaningPage" in text
    # No serial or hostname may survive into the repository.
    assert "CN412171R6" not in text
    assert "644ED730966B" not in text


def test_the_job_list_is_parsed_from_real_xml_not_a_list_here() -> None:
    """Guard against the fixture being replaced by a hardcoded list."""

    path = FIXTURES / "st750-ledm" / "DevMgmt_InternalPrintCap.xml"
    if not path.exists():
        pytest.skip("capability document not captured")
    root = _strip_namespaces(DefusedET.fromstring(path.read_text(encoding="utf-8")))
    names = [
        (n.text or "").strip()
        for n in root.iter()
        if n.tag == "JobType" and (n.text or "").strip()
    ]
    assert len(names) == 19, "the device publishes 19 job types"
    assert json.dumps(names[:3]) == json.dumps(
        ["cleaningPage", "cleaningPageLevel2", "cleaningPageLevel3"]
    )
