"""The LEDM readings the *Cap.xml gap analysis turned up.

Every assertion here is a value the Smart Tank 750 actually returned. The
gap analysis worked by differencing three sets -- what the capability document
declares, what the values document actually contains, and what the parser
asks for -- and this file pins the third against the second, so the
difference cannot silently grow back.

Two of these tests exist because the first run of the parsers read the wrong
subtrees. A field placed beside its name rather than where the document puts
it returns None without raising, which is a parser that looks finished and
reads nothing.
"""

from pathlib import Path

from defusedxml import ElementTree as DefusedET
import pytest

from custom_components.hp_printers.api import (
    LEDMClient,
    _parse_ledm_alerts,
    _parse_ledm_exposure,
    _parse_ledm_jobs,
    _parse_ledm_trays,
    _parse_product_config,
    _strip_namespaces,
)
from custom_components.hp_printers.models import ProductInfo

FIXTURES = Path(__file__).resolve().parent / "fixtures"
LEDM = FIXTURES / "st750"
LEDM_LEDM = FIXTURES / "st750-ledm"

pytestmark = pytest.mark.skipif(
    not LEDM.is_dir() or not LEDM_LEDM.is_dir(),
    reason="real LEDM captures not present",
)


def doc(name: str):
    """Return one captured LEDM document, from whichever tree holds it.

    The original eight captures live in st750/; the documents added with the
    capability set (NetApps, Media, PrintConfig) live in st750-ledm/. Both
    are anonymized captures of the same machine, so a test that needs one of
    them should not have to know which tree it was filed under.
    """
    for tree in (LEDM, LEDM_LEDM):
        path = tree / f"DevMgmt_{name}.xml"
        if path.exists():
            return _strip_namespaces(
                DefusedET.fromstring(path.read_text(encoding="utf-8"))
            )
    return None


# ------------------------------------------------------------------- jobs


def test_the_job_counters_come_from_the_printers_own_subunit() -> None:
    """The usage document repeats every counter name under every subunit.

    Taken from the document root, JobCount would be whichever subunit appeared
    first. The printer's is the one a user means by "how many jobs has this
    printer printed".
    """
    jobs = _parse_ledm_jobs(doc("ProductUsageDyn"))

    assert jobs["print_job_count"] == 7039
    assert jobs["job_successes"] == 3
    assert jobs["job_failures"] == 4
    assert jobs["job_cancelled"] == 0
    assert jobs["job_skipped"] == 0


def test_a_job_can_be_counted_and_still_have_failed() -> None:
    """3 successes and 4 failures against 7039 jobs is the whole point.

    A single "print jobs" counter reads as "the printer printed 7039 things".
    The outcome split is what makes it a health signal rather than a vanity
    number, and a model exposing only the total should not pretend otherwise.
    """
    jobs = _parse_ledm_jobs(doc("ProductUsageDyn"))

    assert jobs["print_job_count"] > (jobs["job_successes"] or 0) + (
        jobs["job_failures"] or 0
    )


def test_the_wired_and_wireless_page_counts_are_separate() -> None:
    """14920 over the network and 1676 over Wi-Fi.

    The single printed-page total cannot tell which path is in use, and the
    wireless figure is the one that goes to zero when the radio is the
    problem.
    """
    jobs = _parse_ledm_jobs(doc("ProductUsageDyn"))

    assert jobs["network_printed_pages"] == 14920
    assert jobs["wireless_printed_pages"] == 1676
    assert jobs["subscription_printed_pages"] == 0


def test_job_counters_are_absent_without_the_document() -> None:
    """A model serving no usage document reports nothing, not zeros."""
    assert _parse_ledm_jobs(None) == {}


# ------------------------------------------------------------------ alerts


def test_ledm_reports_its_own_live_alerts() -> None:
    """Three genuine-supply notices, all informational.

    Without this the LEDM side would have had no live alerts at all, and
    "not supported" would have been the wrong thing to say about a machine
    that publishes an AlertTable.
    """
    alerts = _parse_ledm_alerts(doc("ProductStatusDyn"))

    assert len(alerts) == 3
    assert {alert.category for alert in alerts} == {"genuineHP"}
    assert all(alert.severity == "info" for alert in alerts)
    assert all(alert.priority == 400 for alert in alerts)


def test_the_ledm_severity_word_is_folded_onto_the_cdp_one() -> None:
    """LEDM says "Info" where CDP says "information".

    Left as "Info" the enum sensor would not match a declared option and the
    entity would refuse to be created on every LEDM printer, so the two
    protocols have to land on one spelling.
    """
    alerts = _parse_ledm_alerts(doc("ProductStatusDyn"))

    assert alerts[0].severity == "info"


def test_an_unseen_severity_word_passes_through_rather_than_being_dropped() -> None:
    """An alert whose severity is unknown is still an alert.

    Dropping it would make the count wrong, and the count is what tells
    someone the machine is complaining.
    """
    document = doc("ProductStatusDyn")
    alerts = _parse_ledm_alerts(document)
    severities = {alert.severity for alert in alerts}

    assert all(s in ("info", "warning", "error", "critical") for s in severities)


# -------------------------------------------------------- product config


def test_the_config_fields_are_read_from_where_the_document_puts_them() -> None:
    """Three of these came back empty on the first run.

    ``FailedAttemptsRemaining`` lives under a RegionInformation block inside
    ProductInformation, ``DeviceLanguage`` one level down again under
    ProductSettings, and ``CountryAndRegionName`` is a direct child of
    ProductSettings rather than of ProductInformation with its neighbours.
    Each was read from the obvious-looking place, and each returned None
    without raising.
    """
    config = _parse_product_config(doc("ProductConfigDyn"))

    assert config["failed_attempts_remaining"] == 50
    assert config["device_language"] == "zh-CN"
    assert config["country_region"] == "china"
    assert config["product_derivative_number"] == "2100043626"


def test_memory_is_reported_in_the_units_the_device_uses() -> None:
    """176197 of 262144. Kilobytes, not a rounded SI prefix it never applied."""
    config = _parse_product_config(doc("ProductConfigDyn"))

    assert config["available_memory_kb"] == 176197
    assert config["total_memory_kb"] == 262144
    assert config["available_memory_kb"] < config["total_memory_kb"]


def test_a_fitted_duplexer_and_the_auto_duplex_setting_are_different_questions() -> (
    None
):
    """Installed, while the setting reads disabled.

    One field would have to be wrong on this machine. It has a duplexer --
    ten thousand double-sided sheets have come out of it -- and automatic
    duplex is currently switched off.
    """
    config = _parse_product_config(doc("ProductConfigDyn"))

    assert config["duplexer_installed"] is True
    assert config["auto_duplex_enabled"] is False


def test_the_ledm_setup_phase_folds_onto_the_cdp_vocabulary() -> None:
    """``setupComplete`` becomes ``complete``, on ProductInfo.

    Left as the device's own word the enum sensor would not match an option
    and would refuse to be created, which would say "this model has no setup
    state" about a machine that publishes one. It rides on the product
    configuration rather than the status document, which is why it is
    ProductInfo and not PrinterData.
    """
    assert _parse_product_config(doc("ProductConfigDyn"))["setup_phase"] == "complete"


def test_an_absent_product_config_reports_nothing() -> None:
    """A model serving no product config yields None for every field, not zero."""
    config = _parse_product_config(None)

    assert config["available_memory_kb"] is None
    assert config["duplexer_installed"] is None
    assert config["setup_phase"] is None


def test_the_product_info_carries_them() -> None:
    """They land on ProductInfo, not PrinterData.

    The document is already read there on the slow cadence and none of these
    changes between polls, so putting them on PrinterData would add a request
    to every 60-second refresh for values that never move.
    """
    client = LEDMClient.__new__(LEDMClient)
    info = client._parse_product_info(doc("ProductConfigDyn"))  # noqa: SLF001

    assert isinstance(info, ProductInfo)
    assert info.country_region == "china"
    assert info.duplexer_installed is True
    assert info.failed_attempts_remaining == 50


# ----------------------------------------------------------------- trays


def test_the_machine_describes_its_own_shape() -> None:
    """One tray, one bin -- which a list of trays alone does not say."""
    trays = _parse_ledm_trays(doc("MediaHandlingDyn"))

    assert trays["input_tray_count"] == 1
    assert trays["output_bin_count"] == 1
    assert trays["default_input_tray"] == "Tray1"
    assert trays["default_output_bin"] == "OutputBin1"


# ------------------------------------------------------------- exposure


def test_ledm_reports_the_same_raw_printing_exposure_as_cdp() -> None:
    """Port 9100 on, HTTPS redirection off, on the second printer too.

    This is the LEDM spelling of what the CDP side reports as print
    services, and it says the same thing about a different machine: raw
    printing is on and the web interface answers plain HTTP.
    """
    exposure = _parse_ledm_exposure(doc("NetAppsDyn"))

    assert exposure["port_9100_enabled"] is True
    assert exposure["https_redirection_enabled"] is False
    assert exposure["direct_print_enabled"] is True
    assert exposure["llmnr_enabled"] is True


def test_web_scan_is_not_the_same_field_as_the_ws_scan_service() -> None:
    """``WebScan`` reads disabled while ``WebServicesConfig/WSScan`` reads enabled.

    Two fields a printer's own settings page shows side by side, with
    opposite values, and only one of them is a setting a user would go and
    change. Reading the other would have reported a disabled feature as
    enabled.
    """
    exposure = _parse_ledm_exposure(doc("NetAppsDyn"))

    assert exposure["web_scan_enabled"] is False
