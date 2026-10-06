"""Capture HP CDP JSON from a live printer for use as test fixtures.

Run this from a machine on the same network as the printer. It fetches the
endpoints the integration reads and writes each response under
``scripts/captures/<host>-<timestamp>/`` so it can be reviewed and
anonymized before being committed.

Usage:
    ./.venv/bin/python scripts/capture_cdp.py --host 192.168.0.64 --https
    ./.venv/bin/python scripts/capture_cdp.py --host 192.168.0.64 --https --port 443
    ./.venv/bin/python scripts/capture_cdp.py --host 192.168.0.64 --https --output ./raw

The script is read-only: every request is a ``GET``.

Why this exists alongside ``capture_ledm.py``
---------------------------------------------
Not every HP model serves the LEDM layer. Newer consumer models (Smart Tank
580-590, measured 2026-10) answer ``/DevMgmt/DiscoveryTree.xml`` with 404 and
expose the same facts -- identity, usage counters, consumables, events --
through a CDP REST surface at ``/cdm/<service>/<version>/<resource>``
returning JSON instead of XML.

The discovery document is captured too, precisely so a fixture can record
that a model *lacks* LEDM rather than only recording what it has.

Two transport quirks the print firmware forces on us, both found by
measurement and both worth keeping in the fixture story:

- The printer serves a self-signed certificate, and offers only legacy
  static-RSA cipher suites, so TLS verification is disabled *and* the default
  cipher list has to be relaxed. ``ssl=False`` alone is not enough on every
  build.
- The printer ignores ``Accept-Encoding: identity`` and always answers
  ``Content-Encoding: gzip``; aiohttp decodes that transparently, but a
  hand-rolled HTTP client will read compressed bytes as text.
"""

import argparse
import asyncio
from datetime import UTC, datetime
from pathlib import Path
import ssl

import aiohttp

# The endpoints the CDP client reads, plus two that are deliberately absent on
# some models: DiscoveryTree.xml proves LEDM is missing, and ProductConfigDyn
# is its LEDM counterpart.
ENDPOINTS = (
    "/DevMgmt/DiscoveryTree.xml",
    "/DevMgmt/ProductConfigDyn.xml",
    "/cdm/system/v1/identity",
    "/cdm/system/v1/status",
    "/cdm/system/v1/statistics",
    "/cdm/system/v1/configuration",
    "/cdm/deviceUsage/v1/lifetimeCounters",
    "/cdm/deviceUsage/v1/serviceCounters",
    "/cdm/supply/v1/suppliesPublic",
    "/cdm/supply/v1/configPublic",
    "/cdm/supply/v1/lifetimeCounters",
    "/cdm/supply/v1/alerts",
    "/cdm/print/v2/status",
    "/cdm/print/v2/configuration",
    "/cdm/print/v2/alerts",
    "/cdm/scan/v1/status",
    "/cdm/scan/v1/alerts",
    "/cdm/media/v1/configuration",
    "/cdm/media/v1/alerts",
    "/cdm/media/v1/capabilities",
    "/cdm/alert/v1/alerts",
    "/cdm/alert/v1/capabilities",
    "/cdm/diagnostic/v1/systemEvents",
    "/cdm/ioConfig/v2/capabilities",
    "/cdm/network/v1/snmpConfig",
    "/cdm/security/v1/deviceAdminConfig",
    # Added after asking the device what it advertises rather than guessing:
    # /cdm/servicesDiscovery lists 89 links on the model measured, and these
    # are the ones that answer a question a user asks. The four *Private
    # documents are deliberately absent -- the device does not redact them, so
    # capturing one would put a real cartridge serial in the repository.
    "/cdm/servicesDiscovery",
    "/cdm/deviceSetup/v1/status",
    "/cdm/firmwareUpdate/v2/updateStatus",
    "/cdm/firmwareUpdate/v2/updateCheck",
    "/cdm/firmwareUpdate/v2/configuration",
    "/cdm/firmwareUpdate/v2/updateHistory",
    "/cdm/alert/v1/criticalAlerts",
    "/cdm/alert/v1/errorAlerts",
    "/cdm/certificate/v1/certificates/selfSignedCertificate",
    "/cdm/certificate/v1/capabilities",
    "/cdm/ioConfig/v2/adapterStats",
    "/cdm/network/v1/internetDiagnostics",
    "/cdm/network/v1/printServices",
    "/cdm/network/v1/proxyConfig",
    "/cdm/network/v1/discoveryServices",
    "/cdm/network/v1/nameResolverServices",
    "/cdm/supply/v1/configPrivate",
    "/cdm/supply/v1/regionReset",
    "/cdm/print/v2/printCapabilities",
    "/cdm/print/v2/setupStatus",
    "/cdm/ble/v1/configuration",
    "/cdm/system/v1/serviceConfig",
    "/cdm/system/v1/images",
    "/cdm/power/v1/configuration",
    "/cdm/calibration/v1/capabilities",
    "/cdm/calibration/v1/calibrations",
    "/cdm/report/v1/reports",
    # An LEDM printer answers these three alongside its XML, and they are the
    # only place its quiet-print flag, panel language and instant-ink status
    # exist. A model serving none of them answers 404 and they are skipped.
    "/cdm/print/v1/printModeConfiguration",
    "/cdm/controlPanel/v1/configuration",
    "/cdm/consumableSubscription/v1/info",
)

# Sent so a 404 is recorded as a body-less response rather than crashing the
# run: the point of this capture is to record what a model does *not* serve.
STATUS_MARKER = "HTTP %(status)s"


def _ssl_setting(scheme: str) -> object:
    """Return the ssl argument for aiohttp.

    Printers use a self-signed certificate and, on the models measured so
    far, only offer legacy static-RSA suites. Passing ``False`` disables
    verification but keeps the default cipher list, which those builds do
    not satisfy -- so the cipher list is relaxed here instead of left to
    the default and discovered to fail at handshake time.
    """
    if scheme == "http":
        return False

    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    # Printers on the models measured so far offer only legacy static-RSA
    # suites such as AES128-GCM-SHA256, which OpenSSL's default security
    # level rejects outright -- the handshake fails before certificate
    # checking is ever reached. SECLEVEL=1 keeps those suites available.
    context.set_ciphers("DEFAULT:@SECLEVEL=1")
    return context


async def capture(host: str, port: int, scheme: str, output: Path) -> None:
    """Fetch every CDP endpoint and save the responses."""
    output.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target_dir = output / f"{host}-{timestamp}"
    target_dir.mkdir(parents=True, exist_ok=True)

    base = f"{scheme}://{host}:{port}"
    headers = {"User-Agent": "ha-hp-printers capture/1.0"}

    timeout = aiohttp.ClientTimeout(total=20)
    ssl_setting = _ssl_setting(scheme)

    captured = 0
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        for endpoint in ENDPOINTS:
            url = f"{base}{endpoint}"
            # Flatten the path so services that share a resource name stay
            # distinct (``/cdm/print/v2/status`` vs ``/cdm/scan/v1/status``),
            # and keep the extension. Dropping it silently produced a directory
            # of extensionless files, which the anonymizer -- which selects on
            # ``.json`` -- then skipped without a word.
            filename = endpoint.lstrip("/").replace("/", "_")
            if not filename.endswith((".json", ".xml")):
                filename += ".json"
            target = target_dir / filename
            print(f"GET {url} -> {target}")  # noqa: T201
            try:
                async with session.get(url, ssl=ssl_setting) as response:
                    body = await response.text()
                    if response.status != 200:
                        # A model that does not serve this resource is part of
                        # the fixture story, so record the status rather than
                        # dropping the entry.
                        body = STATUS_MARKER % {"status": response.status}
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                body = f"ERROR {type(exc).__name__}: {exc}"
                print(f"  failed: {exc}")  # noqa: T201

            target.write_text(body, encoding="utf-8")
            captured += 1
            # Brief pause so we do not trigger rate limiting on tight loops.
            await asyncio.sleep(0.5)

    print(f"\nCaptured {captured} endpoints into {target_dir}")  # noqa: T201


def main() -> None:
    """Parse arguments and run the capture."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, help="Printer hostname or IP")
    parser.add_argument("--port", type=int, default=443, help="Web port (default 443)")
    parser.add_argument(
        "--https",
        action="store_true",
        help=("Use HTTPS. Note: printers usually serve self-signed certificates."),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("scripts/captures"),
        help="Directory to write captures into",
    )
    args = parser.parse_args()

    asyncio.run(
        capture(
            host=args.host,
            port=args.port,
            scheme="https" if args.https else "http",
            output=args.output,
        )
    )


if __name__ == "__main__":
    main()
