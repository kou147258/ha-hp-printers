"""Pick the client a printer actually speaks.

Two vendor interfaces are in the wild and a single model serves only one of
them:

- **LEDM**, XML under ``/DevMgmt/``, on laser and older consumer models.
- **CDP**, JSON under ``/cdm/``, on newer consumer models. Smart Tank 580-590
  measured 2026-10-05 answers 404 for every LEDM path including
  ``DiscoveryTree.xml``, and serves the same facts over CDP.

Nothing above this module should care which one is in use: both clients
expose ``async_validate`` / ``async_get_product_info`` / ``async_get_data``
and return the same dataclasses. The protocol is therefore discovered, not
configured -- there is no user-facing setting to get wrong, and a printer
that is replaced by a different model on the same DHCP lease re-negotiates on
its own.

Detection order is LEDM first because it is the better-documented of the two
and because a LEDM-capable printer also tends to answer quickly on the first
endpoint, so the probe costs one request in the common case.
"""

import logging
import ssl

from aiohttp import ClientSession

from .api import (
    HPPrinterConnectionError,
    HPPrinterError,
    HPPrinterNotSupportedError,
    HPPrinterParseError,
    LEDMClient,
)
from .api_cdp import CDPClient
from .models import ProductInfo

_LOGGER = logging.getLogger(__name__)

type HPPrinterClient = LEDMClient | CDPClient


def _construct(
    factory: type[LEDMClient] | type[CDPClient],
    session: ClientSession,
    host: str,
    port: int,
    use_ssl: bool,
    *,
    ssl_context: ssl.SSLContext | None,
    password: str | None,
) -> LEDMClient | CDPClient:
    """Build a client, passing the password only to the one that takes it.

    LEDMClient has no write path -- the maintenance surface on a LEDM model
    lives behind the EWS web application rather than in a documented LEDM
    resource, and nothing here pretends otherwise. Handing it a sixth
    positional argument would raise a TypeError at setup on every LEDM
    printer, which is the worst possible moment to discover the difference
    between the two protocols.
    """
    if factory is CDPClient:
        return factory(session, host, port, use_ssl, ssl_context, password=password)
    return factory(session, host, port, use_ssl, ssl_context)


async def async_build_client(
    session: ClientSession,
    host: str,
    port: int,
    use_ssl: bool,
    ssl_context: ssl.SSLContext | None = None,
    *,
    password: str | None = None,
) -> tuple[HPPrinterClient, ProductInfo]:
    """Return the client this printer speaks, plus its identity.

    Raises :class:`HPPrinterConnectionError` when the host could not be
    reached at all, and :class:`HPPrinterParseError` when it answered but
    served neither interface. The two are kept apart because the config flow
    shows a different message for each, and "cannot connect" for a printer
    that is plainly online is the more misleading of the two.

    ``password`` is the EWS admin password, used only by the maintenance
    buttons. It is passed to whichever client wins the probe, and to neither
    of the failed attempts: a probe that never validates has no business
    holding a credential.
    """
    ledm_error: HPPrinterError | None = None
    cdp_error: HPPrinterError | None = None

    for factory in (LEDMClient, CDPClient):
        protocol = "LEDM" if factory is LEDMClient else "CDP"
        client = _construct(
            factory,
            session,
            host,
            port,
            use_ssl,
            ssl_context=ssl_context,
            password=password,
        )
        try:
            info = await client.async_validate()
        except HPPrinterError as error:
            _LOGGER.debug("%s probe on %s:%d failed: %s", protocol, host, port, error)
            if factory is LEDMClient:
                ledm_error = error
            else:
                cdp_error = error
            continue
        _LOGGER.debug(
            "%s at %s:%d answered as %s (serial %s)",
            protocol,
            host,
            port,
            info.make_and_model,
            info.serial_number,
        )
        return client, info

    # Neither interface answered. A connection failure from either probe means
    # the host is down, asleep, or off the network; that is the more useful
    # thing to tell someone than "this is not an HP printer we understand".
    for error in (ledm_error, cdp_error):
        if isinstance(error, HPPrinterConnectionError):
            raise error
    for error in (ledm_error, cdp_error):
        if isinstance(error, HPPrinterNotSupportedError):
            # The host answered every probe with 404: a web server is there,
            # but not one of the two interfaces.
            raise HPPrinterParseError(
                f"{host} answered but serves neither LEDM nor CDP"
            ) from error
    raise HPPrinterParseError(f"{host} answered with an unreadable response")
