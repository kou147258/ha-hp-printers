"""The LEDM calibration write path.

The interface this covers was written off as absent before it was read. It is
in DiscoveryTree.xml, at ``/Calibration/CalibrationManifest.xml`` -- one path
segment deeper than the 324 candidates tried, every one of which built the
path as ``/CalibrationManifest.xml/...`` and answered 404. Nothing about the
printer says it cannot align; what said that was a wrong prefix.

So the expectations here are read out of the printer's own manifest rather
than written down here. That is the only reason this file can claim anything
about correctness: a test asserting the request "looks like" the one in the
printer's browser would pass on any plausible-looking XML, and a
plausible-looking XML is exactly what produced two wrong implementations
earlier. The manifest pairs each URI with the element its body carries, so
deriving the expectation from it fails loudly on a wrong namespace -- and the
namespace here is the one thing that cannot be guessed, because it is the only
one on this device that does not sit under ``.../con/ledm/...``.

    GET  /Calibration/Capabilities  -> the routines on offer
    POST /Calibration/Session       -> <cal:CalibrationState>Printing</...>
"""

import base64
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from aiohttp import ClientError
from defusedxml import ElementTree as DefusedET
import pytest

from custom_components.hp_printers.api import HPPrinterWriteError, LEDMClient
from custom_components.hp_printers.const import (
    CALIBRATION_ALIGNMENT_STATE,
    ENDPOINT_CALIBRATION_SESSION,
    NS_CALIBRATION,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "st750-ledm"

# Not a real credential and not a guess at one: it only has to survive the
# base64 round trip and come back out the other side.
TEST_PASSWORD = "not-a-real-password"

# Two of the namespaces the manifest declares, named here only so the test can
# navigate it. The one that matters -- the calibration namespace -- is
# deliberately NOT among them: it is read out of the parsed document.
MAP_NS = "http://www.hp.com/schemas/imaging/con/ledm/resourcemap/2009/01/27"
DD_NS = "http://www.hp.com/schemas/imaging/con/dictionaries/1.0/"


def _manifest() -> str:
    path = FIXTURES / "Calibration_CalibrationManifest.xml"
    if not path.exists():
        pytest.skip("calibration manifest not captured")
    return path.read_text(encoding="utf-8")


def _capabilities() -> str:
    path = FIXTURES / "Calibration_Capabilities.xml"
    if not path.exists():
        pytest.skip("calibration capabilities not captured")
    return path.read_text(encoding="utf-8")


def _declared_session(manifest_text: str) -> tuple[str, str]:
    """Return the full URI and the element tag for the POST-able session.

    Read the way a client reads the document: the resource map has one root
    link carrying the base and a node per resource carrying a URI relative to
    it, and each node pairs that URI with the XML element the body is expected
    to carry. The element's qualified name therefore *is* the namespace the
    body has to use -- extracted from the parsed tree, not assumed.
    """
    root = DefusedET.fromstring(manifest_text)

    base = None
    for link in root.iter(f"{{{MAP_NS}}}ResourceLink"):
        uri = link.findtext(f"{{{DD_NS}}}ResourceURI")
        if uri and link.findtext(f"{{{MAP_NS}}}Base") == "Root":
            base = uri
    assert base == "/Calibration", "the manifest's own base URI"

    for node in root.iter(f"{{{MAP_NS}}}ResourceNode"):
        verbs = [v.text for v in node.iter(f"{{{MAP_NS}}}Verb")]
        if "Post" not in verbs:
            continue
        if node.findtext(f".//{{{DD_NS}}}ResourceURI") != "/Session":
            # /SessionV2 is a different operation, on a different branch of
            # the printer's own code. Skipping it is what keeps this test from
            # silently following whichever Post node comes first in the file.
            continue
        element = node.find(f"./{{{MAP_NS}}}XmlElement/*")
        assert element is not None, "the manifest pairs the URI with an element"
        return base + node.findtext(f".//{{{DD_NS}}}ResourceURI"), element.tag
    raise AssertionError("the manifest declares no POST-able /Session resource")


def _client(session: MagicMock, password: str | None = TEST_PASSWORD) -> LEDMClient:
    return LEDMClient(session, "printer.local", 443, True, False, password=password)


def _response(status: int, text: str, headers: dict | None = None) -> MagicMock:
    response = MagicMock()
    response.status = status
    response.text = AsyncMock(return_value=text)
    # A real dict rather than a mocked headers object: the client only ever
    # calls .get() on it, and a MagicMock with a side_effect turned out to
    # bind its default arguments in a way that made the fallback value a
    # string instead of an empty mapping.
    response.headers = dict(headers or {})
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=response)
    context.__aexit__ = AsyncMock(return_value=False)
    return context


def _session(
    *,
    status: int = 201,
    body: str = "",
    headers: dict | None = None,
    get_status: int = 200,
    get_text: str = "",
) -> MagicMock:
    """A session serving both verbs this path uses.

    One object with both, rather than a read-session swapped for a write one:
    the write reads its capability document first, and a stub with only a POST
    fails at the read with an error that has nothing to do with what is under
    test.
    """
    session = MagicMock()
    session.post = MagicMock(return_value=_response(status, body, headers))
    session.get = MagicMock(return_value=_response(get_status, get_text))
    return session


def _capability_client(
    *, status: int = 201, body: str = "", headers: dict | None = None
) -> tuple[LEDMClient, MagicMock]:
    """A client whose capability read succeeds, as it does on the model measured."""
    session = _session(
        status=status, body=body, headers=headers, get_text=_capabilities()
    )
    return _client(session), session


# ------------------------------------------------------------- reading the list


async def test_the_routine_list_comes_from_the_device() -> None:
    """Not a constant: a model that offers no alignment gets no button."""
    client, _ = _capability_client()

    capabilities = await client.async_get_calibration_capabilities()

    assert "Alignment" in capabilities["availableCalibrations"]


async def test_the_device_listing_the_same_routine_twice_yields_one() -> None:
    """It does, and the answer is a set of routines, not a list of sources."""
    root = DefusedET.fromstring(_capabilities())
    raw = [
        (n.text or "").strip()
        for n in root.iter()
        if n.tag.endswith("}CalibrationJobType")
    ]
    assert len(raw) == 2, "the captured document really does list Alignment twice"
    assert len(set(raw)) == 1

    client, _ = _capability_client()

    capabilities = await client.async_get_calibration_capabilities()

    assert capabilities["availableCalibrations"] == ["Alignment"]


async def test_a_printer_with_no_calibration_document_reports_nothing() -> None:
    """Absent is not empty, and neither is an error.

    A model whose firmware does not ship this interface answers 404, and it
    gets no button rather than a setup failure.
    """
    client = _client(_session(get_status=404, get_text=""))

    assert await client.async_get_calibration_capabilities() == {}


async def test_the_scan_capability_is_reported_as_the_device_states_it() -> None:
    """A second fact from the same document, read rather than assumed.

    Worth pinning because the alternative -- inferring "no scan calibration"
    from an absent routine -- is a different claim, and this printer makes it
    explicitly.
    """
    client, _ = _capability_client()

    capabilities = await client.async_get_calibration_capabilities()

    assert capabilities["scanCalibration"] is False


# ----------------------------------------------------------------- the write


async def test_the_url_and_element_are_the_ones_the_manifest_declares() -> None:
    """The two things that cannot be guessed, checked against the device's map.

    The namespace is the one place where a wrong value still produces a
    plausible-looking request, because ``.../con/ledm/calibration/...`` reads
    correctly and is wrong. This printer's calibration schema sits under
    ``cnx``; nothing else in the interface hints at that.
    """
    full_uri, element_tag = _declared_session(_manifest())
    namespace, _, local = element_tag[1:].partition("}")

    assert full_uri == ENDPOINT_CALIBRATION_SESSION
    assert local == "CalibrationState"
    assert namespace == NS_CALIBRATION
    assert "cnx/markingagentcalibration" in namespace, (
        "the real namespace is not under the ledm tree; do not 'fix' it to one "
        "that looks consistent with the other schemas on this device"
    )


async def test_the_body_is_the_element_the_manifest_pairs_with_the_uri() -> None:
    """Derived from the manifest, then parsed back -- not a hand-written string."""
    _, element_tag = _declared_session(_manifest())
    client, session = _capability_client()

    await client.async_run_calibration("Alignment")

    body = session.post.call_args.kwargs["data"].decode()
    root = DefusedET.fromstring(body)
    assert root.tag == element_tag
    assert root.text == CALIBRATION_ALIGNMENT_STATE


async def test_the_body_carries_a_state_and_not_the_routines_name() -> None:
    """The distinction the whole request turns on.

    ``Alignment`` is what the capability document advertises; ``Printing`` is
    the state that starts the phase which prints the alignment pattern. The
    printer's own page sends the state, and the routine is implied by which
    button was pressed and by the resource itself. A body carrying the routine
    name instead is a different request that happens to parse.
    """
    client, session = _capability_client()

    await client.async_run_calibration("Alignment")

    body = session.post.call_args.kwargs["data"].decode()
    assert "Printing" in body
    assert "Alignment" not in body


async def test_the_post_goes_to_the_session_resource() -> None:
    """POST, not PUT, and not to a member path the way CDP needs one."""
    client, session = _capability_client()

    await client.async_run_calibration("Alignment")

    kwargs = session.post.call_args.kwargs
    # The URL is positional in the transport; only the headers are keywords.
    assert session.post.call_args.args[0].endswith(ENDPOINT_CALIBRATION_SESSION)
    assert kwargs["headers"]["Content-Type"].startswith("text/xml")


async def test_the_write_carries_the_admin_password() -> None:
    """The one place on this protocol that authenticates.

    Worth pinning because getting it backwards is silent on one side and a 401
    on the other: CDP needs no credential to write, and sending one there
    breaks the reads instead.
    """
    client, session = _capability_client()

    await client.async_run_calibration("Alignment")

    auth = session.post.call_args.kwargs["headers"]["Authorization"]
    assert (
        auth == "Basic " + base64.b64encode(b"admin:" + TEST_PASSWORD.encode()).decode()
    )


async def test_a_routine_the_device_does_not_offer_is_refused_before_the_post() -> None:
    """Said here rather than left to whatever the device answers.

    The capability document is the same one the printer's own page checks
    before it draws a button, so refusing here is the same decision at the
    same moment -- and with a message the user can act on.
    """
    client, session = _capability_client()

    with pytest.raises(HPPrinterWriteError, match="does not offer"):
        await client.async_run_calibration("LineFeedCalibration")

    session.post.assert_not_called()


async def test_an_empty_routine_is_refused() -> None:
    """Rather than posted as an empty document and left to fail."""
    client, _ = _capability_client()

    with pytest.raises(HPPrinterWriteError, match="No calibration type"):
        await client.async_run_calibration("")


async def test_a_refused_alignment_keeps_the_device_s_own_reason() -> None:
    """The body is the only place the reason appears."""
    client, _ = _capability_client(status=409, body="printer busy")

    with pytest.raises(HPPrinterWriteError) as caught:
        await client.async_run_calibration("Alignment")

    assert "printer busy" in str(caught.value)


async def test_a_refusal_with_no_body_says_so() -> None:
    """An error that says only 'request failed' reads like a broken integration."""
    client, _ = _capability_client(status=500, body="")

    with pytest.raises(HPPrinterWriteError, match="no detail given"):
        await client.async_run_calibration("Alignment")


async def test_a_timeout_says_the_printer_may_still_be_running_it() -> None:
    """An alignment can sit waiting on the scanner glass for a long time."""
    context = MagicMock()
    context.__aenter__ = AsyncMock(side_effect=TimeoutError)
    context.__aexit__ = AsyncMock(return_value=False)
    session = _session(get_text=_capabilities())
    session.post = MagicMock(return_value=context)
    client = _client(session)

    with pytest.raises(HPPrinterWriteError, match="may still be running"):
        await client.async_run_calibration("Alignment")


async def test_a_transport_failure_is_reported_as_a_write_failure() -> None:
    """So it is distinguishable from a read that failed to refresh."""
    context = MagicMock()
    context.__aenter__ = AsyncMock(side_effect=ClientError("reset"))
    context.__aexit__ = AsyncMock(return_value=False)
    session = _session(get_text=_capabilities())
    session.post = MagicMock(return_value=context)
    client = _client(session)

    with pytest.raises(HPPrinterWriteError, match="reset"):
        await client.async_run_calibration("Alignment")


async def test_the_created_job_is_returned_for_the_caller_to_follow() -> None:
    """201 with a Location is the device acknowledging, not reporting a result.

    The alignment then waits on the user to put the printed pattern on the
    scanner glass, so there is no completion to report and the job's own state
    resource is the honest thing to hand back.
    """
    client, _ = _capability_client(
        headers={"Location": "http://printer/Jobs/JobList/4"}
    )

    result = await client.async_run_calibration("Alignment")

    assert result["location"] == "http://printer/Jobs/JobList/4"


# ------------------------------------------------------------- the fixtures


def test_the_capability_document_is_captured_as_a_fixture() -> None:
    """The tests above read the real document; this is the thing they read.

    A test that replays a fixture nobody captured would pass on a machine
    that has never seen a printer, which is not the same as passing.
    """
    path = FIXTURES / "Calibration_Capabilities.xml"
    if not path.exists():
        pytest.skip("calibration capabilities not captured")
    text = path.read_text(encoding="utf-8")
    assert "Alignment" in text
    # No serial or hostname may survive into the repository.
    assert "CN412171R6" not in text
    assert "644ED730966B" not in text


def test_the_manifest_is_the_source_of_the_namespace_not_a_comment() -> None:
    """A regression guard on where the authority for this lives.

    The namespace was read out of the manifest's own element names. If someone
    later "tidies" the constant to match the other schemas on this device --
    every one of which sits under .../con/ledm/ -- the request would still be
    well-formed XML and would still fail on the printer. The test above fails
    on that change; this one says why.
    """
    root = DefusedET.fromstring(_manifest())
    assert root.tag.startswith("{http://www.hp.com/schemas/imaging/con/ledm/manifest/")

    _, element_tag = _declared_session(_manifest())
    namespace = element_tag[1:].partition("}")[0]

    assert namespace == NS_CALIBRATION
    assert not namespace.startswith("http://www.hp.com/schemas/imaging/con/ledm/"), (
        "the calibration schema is not in the ledm tree"
    )
