"""What the printhead has actually ejected, and what it could not attribute.

The usage document keeps an ink accounting per station, and it is the
printhead's own tally rather than the cartridge's self-report. That
distinction is the whole reason these are worth reading: a clone chip reports
the genuine part number, so a third-party cartridge presents as HP, and the
one place that disagrees is the head that had to fire the ink.

Two things this file is careful about, both of which are ways to be wrong
here rather than merely incomplete:

* The drop counters are totalled across stations, because each station is a
  different set of heads. The pen-stall counters are taken from one station
  only, because the device repeats the identical block under every station --
  summing those would double all eight numbers, and attributing them to a
  cartridge would tell the user their black cartridge has stalled four
  billion times.
* The pen-stall unit is not published, so there is no total anywhere in this
  file and no sensor that presents one.
"""

from pathlib import Path

from defusedxml import ElementTree as DefusedET
import pytest

from custom_components.hp_printers.api import (
    _parse_ledm_jobs,
    _parse_pen_stalls,
    _parse_printhead_drops,
    _parse_product_config,
    _strip_namespaces,
)
from custom_components.hp_printers.models import PrinterData, ProductInfo
from custom_components.hp_printers.sensor import PRINTER_SENSORS

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "st750-ledm"

# The values the machine reported, written out so the aggregation is checked
# against arithmetic rather than against itself. Two stations, so a correct
# total is the sum and a correct stall set is one station's worth.
EXPECTED_HP_DROPS = 58355184 + 57960775 + 63928490 + 266682429


def _strip(text: str) -> DefusedET.Element:
    """Parse XML and strip namespaces, mirroring the live parser."""
    return _strip_namespaces(DefusedET.fromstring(text))


def _usage() -> DefusedET.Element:
    path = FIXTURES / "DevMgmt_ProductUsageDyn.xml"
    if not path.exists():
        pytest.skip("usage document not captured")
    return _strip_namespaces(DefusedET.fromstring(path.read_text(encoding="utf-8")))


# -------------------------------------------------------------- the drop totals


def test_the_drop_counters_are_totalled_across_stations() -> None:
    """Not read from the first station.

    Each station is a different set of heads, so a first-wins read would
    report the colour trio's figure and silently drop the black head's.
    """
    parsed = _parse_printhead_drops(_usage())

    assert parsed["printhead_hp_drops"] == EXPECTED_HP_DROPS


def test_a_zero_is_a_reading_and_not_a_gap() -> None:
    """The non-HP count is zero on this machine, and that is the point.

    A parser that treated 0 as "absent" would drop exactly the field a user
    watches to decide whether their printer has ever been fed third-party
    ink.
    """
    parsed = _parse_printhead_drops(_usage())

    assert parsed["printhead_non_hp_drops"] == 0
    assert parsed["printhead_ooi_drops"] == 0


def test_a_counter_type_the_device_does_not_send_is_absent() -> None:
    """Rather than reported as zero, which is a different claim."""
    parsed = _parse_printhead_drops(_usage())

    # "LDWDropsCount" is a declared type the model does not report. Absent is
    # right; 0 would be a claim the device never made.
    assert "printhead_ldw_drops" not in parsed
    assert parsed["printhead_service_drops"] is not None


def test_a_document_with_no_consumable_subunit_yields_nothing() -> None:
    """Absent is not a set of zeros.

    A printer that reports no ink accounting at all is a different machine
    from one reporting that it has printed no drops, and the None-typed fields
    are what keep the two apart.
    """
    assert _parse_printhead_drops(None) == {}
    assert _parse_printhead_drops(DefusedET.fromstring("<ProductUsageDyn/>")) == {}


# ------------------------------------------------------------------ pen stalls


def test_the_stall_counters_are_one_stations_worth() -> None:
    """The doubling this guards against is invisible in a sum.

    Both stations on the model measured report the same eight values, so
    summing produces a set that is wrong by exactly a factor of two and still
    looks like plausible counters.
    """
    stalls = _parse_pen_stalls(_usage())

    assert len(stalls) == 8
    assert stalls["bank1_zero"] == 3876359005
    assert stalls["bank2_three"] == 3410489852


def test_the_stalls_are_keyed_by_the_devices_own_bank_and_location() -> None:
    """So they can be looked up, without a unit being guessed for them."""
    stalls = _parse_pen_stalls(_usage())

    assert set(stalls) == {
        "bank1_zero",
        "bank1_one",
        "bank1_two",
        "bank1_three",
        "bank2_zero",
        "bank2_one",
        "bank2_two",
        "bank2_three",
    }


def test_one_stations_worth_is_the_rule_even_when_stations_disagree() -> None:
    """The decision is pinned here rather than against the real machine.

    On the model measured both stations report identical values, so "read the
    first" and "merge them" produce identical output and no value-based test
    can tell them apart -- checked, and that is why this test takes this shape.
    The two stations are given different numbers so the rule itself becomes
    observable: the first station wins, because the device is repeating one
    carriage's counters rather than reporting per-cartridge figures.
    """
    document = _strip(
        """
        <ProductUsageDyn><ConsumableSubunit>
          <Consumable><MarkerColor>CyanMagentaYellow</MarkerColor>
            <UsageByPenStall>
              <PenStall><PenStallValue>111</PenStallValue>
                <PenStallValueLocation>zero</PenStallValueLocation></PenStall>
              <PenStallNumber>1</PenStallNumber>
            </UsageByPenStall>
          </Consumable>
          <Consumable><MarkerColor>Black</MarkerColor>
            <UsageByPenStall>
              <PenStall><PenStallValue>999</PenStallValue>
                <PenStallValueLocation>zero</PenStallValueLocation></PenStall>
              <PenStallNumber>1</PenStallNumber>
            </UsageByPenStall>
          </Consumable>
        </ConsumableSubunit></ProductUsageDyn>
        """
    )

    stalls = _parse_pen_stalls(document)

    assert stalls == {"bank1_zero": 111}, (
        "the first station's set is taken verbatim; merging would give 999 "
        "and summing 1110, and neither is a printhead figure"
    )


def test_no_total_stall_value_is_exposed() -> None:
    """HP publishes no unit, so this integration must not produce one.

    A "total pen stall time" would be a number in milliseconds that nobody
    verified, sitting on a dashboard looking authoritative.
    """
    description = next(d for d in PRINTER_SENSORS if d.key == "printhead_non_hp_drops")
    data = PrinterData(pen_stalls=tuple(sorted(_parse_pen_stalls(_usage()).items())))

    attributes = description.attrs_fn(data, ProductInfo())

    assert len(attributes) == 8
    assert not any("total" in key or "time" in key for key in attributes)


def test_no_stalls_is_an_empty_mapping_rather_than_none() -> None:
    """A template iterating it gets nothing, not a missing key."""
    description = next(d for d in PRINTER_SENSORS if d.key == "printhead_non_hp_drops")

    assert description.attrs_fn(PrinterData(), ProductInfo()) == {}


# ------------------------------------------------------------- the other two


def test_the_cloud_and_subscription_counters_are_read_from_their_own_units() -> None:
    """Both come out of the usage document, and neither is the other."""
    parsed = _parse_ledm_jobs(_usage())

    assert parsed["cloud_printed_pages"] == 10
    assert parsed["subscription_printed_pages"] == 0


def test_the_sign_in_budget_is_complete() -> None:
    """Both halves are read from the document, not just declared in the model.

    The first version of this test checked the field existed on ``ProductInfo``
    and stopped there, which passes on a build where the parser never reads it
    -- the same shape of gap the calibration document had yesterday. So this
    drives the parser with a document carrying both counters and checks what
    comes out.
    """
    cap = FIXTURES / "DevMgmt_ProductConfigCap.xml"
    if not cap.exists():
        pytest.skip("capability document not captured")
    declared = {
        el.tag.rpartition("}")[2]
        for el in DefusedET.fromstring(cap.read_text(encoding="utf-8")).iter()
        if el.get("elementXPath")
    }
    assert {"SuccessfulAttemptsRemaining", "FailedAttemptsRemaining"} <= declared

    document = _strip(
        """
        <ProductConfigDyn>
          <ProductInformation>
            <ProductSettings>
              <RegionInformation>
                <FailedAttemptsRemaining>50</FailedAttemptsRemaining>
                <SuccessfulAttemptsRemaining>3</SuccessfulAttemptsRemaining>
              </RegionInformation>
            </ProductSettings>
          </ProductInformation>
        </ProductConfigDyn>
        """
    )

    parsed = _parse_product_config(document)

    assert parsed["successful_attempts_remaining"] == 3
    assert parsed["failed_attempts_remaining"] == 50


def test_the_budget_sensor_reads_the_parsed_field() -> None:
    """The last link, so the entity cannot be right while the data is not."""
    description = next(
        d for d in PRINTER_SENSORS if d.key == "successful_attempts_remaining"
    )
    info = ProductInfo(successful_attempts_remaining=3, failed_attempts_remaining=50)

    assert description.value_fn(PrinterData(), info) == 3
    assert description.value_fn(PrinterData(), ProductInfo()) is None
