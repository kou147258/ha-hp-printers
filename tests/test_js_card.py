"""The JavaScript card's own checks, kept in the repository rather than in a shell.

Two things about ``examples/cards/hp_printers_card.js`` are only visible by
running it or by reading it against the integration, and both had already gone
wrong once by the time this was written:

* it asked for ``printer_mispick_events`` and ``ews_access_count``, neither of
  which is a translation key this integration publishes -- so two facts simply
  did not appear on the card, with nothing on screen to say so;
* its status pills ran every fact through the binary-switch rule, which drops
  anything whose state is not "on" or "off", so a firmware failure reason of
  ``manifestNotFound`` was treated as neither and hidden.

The key check is here, and it reads the card's source against the
integration's own tables. The behavioural checks need a JavaScript engine, so
they live beside the card in ``test_hp_printers_card.js`` and are run with
``node``; this test only asserts that they are present and that the card
parses as a balanced, non-empty module rather than a stub.
"""

from pathlib import Path
import re
import subprocess

import pytest

from custom_components.hp_printers.binary_sensor import (
    CONSUMABLE_BINARY_SENSORS,
    PRINTER_BINARY_SENSORS,
)
from custom_components.hp_printers.button import BUTTONS
from custom_components.hp_printers.sensor import CONSUMABLE_SENSORS, PRINTER_SENSORS

REPO = Path(__file__).resolve().parent.parent
CARD_DIR = REPO / "examples" / "cards"
CARD = CARD_DIR / "hp_printers_card.js"
CARD_TEST = CARD_DIR / "test_hp_printers_card.js"


def known_keys() -> set[str]:
    """Every translation key the integration publishes, on any platform."""
    return {
        d.translation_key
        for d in (
            *PRINTER_SENSORS,
            *CONSUMABLE_SENSORS,
            *PRINTER_BINARY_SENSORS,
            *CONSUMABLE_BINARY_SENSORS,
            *BUTTONS,
        )
        if getattr(d, "translation_key", None)
    }


def test_the_card_is_a_real_module_not_a_placeholder() -> None:
    """A card that renders nothing still parses, so its size is asserted."""
    source = CARD.read_text(encoding="utf-8")
    assert len(source) > 4000, "the card is a stub"
    assert "customElements.define" in source
    assert "hp-printers-card" in source


def test_every_key_the_card_looks_for_exists() -> None:
    """The failure mode is silence: a key that is not there just does not appear."""
    source = CARD.read_text(encoding="utf-8")
    asked = set(re.findall(r'\["([a-z][a-z0-9_]*)"', source))
    asked |= set(re.findall(r'"((?:ledm_)?(?:clean|calibrate)[a-z0-9_]*)"', source))
    missing = sorted(asked - known_keys())

    assert not missing, (
        "the card looks for entity keys this integration does not publish, so "
        f"those facts silently do not appear: {missing}"
    )
    assert len(asked) > 20, "the extraction stopped matching; the check is vacuous"


def test_sensor_facts_are_not_routed_through_the_switch_rule() -> None:
    """A state that is neither on nor off is a reading, not a switch.

    The rule that hides it is a single shared guard, so this asserts on the
    source: a sensor-fact list sitting inside the binary-switch list is the
    defect, and it is easier to spot in the file than in a rendered card.
    """
    source = CARD.read_text(encoding="utf-8")
    for key in ("firmware_update_failure_reason", "supply_alert_colors"):
        index = source.index(f'["{key}"')
        # The facts loop builds its pill with a fixed colour kind, which the
        # switch loop cannot: there a fact would be dropped rather than shown.
        window = source[index : index + 120]
        assert "bad" in window or "warn" in window, (
            f"{key} looks like it is being handled as a binary switch, which "
            "drops any value that is not on or off"
        )


def _node_available() -> bool:
    """Whether a JavaScript engine is on PATH, without raising.

    A ``FileNotFoundError`` here is not a failure of anything under test: the
    engine is a development tool, and a contributor without it should still be
    able to run the Python suite. An earlier version of this check called
    ``subprocess.run`` inside a ``skipif`` decorator, which runs at collection
    time and raised instead of skipping.
    """
    try:
        return (
            subprocess.run(
                ["node", "--version"],
                capture_output=True,
                timeout=30,
                check=False,
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False


def test_the_card_passes_its_own_behavioural_checks() -> None:
    """Run the card against a fake Home Assistant.

    The fixture there is built from real entity keys and real readings, so
    this covers the parts a source-level check cannot: that the card finds its
    own integration's entities, groups them per device, and renders the
    values rather than a structurally correct empty box.
    """
    if not _node_available():
        pytest.skip("no JavaScript engine on PATH")
    result = subprocess.run(
        ["node", str(CARD_TEST)],
        capture_output=True,
        text=True,
        cwd=str(CARD_DIR),
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "all card checks passed" in result.stdout
