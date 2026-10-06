"""The two call sites and the protocol probe, tested the way Home Assistant runs them.

Both defects fixed in the same pass were invisible to the suite, and for the
same reason: the tests exercised the functions while the real failures were in
the *call sites* and in a branch the tests stubbed out.

1. ``async_build_client`` takes ``password`` as keyword-only, and the setup
   entry point passed it sixth, positionally. Every test called the function
   directly with a keyword, so the one place in the integration that spells
   the call out for Home Assistant was the one place never checked. The
   assertion is therefore about the source, not about behaviour: a positional
   password is a syntax-level mistake and a behavioural test is the wrong
   instrument.

2. The CDP identity document was read through the retrying helper, which
   turns a failure into an empty document. That document is what makes a
   machine a CDP device: the protocol probe relies on it *failing* to move on
   to the LEDM client, so the LEDM model could not be set up at all. The test
   below drives the probe the way the config flow does and asserts it still
   rejects a machine that will not produce an identity document.
"""

import ast
import inspect
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.hp_printers.api import HPPrinterError
from custom_components.hp_printers.api_cdp import CDPClient
from custom_components.hp_printers.client import async_build_client

COMPONENT = Path(__file__).resolve().parent.parent / "custom_components" / "hp_printers"


# ------------------------------------------------- the call sites, as written

CALLERS = ["__init__.py", "config_flow.py"]


@pytest.mark.parametrize("filename", CALLERS)
def test_no_caller_passes_the_password_positionally(filename: str) -> None:
    """Read the source: ``password=`` is required wherever it is passed.

    A behavioural test cannot do this. The function raises a ``TypeError``
    before any of its body runs, so a test would have to stand up Home
    Assistant to reach it -- and the whole point is that the mistake is in
    how the call is written.

    The config flow legitimately omits the password: it is a probe, and the
    credential is only ever supplied by setup or by a reconfigure. So the
    rule is "keyword or absent", not "keyword always".
    """
    tree = ast.parse((COMPONENT / filename).read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", None) == "async_build_client"
    ]
    assert calls, f"{filename} does not call async_build_client at all"

    for call in calls:
        assert len(call.args) <= 5, (
            f"{filename}:{call.lineno} passes {len(call.args)} positional "
            "arguments; the signature takes five before the keyword-only ones, "
            "and the setup path is the one place this call is written by hand"
        )


def test_the_setup_entry_point_supplies_the_password() -> None:
    """The one call site that must carry it, pinned separately.

    Dropping the password entirely is a quieter regression than passing it in
    the wrong place: every read still works, and only the maintenance buttons
    start returning an authentication error.
    """
    tree = ast.parse((COMPONENT / "__init__.py").read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", None) == "async_build_client"
    ]
    assert calls
    assert all("password" in {kw.arg for kw in c.keywords} for c in calls)


def test_the_signature_really_is_keyword_only() -> None:
    """The check above only means something while this holds.

    Someone tidying the signature to accept a sixth positional argument would
    otherwise make the call site valid and the guard silently pointless.
    """
    signature = inspect.signature(async_build_client)
    password = signature.parameters["password"]
    assert password.kind is inspect.Parameter.KEYWORD_ONLY


# ------------------------------------------------ the protocol probe, as run


async def test_a_printer_that_will_not_serve_an_identity_document_is_rejected() -> None:
    """The LEDM model depends on this raising.

    ``async_build_client`` tries LEDM first and CDP second; a machine that
    answers CDP's identity document is a CDP machine. So the CDP client has to
    *raise* when that read fails, and the only way it used not to was a
    reading that returned an empty document instead -- which turned a clean
    "not this protocol" into an ``AttributeError`` three frames down.
    """
    client = CDPClient(MagicMock(), "printer.local", 443, True, False)
    client._fetch = AsyncMock(  # noqa: SLF001
        side_effect=HPPrinterError("identity document unavailable")
    )
    client._fetch_optional = AsyncMock(return_value=None)  # noqa: SLF001

    with pytest.raises(HPPrinterError):
        await client.async_validate()


async def test_a_missing_identity_document_is_a_parse_error_not_an_attribute_error() -> (
    None
):
    """The backstop, tested directly.

    The caller is expected to raise before this point. If one ever does not,
    the failure has to say what happened rather than reporting a ``NoneType``
    three frames below a call that looked like it worked.
    """
    with pytest.raises(HPPrinterError):
        CDPClient._parse_product_info(None)  # noqa: SLF001


async def test_the_optional_documents_still_fail_soft() -> None:
    """A printer serving none of the six still produces product info.

    The opposite requirement to the test above, and the reason the retrying
    helper exists: these are static extras, and losing one costs an entity
    rather than the entry.
    """
    client = CDPClient(MagicMock(), "printer.local", 443, True, False)
    client._fetch = AsyncMock(  # noqa: SLF001
        side_effect=lambda endpoint: {"makeAndModel": {"name": "Smart Tank 580"}}
    )
    client._fetch_optional = AsyncMock(return_value=None)  # noqa: SLF001

    info = await client.async_get_product_info()

    assert info.make_and_model == "Smart Tank 580"
    assert info.wifi_encryption is None
    assert info.media_trays == ()
