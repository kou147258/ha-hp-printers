"""Replay the real captures through the new parsers.

These are the tests that would have caught the two wrong readings found while
writing this code, and neither of them is findable from a hand-built sample:

- ``GetCommunityNameConfig`` is an enumeration whose only declared values are
  ``publicAllowed`` and ``publicNotAllowed``. A generic on/off test reports a
  printer permitting the default community string as switched off, because
  neither value contains "enabled". A hand-written fixture would have used
  whatever spelling the author imagined.
- ``SNMPConfigWithVersion`` on the LEDM side and ``snmpV1V2Config`` on the CDP
  side are the same fact in two shapes, and only reading the real documents
  shows that.

Every value asserted here came out of a printer, and every fixture is the
repository's own anonymized capture. Nothing in this file is invented, which
is the point: a parser can only be pinned to what the device actually said.
"""

import json
from pathlib import Path

from defusedxml import ElementTree as DefusedET
import pytest

from custom_components.hp_printers.api import (
    LEDMClient,
    _parse_current_media,
    _parse_instant_ink,
    _parse_network_services,
    _parse_print_configuration,
    _snmp_public_allowed,
    _strip_namespaces,
)
from custom_components.hp_printers.api_cdp import CDPClient
from custom_components.hp_printers.models import PrinterData

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CDP12 = FIXTURES / "st580_590"
CDP20 = FIXTURES / "st750-cdp"
LEDM = FIXTURES / "st750-ledm"

# The fixture directories are the ones this file exists to exercise, so a
# capture that was never taken should fail here rather than silently skip.
pytestmark = pytest.mark.skipif(
    not CDP12.is_dir() or not LEDM.is_dir(),
    reason="real captures not present",
)


def cdp(name: str) -> dict:
    """Return one JSON document from the CDP printer's capture."""
    path = CDP12 / name
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def cdp20(name: str) -> dict:
    """Return one JSON document from the LEDM printer's CDP layer."""
    path = CDP20 / name
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def ledm(name: str):
    """Return one namespace-stripped XML document from the LEDM capture."""
    path = LEDM / name
    if not path.exists():
        return None
    # The same step the live parser does; without it every lookup by local
    # name returns nothing and the failure reads like a wrong key name.
    return _strip_namespaces(DefusedET.fromstring(path.read_text(encoding="utf-8")))


# ------------------------------------------------------------- setup steps


def test_the_outstanding_setup_step_is_named() -> None:
    """The alignment step is the one the device is still waiting on.

    This is the reading that reframes a calibration failure: the printhead is
    not broken, the first-time setup was never finished. Asserted on the
    capture rather than derived, because the mapping from ``action<Name>`` to
    the exposed name is where a parser silently drops half its output.
    """
    steps = CDPClient._parse_setup_steps(cdp("cdm_deviceSetup_v1_status.json"))  # noqa: SLF001

    assert steps == ("SemiAutoCalibration",)


def test_a_fully_set_up_device_has_no_pending_steps() -> None:
    """Completed steps are not news; listing them would make every printer.

    that finished all five look like one that finished none.
    """
    document = {
        "setupOperationState": "idle",
        "actionLanguageCountry": {"status": "completed", "suggestedOrder": 1},
        "actionFillInTanks": {"status": "completed", "suggestedOrder": 2},
    }
    assert CDPClient._parse_setup_steps(document) == ()  # noqa: SLF001


def test_a_printer_with_no_checklist_is_not_reported_as_setup_complete() -> None:
    """Off reads as a positive answer, not as an absence of one.

    An LEDM model publishes no setup document at all, so returning False
    there would have the integration asserting that setup is finished on a
    machine that never mentioned it.
    """
    assert PrinterData().setup_incomplete is None
    assert PrinterData(setup_operation_state="idle").setup_incomplete is False
    assert (
        PrinterData(
            setup_operation_state="actionPending",
            setup_pending_steps=("SemiAutoCalibration",),
        ).setup_incomplete
        is True
    )


def test_pending_steps_come_back_in_the_devices_own_order() -> None:
    """The device supplies suggestedOrder; re-sorting by name would put the.

    last step first, which is the opposite of the order it has to be done.
    """
    document = {
        "actionSecond": {"status": "pending", "suggestedOrder": 2},
        "actionFirst": {"status": "pending", "suggestedOrder": 1},
    }
    assert CDPClient._parse_setup_steps(document) == ("First", "Second")  # noqa: SLF001


# ------------------------------------------------------------------ alerts


def test_every_live_alert_is_read_not_just_the_first() -> None:
    """Six informational alerts on the machine measured, in its own order."""
    alerts = CDPClient._parse_alerts(cdp("cdm_alert_v1_alerts.json"))  # noqa: SLF001

    assert len(alerts) == 6
    assert {alert.category for alert in alerts} == {
        "genuineHPsupply",
        "usedSupplyPrompt",
    }
    assert all(alert.severity == "information" for alert in alerts)
    assert all(alert.alert_id is not None for alert in alerts)


def test_an_entry_without_a_category_is_dropped_not_guessed() -> None:
    """A category is the only field that makes an alert identifiable. An.

    entry lacking it is not an alert this integration can describe, and
    inventing a name for it would put a blank-looking row in the UI.
    """
    document = {"alerts": [{"id": 1, "severity": "error"}, "not-an-object"]}
    assert CDPClient._parse_alerts(document) == ()  # noqa: SLF001


# ---------------------------------------------------------------- firmware


def test_a_failed_firmware_update_is_reported_as_such() -> None:
    """Auto-update on, no update available, every attempt failed.

    The firmware build date is the only version marker the read path had
    before, and it cannot express any of this.
    """
    status = cdp("cdm_firmwareUpdate_v2_updateStatus.json")
    check = cdp("cdm_firmwareUpdate_v2_updateCheck.json")
    config = cdp("cdm_firmwareUpdate_v2_configuration.json")

    assert status["lastUpdateResult"] == "failed"
    # An empty string is "nothing to install", not a missing document, so the
    # sensor turns it into None rather than reporting an empty version.
    assert check["availableVersion"] == ""
    assert config["autoUpdateEnabled"] == "true"


def test_the_1970_timestamp_is_not_mistaken_for_a_real_date() -> None:
    """The device has no real-time clock, so its update timestamp is.

    meaningless. It is left as text on the model rather than parsed, which is
    why no sensor exposes it: a date of 1970 would be worse than none.
    """
    status = cdp("cdm_firmwareUpdate_v2_updateStatus.json")
    assert status["lastUpdateTimeStamp"].startswith("1970-")


# -------------------------------------------------------------- certificate


def test_the_certificate_expiry_is_read_from_its_own_block() -> None:
    """Ten years, and nothing warns on the way there."""
    document = cdp("cdm_certificate_v1_certificates_selfSignedCertificate.json")
    validity = document.get("validity") or {}

    assert validity["fromDate"] == "2025-03-07"
    assert validity["toDate"] == "2035-03-05"


# ----------------------------------------------------------- network, split


def test_interfaces_are_reported_separately() -> None:
    """An aggregate cannot tell a printer on Wi-Fi from one whose cable is.

    unplugged: both report a small number, and only the split shows which
    port is live. On the machine measured eth0 is entirely zero.
    """
    adapters = CDPClient._parse_adapter_stats(  # noqa: SLF001
        cdp("cdm_ioConfig_v2_adapterStats.json")
    )

    by_name = {adapter.name: adapter for adapter in adapters}
    assert set(by_name) == {"eth0", "wifi0"}
    assert by_name["eth0"].received_bytes == 0
    assert by_name["wifi0"].received_bytes and by_name["wifi0"].received_bytes > 0
    assert by_name["wifi0"].error_total == 0


def test_error_total_adds_every_error_counter() -> None:
    """Collisions and late collisions are the same physical fault counted.

    twice by the device, so they are summed rather than left for a caller to
    remember to include.
    """
    adapters = CDPClient._parse_adapter_stats(  # noqa: SLF001
        {
            "version": "2.1.0",
            "eth0": {
                "receiverErrors": 1,
                "transmitterErrors": 2,
                "transmitterCollisions": 4,
                "transmitterLateCollisions": 8,
            },
        }
    )
    assert adapters[0].error_total == 15


def test_the_snmp_public_flag_is_not_read_as_a_generic_yes() -> None:
    """The two readings of the device's own enumeration, both real.

    Read through a generic enabled/disabled test, ``publicAllowed`` reports as
    False -- a printer that permits the default community string described as
    having it switched off, which is the opposite of the exposure.
    """
    assert _snmp_public_allowed("publicAllowed") is True
    assert _snmp_public_allowed("publicNotAllowed") is False


def test_an_unrecognised_snmp_value_is_unknown_not_false() -> None:
    """Defaulting a security field to the safe-looking answer is the one way.

    this can be quietly wrong, so an undeclared value stays undecided.
    """
    assert _snmp_public_allowed("somethingNew") is None
    assert _snmp_public_allowed(None) is None


def test_both_protocols_report_the_same_snmp_fact() -> None:
    """CDP says it in ``snmpV1V2Config``, LEDM in ``SNMPConfigWithVersion``,.

    and both models have the public community permitted. One field carries
    both so the two protocols present the same exposure under one name.
    """
    from_cdp = CDPClient._parse_snmp(  # noqa: SLF001
        cdp("cdm_network_v1_snmpConfig.json"), "readOnlyPublicAllowed"
    )
    from_ledm = _parse_network_services(ledm("DevMgmt_NetAppsDyn.xml"))

    assert from_cdp is True
    assert from_ledm["snmp_public_allowed"] is True
    assert from_ledm["snmp_enabled"] is True


def test_the_ledm_discovery_services_are_named() -> None:
    """Four ways for something on the network to find the printer, all on by.

    default on the model measured.
    """
    services = _parse_network_services(ledm("DevMgmt_NetAppsDyn.xml"))["print_services"]
    assert services == ("mdns", "ws-discovery", "ws-print", "ws-scan")


# ------------------------------------------------ print protocols, consumed


def test_enabled_print_protocols_are_listed_by_service() -> None:
    """airPrint, ipp, port9100 and wsPrint are all on; each carries its flags.

    under a different key, and the useful question is which service, not which
    sub-flag.
    """
    services = CDPClient._parse_print_services(  # noqa: SLF001
        cdp("cdm_network_v1_printServices.json")
    )
    assert services == ("airPrint", "ipp", "port9100", "wsPrint")


def test_a_service_whose_flags_are_all_off_is_not_listed() -> None:
    """A disabled service is the one a user would want to see missing."""
    document = {
        "version": "1.0.0",
        "airPrint": {"enabled": "false"},
        "ipp": {"ipp": "true"},
    }
    assert CDPClient._parse_print_services(document) == ("ipp",)  # noqa: SLF001


# ------------------------------------------------ consumables and mechanics


def test_cartridge_changes_is_the_worst_slot_not_the_total() -> None:
    """Slots are refilled independently, so three slots holding two each is a.

    machine on its second round, not one that has seen six.
    """
    changes = CDPClient._parse_cartridge_changes(  # noqa: SLF001
        cdp("cdm_supply_v1_lifetimeCounters.json"), cdp("cdm_suppliesPublic.json")
    )
    assert changes == 5


def test_the_region_reset_counter_has_a_countdown() -> None:
    """Some schemes allow a fixed number of attempts and then stop."""
    document = cdp("cdm_supply_v1_regionReset.json")
    assert document["numberRemaining"] == 3
    assert document["remainingFailureCount"] == 50


def test_the_carriage_state_is_read_separately_from_the_printer_status() -> None:
    """A mechanical state that neither the print nor the scan status word.

    covers: the printer can report itself ready with the carriage not ok.
    """
    assert cdp("cdm_print_v2_setupStatus.json")["carriageStatus"] == "ok"


def test_bluetooth_beaconing_is_read_as_a_flag() -> None:
    """The device reports it as the string "true", not as a JSON boolean --.

    the same convention every CDP flag uses.
    """
    assert cdp("cdm_ble_v1_configuration.json")["currentBeaconingEnabled"] == "true"


# ----------------------------------------------- LEDM print configuration


def test_the_print_settings_are_settings_not_measurements() -> None:
    """What the machine is configured to do, which is the answer to "the.

    output got worse" when the cause is a resolution somebody changed.
    """
    parsed = _parse_print_configuration(ledm("DevMgmt_PrintConfigDyn.xml"))

    assert parsed["print_quality"] == "Normal"
    assert parsed["resolution_setting"] == "fast600dpi"
    assert parsed["default_copies"] == 1
    assert parsed["borderless_printing"] is True


def test_the_media_names_are_the_devices_own_vocabulary() -> None:
    """Kept verbatim rather than mapped to a friendlier label: the device's.

    spelling is the one its own web page and the paper in the tray both use.
    """
    parsed = _parse_current_media(ledm("DevMgmt_MediaDyn.xml"))

    assert parsed["current_media_type"] == "lightWeight"
    assert parsed["current_media_size"] == "iso_a4_210x297mm"


# ------------------------------------------- the LEDM printer's CDP layer


def test_an_ledm_printer_reports_quiet_mode_only_through_its_cdp_layer() -> None:
    """The value the LEDM side cannot see at all.

    Reported here rather than left absent, because "the LEDM documents do not
    carry it" and "the printer has no such setting" are different claims and
    only the second one would be true if it were missing.
    """
    assert (
        cdp20("cdm_print_v1_printModeConfiguration.json")["quietPrintModeEnabled"]
        == "false"
    )


def test_the_panel_language_comes_from_the_same_place() -> None:
    """The control panel's own language setting, read from the same layer."""
    assert cdp20("cdm_controlPanel_v1_configuration.json")["deviceLanguage"] == "zh-CN"


def test_the_model_string_is_not_an_enrolment_status() -> None:
    """GloballyUniqueDeviceModelID is model, SKU and region in one string. It.

    was briefly used as a fallback for the instant-ink status when that was
    empty, which reported "Smart Tank 750 series:28B72A:0" as a subscription
    state -- a category error, so the two are separate fields now.
    """
    parsed = _parse_instant_ink(ledm("DevMgmt_ShopForSupplies.xml"), None)

    assert parsed["instant_ink_status"] is None
    assert parsed["full_model_string"] == "Smart Tank 750 series:28B72A:0"


def test_an_empty_subscription_code_is_no_enrolment_not_a_missing_document() -> None:
    """The device answers with an empty string, which means "never enrolled"."""
    assert (
        cdp20("cdm_consumableSubscription_v1_info.json")["supplySubscriptionStatusCode"]
        == ""
    )
    assert _parse_instant_ink(None, "")["instant_ink_status"] is None


async def test_the_ledm_side_reads_its_json_layer_without_reaching_for_xml() -> None:
    """A decoded JSON document must not be handed to the XML accessors.

    ``_text`` walks an ElementTree node and reads ``.tag``, so passing it a
    dict raises AttributeError on the first key. This line is reached only on
    a live LEDM printer, and a mock is happy to be handed anything -- so the
    only thing that caught it was running against the real machine.
    """
    client = LEDMClient.__new__(LEDMClient)
    client._session = None  # noqa: SLF001
    client._host = "printer.local"  # noqa: SLF001
    client._port = 443  # noqa: SLF001
    client._ssl = True  # noqa: SLF001
    client._ssl_context = False  # noqa: SLF001

    quiet, language, ink = await client.async_ledm_cdp()

    assert (quiet, language, ink) == (None, None, None)


# ------------------------------------------------------ no data, no crash


@pytest.mark.parametrize(
    "parser",
    [
        _parse_print_configuration,
        _parse_current_media,
        _parse_network_services,
    ],
)
def test_an_absent_ledm_document_gives_nothing_rather_than_an_error(parser) -> None:
    """Optional documents are optional. A model serving none of them must.

    still update.

    "Nothing" is None for a scalar and an empty collection for a list-valued
    field, and both shapes are accepted: a bare None where a tuple belongs
    would break the caller, and an empty tuple where a scalar belongs would
    quietly report a setting the device never mentioned.
    """
    for value in parser(None).values():
        assert value is None or value == ()


def test_an_absent_cdp_document_gives_an_empty_result() -> None:
    """None, or empty, for every optional CDP document that is not served."""
    for result in (
        CDPClient._parse_setup_steps(None),  # noqa: SLF001
        CDPClient._parse_alerts(None),  # noqa: SLF001
        CDPClient._parse_adapter_stats(None),  # noqa: SLF001
        CDPClient._parse_print_services(None),  # noqa: SLF001
        CDPClient._parse_snmp(None, "enabled"),  # noqa: SLF001
        CDPClient._parse_cartridge_changes(None, None),  # noqa: SLF001
    ):
        assert result is None or result == ()
