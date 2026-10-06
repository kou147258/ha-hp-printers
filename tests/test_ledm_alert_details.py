"""The detail block nested inside every LEDM alert.

An LEDM alert is not a flat record. The device puts the identity of the
problem one element down::

    <Alert>
      <ProductStatusAlertID>genuineHP</ProductStatusAlertID>
      <Severity>Info</Severity>
      <AlertPriority>400</AlertPriority>
      <AlertDetails>
        <AlertDetailsMarkerColor>Magenta</AlertDetailsMarkerColor>
        <AlertDetailsConsumableTypeEnum>inkTank</AlertDetailsConsumableTypeEnum>
        <AlertDetailsMarkerLocation>3</AlertDetailsMarkerLocation>
        <AlertDetailsUserAction>acknowledgeConsumableState</AlertDetailsUserAction>
      </AlertDetails>
    </Alert>

The consequence is not cosmetic. ``genuineHP`` is the alert every one of
these printers raises most often, and without the detail it is a complaint
with no subject: no colour, no part, nothing to act on.

Worth being precise about why it went unread, because the obvious explanation
is wrong. ``_text`` resolves a name with ``.//``, so it reaches descendants at
any depth and would have found ``AlertDetailsMarkerColor`` under the nested
block without complaint. The fields were missing because ``ActiveAlert`` was
built from five names and none of them was one of these -- nobody asked, so
nothing was read.
"""

from pathlib import Path

from defusedxml import ElementTree as DefusedET
import pytest

from custom_components.hp_printers.api import _parse_ledm_alerts, _strip_namespaces
from custom_components.hp_printers.models import PrinterData
from custom_components.hp_printers.sensor import PRINTER_SENSORS

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "st750-ledm"


def _status_document() -> DefusedET.Element:
    path = FIXTURES / "DevMgmt_ProductStatusDyn.xml"
    if not path.exists():
        pytest.skip("status document not captured")
    return _strip_namespaces(DefusedET.fromstring(path.read_text(encoding="utf-8")))


# ------------------------------------------------------------- the real alerts


def test_the_fixture_really_carries_the_detail_block() -> None:
    """The tests below read it; this checks that it is there.

    A parser tested against a hand-written alert would pass on a document the
    printer never sends, and the nesting is the entire point of the change.
    Checked structurally rather than by counting on the text: the namespace
    prefixes in a captured file are an artefact of how it was written, and an
    assertion anchored to them fails on a re-capture that renames ``ns1``.
    """
    document = _status_document()
    alerts = [e for e in document.iter() if e.tag == "Alert"]

    assert alerts, "the captured document carries no alert at all"
    assert all(any(child.tag == "AlertDetails" for child in a) for a in alerts), (
        "every alert the models measured nests a detail block"
    )
    assert any(
        any(c.tag == "AlertDetailsMarkerColor" for c in a.iter()) for a in alerts
    ), "and at least one fills in a colour"

    text = (FIXTURES / "DevMgmt_ProductStatusDyn.xml").read_text(encoding="utf-8")
    # No serial or hostname may survive into the repository.
    assert "CN412171R6" not in text
    assert "644ED730966B" not in text


def test_the_colour_is_read_from_the_nested_block() -> None:
    """The specific gap: a supply alert with no colour attached.

    The mutation this has to catch is the one that actually happened: the
    field being absent from the parse, not the walk being unable to reach it.
    """
    alerts = _parse_ledm_alerts(_status_document())

    assert alerts
    assert any(a.marker_color for a in alerts), (
        "every alert the models measured carries a marker colour; reading none "
        "means the walk is still flat"
    )
    assert "Magenta" in {a.marker_color for a in alerts}


def test_the_whole_detail_block_is_read_not_just_the_colour() -> None:
    """Three of the four fields came from the same block, so all four must."""
    alert = next(
        (a for a in _parse_ledm_alerts(_status_document()) if a.marker_color), None
    )

    assert alert is not None
    assert alert.consumable_type == "inkTank"
    assert alert.marker_location == "3"
    assert alert.user_action == "acknowledgeConsumableState"


def test_the_resource_uri_points_at_the_document_the_detail_lives_in() -> None:
    """So "go look at that" is one request away rather than a search."""
    alert = next(
        (a for a in _parse_ledm_alerts(_status_document()) if a.marker_color), None
    )

    assert alert is not None
    assert alert.resource_uri == "/DevMgmt/ConsumableConfigDyn.xml"


def test_the_severity_and_priority_still_read_as_before() -> None:
    """Regression: the nesting change must not have displaced the flat fields.

    ``Info`` is LEDM's own word where CDP says ``information``; the fold to
    lowercase is what lets one entity description serve both protocols.
    """
    alert = next(
        (a for a in _parse_ledm_alerts(_status_document()) if a.marker_color), None
    )

    assert alert is not None
    assert alert.category == "genuineHP"
    assert alert.severity == "info"
    assert alert.priority == 400
    assert alert.sequence is not None


def test_every_alert_in_the_table_is_still_parsed() -> None:
    """Adding detail must not have turned into "parse only the good ones"."""
    document = _status_document()
    expected = len([e for e in document.iter() if e.tag == "Alert"])

    assert len(_parse_ledm_alerts(document)) == expected
    assert expected >= 3


# --------------------------------------------------------------- the None paths


def test_a_detail_that_does_not_apply_is_absent_rather_than_empty() -> None:
    """A jam alert has no marker colour, and a colour alert no jam location.

    Filled in from the outside rather than read from a document: what is being
    checked is that a missing field stays None, and a document the printer
    sends would not contain the case.
    """
    document = DefusedET.fromstring(
        "<ProductStatusDyn><Status><StatusCategory>ready</StatusCategory></Status>"
        "<AlertTable><Alert>"
        "<ProductStatusAlertID>carriageJam</ProductStatusAlertID>"
        "<Severity>Error</Severity><AlertPriority>100</AlertPriority>"
        "<AlertDetails><AlertDetailsJamLocation>inputTray</AlertDetailsJamLocation>"
        "</AlertDetails>"
        "</Alert></AlertTable></ProductStatusDyn>"
    )

    alert = _parse_ledm_alerts(document)[0]

    assert alert.category == "carriageJam"
    assert alert.severity == "error"
    assert alert.marker_color is None
    assert alert.user_action is None
    assert alert.resource_uri is None


def test_a_document_with_no_alert_table_yields_nothing() -> None:
    """Absent and empty are the same answer, and neither is an error."""
    assert _parse_ledm_alerts(None) == []
    assert _parse_ledm_alerts(DefusedET.fromstring("<ProductStatusDyn/>")) == []


# ------------------------------------------------------------- what HA shows


def test_the_alert_attributes_expose_the_detail() -> None:
    """The parse is not the deliverable; the entity carrying it is."""
    description = next(d for d in PRINTER_SENSORS if d.key == "active_alert_count")
    data = PrinterData(
        active_alerts=tuple(_parse_ledm_alerts(_status_document())),
    )

    assert description.value_fn(data, None) == len(data.active_alerts)
    attributes = description.attrs_fn(data)
    first = attributes["alerts"][0]

    assert "marker_color" in first
    assert "user_action" in first
    assert attributes["marker_colors"], "the aggregated colour list is populated"


def test_the_colour_list_is_empty_rather_than_absent_when_no_alert_has_one() -> None:
    """A template iterating it should get nothing, not a missing key."""
    description = next(d for d in PRINTER_SENSORS if d.key == "active_alert_count")
    data = PrinterData(active_alerts=())
    description.value_fn(data, None)

    assert description.attrs_fn(data)["marker_colors"] == []
