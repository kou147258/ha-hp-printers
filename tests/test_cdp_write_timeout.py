"""Tests for a CDP write whose answer does not come back in time.

Every maintenance button on the Smart Tank 580-590 failed with

    Timeout writing /cdm/report/v1/print; the printer may still be running it

and the measurements say why. On an idle device, one at a time:

    PATCH, reportId the device does not list    -> 400 in 0.02-0.17s
    PATCH, while a report is already running    -> 409 in 0.06s
    PATCH, Printer Status Report                 -> 204 in 3.1s
    that job, read back afterwards               -> processing, then idle
                                                    about 15-20s later

None of that is twenty seconds, and none of it gets slower when a 26-request
poll runs beside it. So the timeout was not a slow network -- it was a report
that takes longer to *accept* than a status page does, because it is doing work
first. The print quality report is the obvious one: it is a diagnostic.

The printer's own web application is the authority on what to do, because it
never waits on the PATCH at all. /webApps/PrintReports/PrintReports.js hands the
job to te.Job2.InternalPrint, whose success callback goes straight to
pollJobState: the PATCH is fired and forgotten, and the job is then read from
the same URL every 3000ms, with 15000ms allowed per request and 300000ms
overall. Every constant here comes from that code.

The property these tests exist to protect is the one that costs paper if it
breaks: a write that times out must never be retried, and must not be reported
as a failure while the printer is still doing the work.
"""

import json
from unittest.mock import AsyncMock, MagicMock

from aiohttp import ClientError
import pytest

from custom_components.hp_printers import api_cdp
from custom_components.hp_printers.api import HPPrinterWriteError
from custom_components.hp_printers.api_cdp import (
    REQUEST_TIMEOUT,
    WRITE_TIMEOUT,
    CDPClient,
)
from custom_components.hp_printers.const import (
    CDP_REPORT_PRINT,
    CDP_WRITE_TIMEOUT_SECONDS,
)


@pytest.fixture(autouse=True)
def _fast_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink the job-poll budget so the confirmation tests finish.

    The budget is 120 seconds on purpose -- the printer is printing a report
    and a person is waiting for the truth about it. A test that exercises "the
    job never finishes" would otherwise sit there for the full two minutes,
    which is not a slower test, it is a wedged suite.

    Patched on the module, because that is where the names were imported into;
    patching const would leave the module reading its own copy.
    """
    monkeypatch.setattr(api_cdp, "CDP_JOB_POLL_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(api_cdp, "CDP_JOB_POLL_INTERVAL_SECONDS", 0.0)


REPORTS_DOC = {
    "version": "1.0.0",
    "reports": [
        {
            "reportId": "printQualityTestReport",
            "version": "1.0.0",
            "printable": "true",
        }
    ],
}

IDLE_SUCCESS = {
    "version": "1.0.0",
    "reportId": "printQualityTestReport",
    "state": "idle",
    "lastResult": "success",
}
PROCESSING = {
    "version": "1.0.0",
    "reportId": "printQualityTestReport",
    "state": "processing",
}


def _patch_context(
    raises: BaseException | None = None, status: int = 204, body: str = ""
) -> MagicMock:
    response = MagicMock()
    response.status = status
    response.text = AsyncMock(return_value=body)

    context = MagicMock()
    if raises is not None:
        context.__aenter__ = AsyncMock(side_effect=raises)
    else:
        context.__aenter__ = AsyncMock(return_value=response)
    context.__aexit__ = AsyncMock(return_value=False)
    return context


def _client(patch_raises: BaseException | None = None) -> tuple[CDPClient, MagicMock]:
    session = MagicMock()
    session.patch = MagicMock(return_value=_patch_context(patch_raises))
    client = CDPClient(session, "printer.local", 443, True, password="pw")
    return client, session


def _stub_job(client: CDPClient, job_states: list[dict | None]) -> AsyncMock:
    """Answer the reports lookup, then the job-state reads, from a script.

    ``job_states`` is what the device gives back on each poll of the job URL.
    The last entry repeats once the script runs out, so "the job never leaves
    processing" is written as a one-element list rather than as a loop. ``None``
    stands for a read the device dropped, which must be retried rather than
    believed.
    """
    script = list(job_states)
    index = 0

    async def _fetch(endpoint: str) -> dict | None:
        nonlocal index
        if endpoint != CDP_REPORT_PRINT:
            return REPORTS_DOC
        value = script[min(index, len(script) - 1)]
        index += 1
        return value

    client._fetch_optional = AsyncMock(side_effect=_fetch)  # noqa: SLF001
    return client._fetch_optional  # noqa: SLF001


# ------------------------------------------------ the answer arrives late


async def test_a_late_answer_is_asked_for_rather_than_reported_as_a_failure() -> None:
    """The PATCH times out, the job then reports success: that is a success.

    This is the whole bug. Before, the timeout was the end of the story and the
    user was told the write failed, which is an invitation to press the button
    again and print the report a second time.
    """
    client, _ = _client(patch_raises=TimeoutError())
    _stub_job(client, [IDLE_SUCCESS])

    assert await client.async_run_report("printQualityTestReport") == {}


async def test_a_job_still_running_when_the_budget_runs_out_is_not_a_failure() -> None:
    """Still printing is the expected outcome of a press, not an error.

    The person asked for a report. The report is coming. Saying "failed" here is
    the one answer that makes things worse, so the write succeeds and the log
    carries the fact that the printer is still going.
    """
    client, _ = _client(patch_raises=TimeoutError())
    # Never leaves "processing": the poll budget runs out.
    _stub_job(client, [PROCESSING])

    assert await client.async_run_report("printQualityTestReport") == {}


async def test_a_job_that_reports_a_failure_still_fails_and_says_why() -> None:
    """Confirming must not swallow a real failure.

    A confirmation path that always returns success would be just as wrong as
    one that always reports failure: the device's own reason has to reach the
    person who pressed the button.
    """
    client, _ = _client(patch_raises=TimeoutError())
    _stub_job(
        client,
        [
            {
                "state": "idle",
                "lastResult": "failed",
                "failureReason": "outOfPaper",
            }
        ],
    )

    with pytest.raises(HPPrinterWriteError, match="outOfPaper"):
        await client.async_run_report("printQualityTestReport")


async def test_a_dropped_job_read_is_retried_rather_than_believed() -> None:
    """A dropped read says nothing about the job.

    The device disconnects under load -- measured, and the reason the read path
    retries at all. Treating a dropped read as "the job is not running" would
    turn a fluke into a verdict.
    """
    client, _ = _client(patch_raises=TimeoutError())
    fetch = _stub_job(client, [None, None, IDLE_SUCCESS])

    assert await client.async_run_report("printQualityTestReport") == {}
    # One reports lookup plus three job reads: the two dropped ones are asked
    # again rather than believed.
    assert fetch.await_count == 4


# --------------------------------------------- the one that costs paper


async def test_a_timed_out_write_is_never_sent_again() -> None:
    """Exactly one PATCH, however long the confirmation takes.

    The device does not answer "did you get that?" separately from the work, so
    a second PATCH is not a confirmation. On the report service it is answered
    with 409 while the first job runs -- and on a machine that has already
    finished, it is a second copy of the report. Nothing in this path may retry.
    """
    client, session = _client(patch_raises=TimeoutError())
    _stub_job(client, [IDLE_SUCCESS])

    await client.async_run_report("printQualityTestReport")

    assert session.patch.call_count == 1


async def test_a_write_with_no_job_to_ask_still_fails_loudly() -> None:
    """Without a state URL there is nothing to confirm against, so say so.

    The report and calibration services both publish the running job on the URL
    they were started on. A write to something that does not has no such
    fallback, and inventing one would be a guess about a device that never
    answered.
    """
    client, _ = _client(patch_raises=TimeoutError())
    client._fetch_optional = AsyncMock(return_value=REPORTS_DOC)  # noqa: SLF001

    with pytest.raises(HPPrinterWriteError, match="may still be running it"):
        await client._patch("/cdm/whatever/v1/thing", {"a": 1})  # noqa: SLF001


# ----------------------------------------------------- answering "busy"


async def test_a_busy_printer_gets_a_sentence_instead_of_a_status_code() -> None:
    """409 is what "another job is running" looks like, measured in 0.06s.

    The generic handler renders it as "HTTP 409, no detail given" -- a status
    code where the person needs an instruction. The empty body is not the
    device withholding information; there is nothing else in it.
    """
    session = MagicMock()
    session.patch = MagicMock(return_value=_patch_context(status=409))
    client = CDPClient(session, "printer.local", 443, True, password="pw")
    client._fetch_optional = AsyncMock(return_value=REPORTS_DOC)  # noqa: SLF001

    with pytest.raises(HPPrinterWriteError) as raised:
        await client._patch(  # noqa: SLF001
            CDP_REPORT_PRINT,
            {"state": "processing", "version": "1.0.0", "reportId": "x"},
            job_endpoint=CDP_REPORT_PRINT,
        )

    message = str(raised.value)
    assert "already running" in message
    assert "409" not in message


async def test_a_busy_printer_that_explains_itself_is_still_believed() -> None:
    """The measured 409 has an empty body; a firmware variant's may not.

    The sentence is there because the device on hand says nothing, not because
    the body was thrown away. Where a body is there it is the device naming the
    job in the way, and dropping it would be the same mistake as dropping any
    other refusal reason.
    """
    session = MagicMock()
    session.patch = MagicMock(
        return_value=_patch_context(status=409, body="maintenance cycle in progress")
    )
    client = CDPClient(session, "printer.local", 443, True, password="pw")
    client._fetch_optional = AsyncMock(return_value=REPORTS_DOC)  # noqa: SLF001

    with pytest.raises(HPPrinterWriteError) as raised:
        await client._patch(  # noqa: SLF001
            CDP_REPORT_PRINT,
            {"state": "processing", "version": "1.0.0", "reportId": "x"},
            job_endpoint=CDP_REPORT_PRINT,
        )

    message = str(raised.value)
    assert "already running" in message
    assert "maintenance cycle in progress" in message


# --------------------------------------------------------- the budgets


def test_a_write_does_not_borrow_the_read_budget() -> None:
    """The two are different kinds of request and carry different numbers.

    A read that takes twenty seconds is a hung device and should be abandoned.
    A write that takes twenty seconds is a report being built, and giving up on
    it is what this file is about.
    """
    assert CDP_WRITE_TIMEOUT_SECONDS != REQUEST_TIMEOUT
    assert WRITE_TIMEOUT.total == CDP_WRITE_TIMEOUT_SECONDS


async def test_a_dropped_connection_says_the_job_may_still_be_running() -> None:
    """A ClientError on a write is ambiguous, and the message admits it.

    The request may have reached the device before the connection went -- these
    models drop connections under load, which is measured. A message reading as
    a clean failure is the one a person acts on by pressing the button again.
    """
    client, _ = _client(patch_raises=ClientError("Server disconnected"))
    client._fetch_optional = AsyncMock(return_value=REPORTS_DOC)  # noqa: SLF001

    with pytest.raises(HPPrinterWriteError) as raised:
        await client.async_run_report("printQualityTestReport")

    message = str(raised.value)
    assert "may still be running it" in message
    # The underlying reason is still there, not swallowed by the advice.
    assert "Server disconnected" in message


# ------------------------------------------------------ request shape


async def test_both_cdp_writes_hand_over_the_url_they_can_be_read_back_from() -> None:
    """Report and calibration each pass their own URL as the job endpoint.

    Neither is a guess: the report service answers both GET and PATCH on
    /cdm/report/v1/print, and the printer's own code polls exactly that URL for
    the report job and exactly the member URL for the calibration job.
    """
    client, session = _client()
    client._fetch_optional = AsyncMock(return_value=REPORTS_DOC)  # noqa: SLF001

    await client.async_run_report("printQualityTestReport")
    assert session.patch.call_args.args[0].endswith("/cdm/report/v1/print")

    await client.async_run_calibration("penAlignSemiauto")
    url = session.patch.call_args.args[0]
    assert url.endswith("/cdm/calibration/v1/calibration/penAlignSemiauto")
    # And the body is the two fields the printer's own code sends.
    body = json.loads(session.patch.call_args.kwargs["data"])
    assert body == {
        "calibrationType": "penAlignSemiauto",
        "operationType": "calibration",
    }
