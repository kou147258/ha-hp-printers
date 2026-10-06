"""Anonymize a directory of captured HP CDP JSON before it is committed.

Usage:
    ./.venv/bin/python scripts/anonymize_cdp.py scripts/captures/192.168.0.64-20260101T120000Z/

The output directory is created next to the input with the suffix ``-anon``.
The script prints every replacement it makes.

Why a separate script from ``anonymize_ledm.py``
-----------------------------------------------
The LEDM capture is XML and the CDP capture is JSON, so the parsing is
different. The two share a *policy* -- same rules about what identifies a
device and what is worth keeping -- but they do not share an implementation,
and merging them would mean one function with a format branch in every step.
What must not be duplicated is the pipeline within one format: ``main`` here
calls :func:`anonymize_file`, which is the only place the JSON pipeline
exists, for the same reason the XML side has a single entry point.

What it removes
---------------

- Device identity: serial number, device UUID, service ID, SKU, derivative
  number.
- Per-consumable serial numbers.
- User-typed free text: ``deviceDescription`` and ``deviceLocation`` are
  editable from the printer's own web UI, so they can carry a room name or a
  person's name.
- ``productNumber`` is replaced with the captured ``makeAndModel`` so the
  fixture still says what device it came from without carrying the SKU.
- IPv4 / IPv6 literals anywhere in a value, for the same reason the XML
  anonymizer does it: an address in a committed fixture is both an identifier
  and a way for a reader to infer the network.

What it deliberately keeps
--------------------------

Counters, dates, states, capabilities, media names, event codes, and SNMP
*settings* -- ``accessOption``, ``enabled``, ``readOnlyPublicAllowed``. The
community string itself is never returned by the device, only whether one is
set, so there is no credential in this capture to remove.

**This is a best-effort filter over the keys seen so far.** It cannot know
about a field on a model nobody has captured yet. Read the diff before
committing, and cover every new key in ``tests/test_anonymize_cdp.py``.
"""

import argparse
from collections.abc import Callable
import ipaddress
import json
from pathlib import Path
import re

# Keys whose value is an identifier. Values are replacement strings; ``None``
# means "resolve the placeholder from the key name", matching the XML side.
IDENTIFIER_KEYS: dict[str, str | None] = {
    "deviceUuid": "00000000-0000-0000-0000-000000000000",
    "uuid": "00000000-0000-0000-0000-000000000000",
    "serialNumber": "SN-ANON-0000",
    "serviceId": "00000000-0000-0000-0000-000000000000",
    "skuIdentifier": "SKU-ANON-0000",
    "derivativeNumber": "DERIV-ANON-0000",
    # Free text the user can type into the printer's own web UI.
    "deviceLocation": "Test Location",
    "deviceDescription": "Test Device",
    "productNumber": None,  # replaced with the captured makeAndModel
    # The self-signed certificate's common name is derived from the MAC
    # (HPF3EC0B from 00:0f:3e:c0:b0:8f), so it is a network identifier wearing
    # an X.509 costume. The postal fields are HP's own registered address and
    # not personal data, so they are left alone -- over-scrubbing them would
    # make the fixture useless for testing the validity-date parsing without
    # making anything safer.
    "commonName": "printer.local",
}


def _ipv6_placeholder(match: re.Match[str]) -> str:
    """Replace a match only when it really is an IPv6 address."""
    try:
        ipaddress.IPv6Address(match.group(0))
    except ValueError:
        return match.group(0)
    return "2001:db8::1"


TEXT_PATTERNS: tuple[
    tuple[re.Pattern[str], str | Callable[[re.Match[str]], str]], ...
] = (
    (
        re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"),
        "192.0.2.1",
    ),
    (
        re.compile(
            r"(?<![0-9A-Za-z:.])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}"
            r"(?![0-9A-Za-z:.])"
        ),
        _ipv6_placeholder,
    ),
)


def _identifier_value(key: str, configured: str | None) -> str:
    """Resolve the configured value or fall back to a stable placeholder."""
    if configured:
        return configured
    return "ANON-0000"


def _scrub_text(text: str) -> str:
    """Apply the free-text address patterns."""
    for pattern, replacement in TEXT_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _scrub(node: object, replacements: list[tuple[str, str, str]]) -> None:
    """Recursively replace identifier values and address-shaped text."""
    if isinstance(node, dict):
        for key in list(node):
            value = node[key]
            if key in IDENTIFIER_KEYS and isinstance(value, str) and value.strip():
                # productNumber is already resolved by
                # _substitute_product_number, which runs first and falls back
                # to a placeholder; re-touching it here would overwrite that
                # with a different value.
                if key == "productNumber":
                    continue
                original = value.strip()
                replacement = _identifier_value(key, IDENTIFIER_KEYS[key])
                node[key] = replacement
                replacements.append((key, original, replacement))
            elif isinstance(value, str):
                scrubbed = _scrub_text(value)
                if scrubbed != value:
                    replacements.append((key, value[:64], scrubbed[:64]))
                    node[key] = scrubbed
            else:
                _scrub(value, replacements)
    elif isinstance(node, list):
        for item in node:
            _scrub(item, replacements)


def _substitute_product_number(
    document: dict[str, object], replacements: list[tuple[str, str, str]]
) -> None:
    """Replace ``productNumber`` with the captured ``makeAndModel``.

    A document that reports a product number but no model still has to be
    scrubbed, so the fallback is a placeholder rather than the raw value --
    leaving it untouched would leak the SKU of a model we have no name for.
    """
    model = document.get("makeAndModel")
    product_number = document.get("productNumber")
    if not isinstance(product_number, str) or not product_number.strip():
        return
    original = product_number.strip()
    replacement = model.strip() if isinstance(model, str) and model.strip() else None
    if not replacement:
        replacement = _identifier_value(
            "productNumber", IDENTIFIER_KEYS["productNumber"]
        )
    if replacement != original:
        document["productNumber"] = replacement
        replacements.append(("productNumber", original, replacement))


def anonymize_file(
    path: Path, target: Path | None = None
) -> list[tuple[str, str, str]]:
    """Anonymize one JSON document and return the replacements it made.

    Writes the result to ``target`` when given. This is the single entry
    point; ``main`` must not reimplement the pipeline.
    """
    document = json.loads(path.read_text(encoding="utf-8"))
    replacements: list[tuple[str, str, str]] = []
    _substitute_product_number(document, replacements)
    _scrub(document, replacements)
    if target is not None:
        target.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return replacements


def main() -> None:
    """Anonymize every captured document and write a sibling ``-anon`` directory."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "input",
        type=Path,
        help="Directory produced by capture_cdp.py",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output directory (defaults to <input>-anon)",
    )
    args = parser.parse_args()

    if not args.input.is_dir():
        raise SystemExit(f"{args.input} is not a directory")

    output = args.output or args.input.with_name(args.input.name + "-anon")
    output.mkdir(parents=True, exist_ok=True)

    for path in sorted(args.input.iterdir()):
        if path.suffix not in (".json", ".xml"):
            continue
        if path.suffix == ".json":
            replacements = anonymize_file(path, output / path.name)
        else:
            # The two LEDM documents captured alongside a CDP run are XML;
            # anonymize_ledm.py owns those.
            continue
        print(f"{path.name}: {len(replacements)} replacement(s)")  # noqa: T201
        for key, original, replacement in replacements:
            print(f"  {key}: {original!r} -> {replacement!r}")  # noqa: T201

    print(f"\nAnonymized capture written to {output}")  # noqa: T201


if __name__ == "__main__":
    main()
