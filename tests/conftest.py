"""Shared fixtures for bootstrap-level tests.

These fixtures stand up a real ``HomeAssistant`` via
``pytest-homeassistant-custom-component`` so the integration's setup,
unload, and config-flow paths are exercised through Home Assistant's own
config-entry manager rather than by calling functions directly.

``async_build_client`` -- the single seam that constructs a client and
returns its identity -- is patched in both namespaces that bind it:
``custom_components.hp_printers`` (setup) and
``custom_components.hp_printers.config_flow`` (probing).

The patch used to be on ``LEDMClient`` itself. That stopped working when
client construction moved behind the protocol probe: a model serving CDP
never constructs a ``LEDMClient`` at all, so patching that class would let
the CDP path reach the real constructor and try to open a socket. Patching
the probe keeps the tests independent of which protocol a fixture
represents.
"""

from collections.abc import Generator
from ipaddress import IPv4Address
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.const import CONF_HOST, CONF_PORT, CONF_SSL
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hp_printers.const import DOMAIN

from .fakes import make_printer_data, make_product_info

TEST_HOST = "192.0.2.1"
TEST_SERIAL = "SN-TEST-1234"


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """Return a config entry keyed to the fixture printer's serial."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Office printer",
        unique_id=TEST_SERIAL,
        data={CONF_HOST: TEST_HOST, CONF_PORT: 80, CONF_SSL: False},
    )


@pytest.fixture
def mock_ledm_client() -> Generator[MagicMock]:
    """Patch the protocol probe and the paper client everywhere they are bound."""
    product_info = make_product_info()
    printer_data = make_printer_data()

    client = MagicMock()
    # The three client coroutines are declared explicitly. A plain MagicMock
    # would return a non-awaitable from an async-looking method, and the
    # coordinator's ``await client.async_get_data()`` would then fail with
    # "object can't be awaited" -- an error that reads like a bug in the
    # integration rather than in the double.
    client.async_validate = AsyncMock(return_value=product_info)
    client.async_get_product_info = AsyncMock(return_value=product_info)
    client.async_get_data = AsyncMock(return_value=printer_data)
    # base_url reaches DeviceInfo(configuration_url=...), which rejects
    # anything that is not a real URL -- a MagicMock included.
    client.base_url = f"http://{TEST_HOST}"
    client.host = TEST_HOST

    # The paper level is read over IPP by a second client. It is patched here
    # for the same reason as the probe: pytest-socket blocks real connections,
    # so an unpatched IPPClient would fail the whole bootstrap suite rather
    # than one test. Returning a tray here also means the wiring is exercised
    # rather than merely constructed.
    ipp_client = MagicMock()
    ipp_client.async_get_paper_level = AsyncMock(
        return_value=[
            {
                "name": "Tray 1",
                "type": "sheetFeedAutoNonRemovableTray",
                "level": 48,
                "max_capacity": 100,
                "unit": "percent",
            }
        ]
    )

    async def _probe(*_args, **_kwargs):
        # Delegating rather than returning a fixed tuple is what keeps the
        # per-test failure injection working: several tests set
        # ``client.async_validate.side_effect`` to make the probe fail, and
        # with a stubbed return value that setting would reach nothing.
        identity = await client.async_validate()
        return client, identity

    probe = AsyncMock(side_effect=_probe)
    with (
        patch(
            "custom_components.hp_printers.async_build_client",
            new=probe,
        ),
        patch(
            "custom_components.hp_printers.config_flow.async_build_client",
            new=probe,
        ),
        patch(
            "custom_components.hp_printers.IPPClient",
            return_value=ipp_client,
        ),
    ):
        yield client


def zeroconf_info(
    hostname: str = "HPE45A5B0.local.",
    host: str = "192.0.2.2",
) -> ZeroconfServiceInfo:
    """Build a ``ZeroconfServiceInfo`` shaped like HP IPP advertisements."""
    address = IPv4Address(host)
    return ZeroconfServiceInfo(
        ip_address=address,
        ip_addresses=[address],
        port=631,
        hostname=hostname,
        type="_ipp._tcp.local.",
        name="HP Printer 1234._ipp._tcp.local.",
        properties={"rp": "ipp/print"},
    )


def user_flow_input(
    host: str = TEST_HOST,
    **overrides: Any,
) -> dict[str, Any]:
    """Build a valid ``user`` step payload (schema-shaped, section nested)."""
    advanced: dict[str, Any] = {CONF_PORT: 80, CONF_SSL: False}
    if "port" in overrides:
        advanced[CONF_PORT] = overrides.pop("port")
    if "ssl" in overrides:
        advanced[CONF_SSL] = overrides.pop("ssl")

    payload: dict[str, Any] = {CONF_HOST: host}
    if "name" in overrides:
        payload["name"] = overrides.pop("name")
    payload.update(overrides)
    payload["advanced_settings"] = advanced
    return payload
