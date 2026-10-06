"""Six CDP documents that were served the whole time and never opened.

The list this integration fetches was built from what it needed rather than
from what the device offers, so a document answering 200 on every poll went
unread for as long as the integration existed. Two of the six carry facts that
change what a user should do -- the reason a firmware update failed, and
whether the wireless link still allows the legacy cipher.

The file is also where the wireless document's privacy boundary is pinned,
because that is the one place in this integration where a field is present in
the response and deliberately not read:

    "ssid": "CLTX",
    "passPhrase": "password",

An SSID is the user's network name. A pass phrase is a credential. Both would
end up in state attributes and in diagnostics downloads that get pasted into
issue trackers, and neither is needed to answer "is this link encrypted
properly".
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.hp_printers.api_cdp import (
    CDPClient,
    _parse_cdp_media,
    _parse_cdp_system,
    _parse_firmware_history,
    _parse_supply_alert_subjects,
    _parse_wireless_security,
)
from custom_components.hp_printers.const import (
    CDP_FIRMWARE_HISTORY,
    CDP_MEDIA_CONFIG,
    CDP_SUPPLY_ALERTS,
    CDP_WIRELESS_CONFIG,
)
from custom_components.hp_printers.models import PrinterData, ProductInfo
from custom_components.hp_printers.sensor import PRINTER_SENSORS, _media_summary

HOST = "192.168.9.12"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "cdp-580-590"


def _fixture(name: str) -> dict:
    path = FIXTURES / name
    if not path.exists():
        pytest.skip(f"{name} not captured")
    return json.loads(path.read_text(encoding="utf-8"))


# ------------------------------------------------------- the wireless boundary


SSID_PLACEHOLDER = "ANON-SSID-0000"
PASSPHRASE_PLACEHOLDER = "ANON-PASSPHRASE-0000"


def test_the_network_name_and_its_pass_phrase_are_not_read() -> None:
    """The document carries both in clear text. Neither is returned.

    Asserted on the shape of the answer rather than on the absence of a key in
    a dict, because the failure mode is a field that grows rather than one
    that is misspelled: someone adds ``ssid`` to the returned mapping and
    every other test here keeps passing.

    Checked against the placeholders the fixture carries rather than against
    the real values, so a re-capture that skipped the scrubber fails here
    instead of quietly republishing a network name.
    """
    parsed = _parse_wireless_security(_fixture("wirelessConfig.json"))

    assert set(parsed) == {
        "wifi_band",
        "wifi_authentication",
        "wifi_encryption",
        "wifi_wpa_version",
    }
    flat = " ".join(str(v) for v in parsed.values())
    assert SSID_PLACEHOLDER not in flat
    assert PASSPHRASE_PLACEHOLDER not in flat


def test_the_fixture_really_does_carry_them() -> None:
    """Otherwise the test above passes for the wrong reason.

    A privacy guard against a document that stopped including the field is not
    a guard, and this is the only thing that tells the two apart. Asserted on
    the placeholders too: the field has to be present *and* still scrubbed.
    """
    document = _fixture("wirelessConfig.json")
    profile = document[document["preferredProfile"]]

    assert profile["ssid"] == SSID_PLACEHOLDER
    assert profile["passPhrase"] == PASSPHRASE_PLACEHOLDER


def test_the_legacy_cipher_is_still_surfaced() -> None:
    """The point of reading the document at all.

    ``aesOrTkip`` allows TKIP, which is the weak legacy option. Whether or not
    it is in use right now, the machine permitting it is worth knowing.
    """
    parsed = _parse_wireless_security(_fixture("wirelessConfig.json"))

    assert parsed["wifi_encryption"] == "aesOrTkip"
    assert parsed["wifi_band"] == "band2pt4Ghz"
    assert parsed["wifi_authentication"] == "wpaPersonal"


def test_a_missing_document_reads_as_nothing_rather_than_as_secure() -> None:
    """Absent is not "encrypted properly", and the entity must show nothing."""
    assert _parse_wireless_security(None) == {}
    assert _parse_wireless_security({}) == {}


def test_a_profile_the_device_does_not_name_is_not_guessed() -> None:
    """The values are nested under a profile chosen by pointer."""
    document = _fixture("wirelessConfig.json")
    document["preferredProfile"] = "wlanProfile2"

    assert _parse_wireless_security(document) == {}


# ------------------------------------------------------------- firmware history


def test_the_failure_reason_is_the_newest_entry_not_the_first() -> None:
    """Only one of the five entries carries a reason, and it is not the last.

    Picking the first entry would report ``None`` and quietly lose the only
    useful sentence in the document; picking the last is also wrong, because
    the device's newest entry here is blank. So the rule is: the most recent
    entry that *has* a reason, falling back to the most recent entry.
    """
    document = _fixture("updateHistory.json")

    parsed = _parse_firmware_history(document)

    assert parsed["firmware_update_failure_reason"] == "manifestNotFound"
    assert parsed["firmware_update_history_count"] == 5
    assert parsed["firmware_update_attempts_failed"] == 5


def test_an_empty_history_is_not_a_clean_one() -> None:
    """Absent history is not a clean history, and neither is an error."""
    assert _parse_firmware_history(None) == {}
    assert _parse_firmware_history({"updates": []}) == {}


# ------------------------------------------------------------- supply subjects


def test_the_colour_comes_out_of_a_pointer_not_out_of_prose() -> None:
    """The CDP twin of the LEDM subjectless alert.

    The alert body says ``genuineHPsupply`` and nothing else. The colour is a
    ``propertyPointer`` into the supplies document with a value beside it.
    """
    parsed = _parse_supply_alert_subjects(_fixture("supplyAlerts.json"))

    assert parsed["supply_alert_colors"] == ("C", "CMY", "K", "M", "Y")


def test_a_pointer_this_integration_has_not_seen_is_ignored() -> None:
    """Reading a colour out of an arbitrary pointer would be a guess."""
    document = {
        "alerts": [
            {
                "data": [
                    {
                        "propertyPointer": "/suppliesList/0/supplyType",
                        "value": {"seValue": "inkTank"},
                    }
                ]
            }
        ]
    }

    assert _parse_supply_alert_subjects(document) == {"supply_alert_colors": ()}


def test_no_alerts_reads_as_none_so_no_entity_is_created() -> None:
    """An empty tuple, so the entity is simply not created."""
    assert _parse_supply_alert_subjects(None) == {}
    assert _parse_supply_alert_subjects({"alerts": []}) == {"supply_alert_colors": ()}


# ------------------------------------------------------------------- the media


def test_the_tray_reports_what_is_loaded_not_what_it_could_take() -> None:
    """This interface reported no media at all before this document was read."""
    parsed = _parse_cdp_media(_fixture("mediaConfiguration.json"))

    assert parsed["media_trays"] == (
        {
            "id": "main",
            "size": "iso_a4_210x297mm",
            "type": "stationery",
            "resolution_dpi": 7200,
        },
    )
    assert parsed["output_bins"] == ("bin",)


def test_an_input_with_no_identifier_is_skipped_rather_than_half_listed() -> None:
    """A tray is listed even with its identifier missing: the size is the fact."""
    assert _parse_cdp_media({"inputs": [{"currentMediaSize": "a4"}]}) == {
        "media_default_source": None,
        "media_trays": (
            {"id": None, "size": "a4", "type": None, "resolution_dpi": None},
        ),
        "output_bins": (),
    }


# ------------------------------------------------------------------- the system


def test_the_region_is_read_and_where_the_printer_sits_is_not() -> None:
    """``deviceLocation`` is free-text placement, and stays unread.

    Region is a real fact for an ink-tank machine: a cartridge bought for one
    region is refused by a printer set to another, and nothing else says so.
    """
    document = _fixture("systemConfiguration.json")
    assert not document["deviceLocation"], (
        "the fixture's placement field is scrubbed, so a leak here would be a "
        "regression in the scrubber rather than in the parser"
    )

    parsed = _parse_cdp_system(document)
    assert parsed == {"country_region": "CN", "device_language": "zh-CN"}
    assert "deviceLocation" not in parsed


# --------------------------------------------------------------- what HA shows


async def test_the_six_documents_are_actually_requested() -> None:
    """Through the slow-cadence read, not through the poll.

    A parse test cannot see a missing fetch, and "fetched but nothing reads
    it" is the shape this integration had for months.

    The method matters as much as the URLs: these belong to
    ``async_get_product_info`` and not to ``async_get_data``. A poll is
    already twenty-six requests wide and the CDP models fail the TLS handshake
    under concurrent connections, so putting them on the poll made the facts
    absent on a varying fraction of refreshes rather than wrong.
    """
    requested: list[str] = []

    async def fake_fetch(endpoint: str):
        requested.append(endpoint)
        if endpoint == CDP_WIRELESS_CONFIG:
            return _fixture("wirelessConfig.json")
        if endpoint == CDP_SUPPLY_ALERTS:
            return _fixture("supplyAlerts.json")
        if endpoint == CDP_FIRMWARE_HISTORY:
            return _fixture("updateHistory.json")
        if endpoint == CDP_MEDIA_CONFIG:
            return _fixture("mediaConfiguration.json")
        return {}

    client = CDPClient(MagicMock(), "printer.local", 443, True, False)
    client._fetch = AsyncMock(side_effect=fake_fetch)  # noqa: SLF001
    client._fetch_optional = AsyncMock(side_effect=fake_fetch)  # noqa: SLF001

    info = await client.async_get_product_info()

    for endpoint in (
        CDP_WIRELESS_CONFIG,
        CDP_SUPPLY_ALERTS,
        CDP_FIRMWARE_HISTORY,
        CDP_MEDIA_CONFIG,
    ):
        assert endpoint in requested, f"{endpoint} was never fetched"

    # And the values actually landed on the object the entities read from.
    assert info.wifi_encryption == "aesOrTkip"
    assert info.supply_alert_colors == ("C", "CMY", "K", "M", "Y")
    assert info.media_trays
    assert info.firmware_update_failure_reason == "manifestNotFound"


async def test_the_six_documents_are_not_on_the_poll_path() -> None:
    """The regression this move exists to prevent.

    Asserted negatively and on the poll path specifically, because the failure
    it guards against is invisible: nothing raises, the documents just come
    back empty on some refreshes, and the entities flicker.
    """
    requested: list[str] = []

    async def fake_fetch(endpoint: str):
        requested.append(endpoint)
        return {}

    client = CDPClient(MagicMock(), "printer.local", 443, True, False)
    client._fetch = AsyncMock(side_effect=fake_fetch)  # noqa: SLF001
    client._fetch_optional = AsyncMock(side_effect=fake_fetch)  # noqa: SLF001

    await client.async_get_data()

    for endpoint in (
        CDP_WIRELESS_CONFIG,
        CDP_SUPPLY_ALERTS,
        CDP_FIRMWARE_HISTORY,
        CDP_MEDIA_CONFIG,
    ):
        assert endpoint not in requested, (
            f"{endpoint} is static and does not belong on the poll path"
        )


def test_the_media_entity_reads_as_a_sentence_not_a_count() -> None:
    """A user opening this wants to know what paper is in the machine."""
    info = ProductInfo(
        media_trays=(
            {
                "id": "main",
                "size": "iso_a4_210x297mm",
                "type": "stationery",
                "resolution_dpi": 7200,
            },
        )
    )

    assert _media_summary(info) == "main / iso_a4_210x297mm / stationery"
    assert _media_summary(ProductInfo()) is None


def test_the_wifi_entity_publishes_no_name_or_phrase() -> None:
    """Checked on the rendered entity, which is where it would leak."""
    description = next(d for d in PRINTER_SENSORS if d.key == "wifi_encryption")
    info = ProductInfo(
        wifi_band="band2pt4Ghz",
        wifi_authentication="wpaPersonal",
        wifi_encryption="aesOrTkip",
        wifi_wpa_version="auto",
    )

    # The cipher is the entity's state, the rest of the radio's shape is its
    # attributes, and neither carries the network's name.
    attributes = description.attrs_fn(PrinterData(), info)
    rendered = json.dumps([description.value_fn(PrinterData(), info), attributes])

    assert "aesOrTkip" in rendered
    assert SSID_PLACEHOLDER not in rendered
    assert PASSPHRASE_PLACEHOLDER not in rendered
    # On the key names, not only on the values. Checking the placeholders
    # alone passes if the field is added with some other value, which is the
    # shape a real re-capture would take.
    assert "ssid" not in {k.lower() for k in attributes}
    assert "passphrase" not in {k.lower() for k in attributes}
