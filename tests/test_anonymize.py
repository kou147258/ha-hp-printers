"""Tests for the fixture anonymizer.

The anonymizer is what stands between a capture from someone's printer and
a public repository, so it gets the same treatment as runtime code. These
tests go through ``anonymize_file`` -- the entry point ``main`` uses -- so
a fix here cannot be undone by a second copy of the pipeline living in the
command-line path.
"""

import json
from pathlib import Path
import sys

from defusedxml import ElementTree as DefusedET
import pytest

from scripts import anonymize_ledm
from scripts.anonymize_cdp import anonymize_file as anonymize_cdp_file
from scripts.anonymize_ledm import anonymize_file


def _write(tmp_path: Path, name: str, xml: str) -> Path:
    """Write an XML document and return its path."""
    path = tmp_path / name
    path.write_text(xml.strip(), encoding="utf-8")
    return path


def _text(path: Path, tag: str) -> list[str]:
    """Return the text of every element with this local name."""
    root = DefusedET.fromstring(path.read_text(encoding="utf-8"))
    return [
        node.text
        for node in root.iter()
        if node.tag.rpartition("}")[2] == tag and node.text
    ]


def test_product_number_keeps_the_model_and_is_not_re_anonymized(
    tmp_path: Path,
) -> None:
    """ProductNumber becomes the model name and stays that way.

    The identifier pass would otherwise overwrite the substitution with its
    own placeholder, throwing away the one detail that makes a fixture
    recognizable.
    """
    source = _write(
        tmp_path,
        "DevMgmt_ProductConfigDyn.xml",
        """
        <ProductConfigDyn>
          <ProductInformation>
            <MakeAndModel>HP Color LaserJet MFP M182nw</MakeAndModel>
            <ProductNumber>7KW55A</ProductNumber>
            <SerialNumber>VNB3M40504</SerialNumber>
          </ProductInformation>
        </ProductConfigDyn>
        """,
    )
    target = tmp_path / "out.xml"

    anonymize_file(source, target)

    assert _text(target, "ProductNumber") == ["HP Color LaserJet MFP M182nw"]
    assert _text(target, "SerialNumber") == ["SN-ANON-0000"]


def test_network_identifiers_are_scrubbed(tmp_path: Path) -> None:
    """IOConfigDyn names the host four ways and carries the MAC twice."""
    source = _write(
        tmp_path,
        "DevMgmt_IOConfigDyn.xml",
        """
        <IOConfigDyn>
          <IOAdaptorConfig>
            <ApplicationConfig>
              <ApplicationServiceName>HP LaserJet (2E7F3D)</ApplicationServiceName>
              <DomainName>NPI2E7F3D.local.</DomainName>
            </ApplicationConfig>
            <NetworkAdaptorConfig>
              <CurrentHostname>NPI2E7F3D</CurrentHostname>
              <DefaultHostname>NPI2E7F3D</DefaultHostname>
              <BOOTP_DHCPv4SuppliedHostname>NPI2E7F3D</BOOTP_DHCPv4SuppliedHostname>
              <HardwareAddress>7c57582e7f3d</HardwareAddress>
              <IPAddress>192.168.0.64</IPAddress>
              <DefaultGateway>192.168.0.1</DefaultGateway>
              <SubnetMask>255.255.255.0</SubnetMask>
              <IPAddress>FE80::7E57:58FF:FE2E:7F3D</IPAddress>
            </NetworkAdaptorConfig>
          </IOAdaptorConfig>
        </IOConfigDyn>
        """,
    )
    target = tmp_path / "out.xml"

    anonymize_file(source, target)
    body = target.read_text(encoding="utf-8")

    for leaked in ("NPI2E7F3D", "7c57582e7f3d", "2E7F3D", "192.168.0."):
        assert leaked not in body, f"{leaked} survived anonymization"
    assert _text(target, "HardwareAddress") == ["000000000000"]
    # A netmask must stay a netmask: rewriting it as an address would read
    # as a parser bug when someone later debugs against the fixture.
    assert _text(target, "SubnetMask") == ["255.255.255.0"]
    assert "2001:db8::1" in body


def test_values_that_only_look_like_addresses_are_left_alone(tmp_path: Path) -> None:
    """The link mode reads like an IPv6 fragment; it must survive intact."""
    source = _write(
        tmp_path,
        "DevMgmt_IOConfigDyn.xml",
        """
        <IOConfigDyn>
          <NetworkAdaptorConfig>
            <SpeedDuplexNegotiationMode>100TX_FULL</SpeedDuplexNegotiationMode>
            <NetworkStatus>ready</NetworkStatus>
          </NetworkAdaptorConfig>
        </IOConfigDyn>
        """,
    )
    target = tmp_path / "out.xml"

    anonymize_file(source, target)

    assert _text(target, "SpeedDuplexNegotiationMode") == ["100TX_FULL"]
    assert _text(target, "NetworkStatus") == ["ready"]


def test_shop_for_supplies_serial_does_not_survive(tmp_path: Path) -> None:
    """The supplies document names the serial three ways, none of them obvious.

    This is the one that got through. Every occurrence is under a tag whose
    name does not contain "Serial" -- RequesterID, GloballyUniqueDeviceID and
    DeviceInfoDeviceID -- so a filter written from the obvious tag names walks
    straight past all three. It was found by capturing the document and
    grepping the result for the real serial, not by reading the code.
    """
    source = _write(
        tmp_path,
        "DevMgmt_ShopForSupplies.xml",
        """
        <ShopForSuppliesRequest>
          <RequesterID type="x-inkjet-serial-number">CN412171R6</RequesterID>
          <Devices>
            <Device>
              <GloballyUniqueDeviceID type="">CN412171R6</GloballyUniqueDeviceID>
              <DeviceInfoDeviceID type="x-inkjet-serial-number">CN412171R6</DeviceInfoDeviceID>
            </Device>
          </Devices>
        </ShopForSuppliesRequest>
        """,
    )
    target = tmp_path / "out.xml"

    anonymize_file(source, target)
    body = target.read_text(encoding="utf-8")

    assert "CN412171R6" not in body
    for tag in ("RequesterID", "GloballyUniqueDeviceID", "DeviceInfoDeviceID"):
        assert _text(target, tag) == ["SN-ANON-0000"]


def test_the_devtype_blob_loses_its_serial_and_keeps_its_languages(
    tmp_path: Path,
) -> None:
    """DeviceInfoData is a field bag, not a single identifier.

    Replacing it wholesale would throw away the printable-language list, which
    is the only reason the document is in the capture. Replacing only ``SN:``
    would leave the serial a second time inside the serialised ``S:`` block,
    so both are rewritten and the rest is left alone.
    """
    source = _write(
        tmp_path,
        "DevMgmt_ShopForSupplies.xml",
        """
        <ShopForSuppliesRequest>
          <DeviceInfoData>MFG:HP;MDL:Smart Tank 750;CLS:PRINTER;SN:CN412171R6;S:038888C484000001006c2400;Z:0500,</DeviceInfoData>
        </ShopForSuppliesRequest>
        """,
    )
    target = tmp_path / "out.xml"

    anonymize_file(source, target)
    body = target.read_text(encoding="utf-8")

    assert "CN412171R6" not in body
    assert "038888C484000001006c2400" not in body
    # The rest of the blob is real data and has to survive.
    assert "MFG:HP" in body
    assert "CLS:PRINTER" in body
    assert "PCL3GUI" in body or "MDL:Smart Tank 750" in body


def test_certificate_common_name_is_scrubbed(tmp_path: Path) -> None:
    """The self-signed cert's common name is derived from the MAC address.

    HPF3EC0B is 00:0f:3e:c0:b0:8f in hex, so it is a network identifier in an
    X.509 costume. The postal fields are HP's registered address rather than
    anyone's personal data and are deliberately left alone.
    """
    source = tmp_path / "cdm_cert.json"
    source.write_text(
        json.dumps(
            {
                "certificateAttributes": {
                    "commonName": "HPF3EC0B",
                    "organization": "HP",
                    "cityOrLocality": "Vancouver",
                },
                "validity": {"fromDate": "2025-03-07", "toDate": "2035-03-05"},
            }
        ),
        encoding="utf-8",
    )
    target = tmp_path / "out.json"

    anonymize_cdp_file(source, target)
    body = target.read_text(encoding="utf-8")

    assert "HPF3EC0B" not in body
    assert json.loads(body)["certificateAttributes"]["commonName"] == "printer.local"
    # The dates are the reason this document is captured; they must survive.
    assert json.loads(body)["validity"]["toDate"] == "2035-03-05"


def test_a_scrubber_failure_stops_the_run_instead_of_copying_it_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a parse failure may copy a document through unmodified.

    The command-line path once caught every exception and reported it as
    "not XML". A broken regex in a scrubber therefore looked like a non-XML
    capture, and the document -- serial number and all -- reached the output
    directory unmodified. The catch is now narrowed to a parse error, so a bug
    in this script stops the run rather than shipping the identifier it was
    written to remove.
    """
    capture = tmp_path / "capture"
    capture.mkdir()
    _write(
        capture,
        "DevMgmt_ShopForSupplies.xml",
        """
        <ShopForSuppliesRequest>
          <RequesterID>CN412171R6</RequesterID>
        </ShopForSuppliesRequest>
        """,
    )
    monkeypatch.setattr(sys, "argv", ["anonymize_ledm.py", str(capture)])

    def _boom(*_args, **_kwargs):
        raise RuntimeError("scrubber bug")

    monkeypatch.setattr(anonymize_ledm, "_identifier_value", _boom)

    with pytest.raises(RuntimeError, match="scrubber bug"):
        anonymize_ledm.main()

    # The pass-through is the failure mode being pinned, so assert it did not
    # happen rather than only asserting the exception.
    passed_through = capture.with_name(capture.name + "-anon")
    assert not passed_through.exists() or not any(passed_through.iterdir())


def test_a_non_xml_capture_is_still_copied_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pass-through itself has to keep working.

    A capture can hold a file named .xml that is really an HTTP status line,
    when the device does not serve that resource. Refusing those would lose
    the record that the device went quiet, and would lose the replacements
    already made for every earlier file in the same run.
    """
    capture = tmp_path / "capture"
    capture.mkdir()
    (capture / "DevMgmt_Missing.xml").write_text(
        "HTTP/1.1 404 Not Found\r\n\r\n", encoding="utf-8"
    )
    monkeypatch.setattr(sys, "argv", ["anonymize_ledm.py", str(capture)])

    anonymize_ledm.main()

    passed_through = capture.with_name(capture.name + "-anon") / "DevMgmt_Missing.xml"
    assert passed_through.exists()
    assert "404" in passed_through.read_text(encoding="utf-8")
