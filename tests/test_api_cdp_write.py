"""Tests for the maintenance write path.

The reads have their own fixture tests. These cover the other half: the
requests that spend ink and paper, and the conditions under which the buttons
refuse to fire at all.

The stance throughout is that a write is the one thing this integration does
that a user cannot undo from Home Assistant, so the paths worth testing are
mostly the refusals. A test that only proves "the PATCH went out" would be
the least useful one in the file.
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from aiohttp import ClientError
import pytest

from custom_components.hp_printers import api_cdp
from custom_components.hp_printers.api import HPPrinterWriteError
from custom_components.hp_printers.api_cdp import CDPClient
from custom_components.hp_printers.const import CDP_REPORTS

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# Not a real credential and not a guess at one: it only has to round-trip
# through base64 and come back out the other side.
TEST_PASSWORD = "not-a-real-password"

# The reports document as the device returned it on 2026-10-05, trimmed to the
# entries that matter. Note that the cleaning pages report printable=false:
# HP does not consider a cleaning page something to put in the tray, which is
# a fact about the reports, not about whether the operation is allowed.
REPORTS_DOC = {
    "version": "1.0.0",
    "reports": [
        {"reportId": "cleaningPage", "printable": "false"},
        {"reportId": "cleaningPageLevel2", "printable": "false"},
        {"reportId": "cleaningPageLevel3", "printable": "false"},
        {"reportId": "paperFeedCleaningPage", "printable": "true"},
        {"reportId": "ribSmearCleaningPage", "printable": "true"},
        {"reportId": "alignmentPage", "printable": "true"},
    ],
}

CALIBRATION_DOC = {
    "availableCalibrations": ["penAlignSemiauto"],
    "version": "1.0.0",
    "requiresMedia": "true",
}


def _json(document: dict) -> str:
    return json.dumps(document)


def _patch_response(
    status: int = 200, body: str = '{"version":"1.0.0","state":"idle"}'
) -> MagicMock:
    response = MagicMock()
    response.status = status
    response.text = AsyncMock(return_value=body)

    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=response)
    context.__aexit__ = AsyncMock(return_value=False)
    return context


def _session(
    status: int = 200, body: str = '{"version":"1.0.0","state":"idle"}'
) -> MagicMock:
    session = MagicMock()
    session.patch = MagicMock(return_value=_patch_response(status, body))
    return session


def _reports_document() -> dict:
    """The printer's real reports document, from the anonymized capture."""
    return json.loads(
        (FIXTURES / "st580_590" / "cdm_report_v1_reports.json").read_text(
            encoding="utf-8"
        )
    )


def _client(
    session: MagicMock,
    password: str | None = TEST_PASSWORD,
    *,
    stub_reports: bool = True,
) -> CDPClient:
    """Build a client whose optional fetches answer the real reports capture.

    A report run reads the reports document first -- the version has to come
    from the device -- so the default client answers it. Tests that exercise
    the optional fetches themselves pass ``stub_reports=False`` so their own
    session is what answers.
    """
    client = CDPClient(session, "printer.local", 443, True, password=password)
    if stub_reports:
        client._fetch_optional = AsyncMock(  # noqa: SLF001
            return_value=_reports_document()
        )
    return client


# --------------------------------------------------------------- the basics


async def test_a_write_sends_no_credential_at_all() -> None:
    """CDP does not authenticate with the EWS password.

    Measured: every CDP document answers with no credential, and attaching a
    correct Basic header turns working 200s into 401s. So the password is held
    in memory and never put on the wire. Sending a value the protocol ignores
    is not harmless -- the day it is echoed into a log or an error it becomes
    a credential leak that bought nothing.
    """
    session = _session()
    client = _client(session)

    await client.async_run_report("cleaningPage")

    headers = session.patch.call_args.kwargs["headers"]
    assert "Authorization" not in headers
    assert headers["Content-Type"] == "application/json"
    # And nothing anywhere in the request carries the secret.
    assert TEST_PASSWORD not in str(session.patch.call_args)


# --------------------------------------------------- refusing to write at all


async def test_a_report_request_carries_state_and_version_as_well_as_the_id() -> None:
    """Three fields, not one.

    The printer's own web application starts from a state, looks the report up
    in the reports document, and carries across its version as well as its
    identifier. A body with only the identifier is rejected, and the device
    answers that with a 400 and an empty body -- so the failure reads as a
    broken feature rather than a wrong request.
    """
    reports = json.loads(
        (FIXTURES / "st580_590" / "cdm_report_v1_reports.json").read_text(
            encoding="utf-8"
        )
    )
    session = _session()
    client = _client(session)
    client._fetch_optional = AsyncMock(return_value=reports)  # noqa: SLF001

    await client.async_run_report("cleaningPage")

    body = json.loads(session.patch.call_args.kwargs["data"].decode())
    assert body == {
        "state": "processing",
        "version": "1.0.0",
        "reportId": "cleaningPage",
    }


async def test_the_report_version_comes_from_the_device_not_from_us() -> None:
    """A hardcoded "1.0.0" would be right until the firmware bumps it.

    The reports document carries a version per report for a reason: the
    request echoes it back. Reading it is the difference between working
    across a firmware update and not.
    """
    reports = {
        "reports": [
            {"reportId": "cleaningPage", "version": "2.3.1", "printable": "false"}
        ]
    }
    session = _session()
    client = _client(session)
    client._fetch_optional = AsyncMock(return_value=reports)  # noqa: SLF001

    await client.async_run_report("cleaningPage")

    body = json.loads(session.patch.call_args.kwargs["data"].decode())
    assert body["version"] == "2.3.1"


async def test_a_report_the_device_does_not_list_is_refused_with_a_reason() -> None:
    """Said here rather than left to a 400 with an empty body.

    A device can list a report and still refuse it, and "this printer does
    not offer that" is the answer the user can act on.
    """
    session = _session()
    client = _client(session)
    client._fetch_optional = AsyncMock(return_value={"reports": []})  # noqa: SLF001

    with pytest.raises(HPPrinterWriteError, match="does not list a report"):
        await client.async_run_report("cleaningPageLevel3")
    session.patch.assert_not_called()


async def test_calibration_patches_the_member_path_with_both_fields() -> None:
    """The member, not the collection -- and a second field beside the type.

    Both errors are invisible: the collection answers 400 to a GET, and it
    answers 400 to a body it will not accept, both with an empty body. The
    only source that can say what the request looks like is the code the
    printer ships to its own browser.
    """
    session = _session()
    client = _client(session)

    await client.async_run_calibration("penAlignSemiauto")

    url = session.patch.call_args.args[0]
    body = json.loads(session.patch.call_args.kwargs["data"].decode())
    assert url.endswith("/cdm/calibration/v1/calibration/penAlignSemiauto")
    assert body == {
        "calibrationType": "penAlignSemiauto",
        "operationType": "calibration",
    }


async def test_a_write_needs_no_password_and_still_sends_the_request() -> None:
    """No credential is required, so none is asked for.

    This is the measured behaviour, and it is the opposite of the first
    version of this test, which asserted that a missing password blocked the
    request. That assertion was written from the assumption that a write needs
    a credential; the device says it does not, and a guard built on the
    assumption would have refused every write on every CDP printer.
    """
    session = _session()
    client = _client(session, password=None)

    assert client.can_write is True
    await client.async_run_report("cleaningPage")
    session.patch.assert_called_once()


async def test_a_rejected_write_does_not_blame_the_password() -> None:
    """A 401 must not be reported as a wrong password.

    Measured on the CDP model, a correct Basic header turns working documents
    into 401s, so a 401 means the *mechanism* is wrong, not the secret.
    "Wrong password" would send the user to re-enter a credential that is
    already correct, and they would never find the real problem.
    """
    client = _client(_session(status=401, body=""))
    with pytest.raises(HPPrinterWriteError) as caught:
        await client.async_run_report("cleaningPage")

    message = str(caught.value)
    assert "does not authenticate with the EWS" in message
    assert "wrong password" not in message.lower()


async def test_an_empty_rejection_body_is_reported_as_such() -> None:
    """The CDP model answers a rejected body with 400 and nothing else.

    An error that says only "request failed" reads like a broken integration.
    Saying the device gave no reason is the accurate and more useful message.
    """
    client = _client(_session(status=400, body=""))
    with pytest.raises(HPPrinterWriteError) as caught:
        await client.async_run_report("cleaningPage")

    assert "no detail given" in str(caught.value)


async def test_a_printer_without_the_resource_says_so() -> None:
    """404 on a write means the model does not offer the operation."""
    client = _client(_session(status=404, body=""))
    with pytest.raises(HPPrinterWriteError, match="does not offer"):
        await client.async_run_report("cleaningPage")


async def test_the_device_s_own_reason_is_kept_in_the_error() -> None:
    """A refusal usually carries why, and that is the useful part.

    Busy, no paper, and a wrong password all come back as an HTTP error; only
    the body says which. Dropping it leaves the user guessing at a printer
    that is telling them exactly what is wrong.
    """
    client = _client(_session(status=409, body="maintenance cycle in progress"))
    with pytest.raises(HPPrinterWriteError, match="maintenance cycle in progress"):
        await client.async_run_report("cleaningPage")


async def test_a_non_json_success_body_is_still_a_success() -> None:
    """An empty or HTML 200 is a successful write, not a parse failure.

    Reading the response back is a convenience, not the operation. Treating a
    body we cannot parse as a failure would tell the user their clean did not
    run when it did.
    """
    client = _client(_session(status=200, body=""))
    assert await client.async_run_report("cleaningPage") == {}

    client = _client(_session(status=200, body="<html>ok</html>"))
    assert await client.async_run_report("cleaningPage") == {}


async def test_a_transport_failure_during_a_write_is_reported_as_a_write_error() -> (
    None
):
    """The write path has to say what it is, not borrow the read's wording.

    A connection error here and a connection error while polling look the same
    to the caller unless they are different types, and the user's response is
    different: this one means the button press did not reach the machine.
    """

    session = MagicMock()
    context = MagicMock()
    context.__aenter__ = AsyncMock(side_effect=ClientError("connection reset"))
    context.__aexit__ = AsyncMock(return_value=False)
    session.patch = MagicMock(return_value=context)

    client = _client(session)
    with pytest.raises(HPPrinterWriteError, match="connection reset"):
        await client.async_run_report("cleaningPage")


async def test_a_timeout_is_resolved_by_asking_the_device_not_by_failing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clean takes minutes; a client timeout does not cancel it.

    This used to assert the opposite -- that a timed-out write raises
    "may still be running it". That was the bug, not the contract: every
    maintenance button on the 580-590 failed that way, because these devices
    start a report by doing the work first and a diagnostic can outlast the
    budget. Reporting it as a failure invites the user to press again, which
    prints the report twice.

    The honest question on a timeout is not "did it fail" but "is it still
    going", and the device answers that on the URL the job was started on. The
    mechanics of the confirmation live in test_cdp_write_timeout.py; what is
    pinned here is that a report run consults it at all.
    """
    monkeypatch.setattr(api_cdp, "CDP_JOB_POLL_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(api_cdp, "CDP_JOB_POLL_INTERVAL_SECONDS", 0.0)

    session = MagicMock()
    context = MagicMock()
    context.__aenter__ = AsyncMock(side_effect=TimeoutError)
    context.__aexit__ = AsyncMock(return_value=False)
    session.patch = MagicMock(return_value=context)

    client = _client(session, stub_reports=False)

    # The reports lookup answers, then every job-state read says the device is
    # still working on it, for as long as the confirmation keeps asking.
    async def _fetch(endpoint: str) -> dict:
        if endpoint == CDP_REPORTS:
            return _reports_document()
        return {"state": "processing", "reportId": "cleaningPage"}

    client._fetch_optional = AsyncMock(side_effect=_fetch)  # noqa: SLF001

    assert await client.async_run_report("cleaningPage") == {}
    # And exactly one PATCH: the answer was read back, never re-asked.
    assert session.patch.call_count == 1


# ------------------------------------------------------ what the device offers


async def test_reports_are_read_from_the_device_not_from_a_constant() -> None:
    """The set of buttons has to match the hardware that is actually there."""
    get_response = MagicMock()
    get_response.status = 200
    get_response.text = AsyncMock(return_value=_json(REPORTS_DOC))
    get_context = MagicMock()
    get_context.__aenter__ = AsyncMock(return_value=get_response)
    get_context.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.get = MagicMock(return_value=get_context)

    client = _client(session, stub_reports=False)
    reports = await client.async_get_reports()

    assert "cleaningPage" in reports
    assert "alignmentPage" in reports
    # printable is a property of the report, not a permission check.
    assert reports["cleaningPage"] is False
    assert reports["alignmentPage"] is True


async def test_a_printer_with_no_reports_document_reports_nothing() -> None:
    """Absent is not the same as empty, and neither is an error here."""
    get_response = MagicMock()
    get_response.status = 404
    get_context = MagicMock()
    get_context.__aenter__ = AsyncMock(return_value=get_response)
    get_context.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.get = MagicMock(return_value=get_context)

    client = _client(session, stub_reports=False)
    assert await client.async_get_reports() == {}


async def test_a_malformed_report_entry_is_skipped_not_guessed_at() -> None:
    """One bad entry must not cost the whole list.

    The reports document decides which buttons exist, so dropping everything
    because of a single string where an object should be would silently
    remove the user's whole maintenance panel.
    """
    get_response = MagicMock()
    get_response.status = 200
    get_response.text = AsyncMock(
        return_value=_json(
            {
                "reports": [
                    "not-an-object",
                    {"noReportId": True},
                    {"reportId": "cleaningPage", "printable": "false"},
                ]
            }
        )
    )
    get_context = MagicMock()
    get_context.__aenter__ = AsyncMock(return_value=get_response)
    get_context.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.get = MagicMock(return_value=get_context)

    client = _client(session, stub_reports=False)
    reports = await client.async_get_reports()

    assert list(reports) == ["cleaningPage"]


async def test_requires_media_is_read_as_the_device_phrases_it() -> None:
    """CDP flags are the strings "true"/"false", not JSON booleans."""
    get_response = MagicMock()
    get_response.status = 200
    get_response.text = AsyncMock(return_value=_json(CALIBRATION_DOC))
    get_context = MagicMock()
    get_context.__aenter__ = AsyncMock(return_value=get_response)
    get_context.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.get = MagicMock(return_value=get_context)

    client = _client(session, stub_reports=False)
    capabilities = await client.async_get_calibration_capabilities()

    assert str(capabilities["requiresMedia"]).lower() == "true"
    assert "penAlignSemiauto" in capabilities["availableCalibrations"]
