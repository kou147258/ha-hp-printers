# Agent Instructions

## Scope

- This repository is a standalone Home Assistant custom integration; the runtime entrypoint is `custom_components/hp_printers/__init__.py`.
- There is no runtime package manifest or lockfile; use the checked-in `.venv` for local verification.
- GitHub Actions are the canonical CI gate: `.github/workflows/ci.yaml` mirrors the commands below on every push and pull request, and `hassfest` validates the manifest and brand assets.

## Verification

- Install test tooling with `./.venv/bin/uv pip install --python .venv/bin/python -r requirements_test.txt` when needed.
- Run `./.venv/bin/python -m pytest -q` for the unit suite.
- Run `./.venv/bin/ruff check --config ruff_ha.toml custom_components/hp_printers tests`.
- Run `./.venv/bin/ruff format --check --config ruff_ha.toml custom_components/hp_printers tests`.
- Run `./.venv/bin/python -m compileall -q custom_components/hp_printers tests` for a syntax-only check.
- Ruff is configured for Home Assistant conventions in `ruff_ha.toml`; do not substitute an ambient Ruff/Python version when the repository venv is available.
- The test stack is a single pin: `pytest-homeassistant-custom-component==0.13.356`, which transitively fixes `homeassistant==2026.8.2`, `pytest==9.0.3` and `pytest-asyncio==1.4.0`. Do not pin pytest or pytest-asyncio separately in `requirements_test.txt`; a separate pin conflicts with what this package requires and breaks CI while local runs stay green.
- The suite requires Python 3.14, which that package's Home Assistant pin also requires.
- `ruff_ha.toml` is extracted from Home Assistant core but **diverges deliberately in one place**: `known-first-party` is `["custom_components", "tests", "scripts"]`, not `["homeassistant"]`. Core builds the `homeassistant` package; here it is a dependency. Left as core had it, isort groups `custom_components` with third-party imports and CI fails on ordering. Re-apply this if the file is ever re-extracted.
- `.pre-commit-config.yaml` runs the same lint, format, test and JSON-parse checks against this venv. Install with `./.venv/bin/uv pip install --python .venv/bin/python pre-commit && ./.venv/bin/pre-commit install`. The hooks are `repo: local` on purpose: the upstream Ruff mirror pins its own Ruff version, which would disagree with the one CI uses.
- Lint and format **`scripts` as well as `custom_components/hp_printers` and `tests`** — CI does, and a check that omits a directory hides real errors in it.

## Test Policy

- **Any change that alters functionality must ship with tests.** A bug fix
  needs a regression test for the case it fixes; a new entity needs coverage
  of the parser path and the entity description that exposes it; a refactor
  must keep the existing suite green.
- The unit suite in `tests/` covers LEDM parsing in `api.py` and the
  config-flow normalization in `config_flow.py`. New behaviour should land
  in those modules (or a new sibling) and a matching test should appear
  alongside it.
- Do not delete a test to make a change pass. If a test is wrong, fix the
  test and explain why in the commit message.
- Coverage was 99% before the CDP and IPP clients landed. It is **97%**
  now, and `quality_scale.yaml` records that next to its `test-coverage`
  claim so the number is checkable rather than remembered. Measure with
  `./.venv/bin/python -m pytest -q --cov=custom_components/hp_printers
  --cov-report=term-missing`. Re-measure and update the claim in the same
  commit that moves the number: a stale `done` reads as a current
  guarantee, which is the failure mode to avoid.
  `api_cdp.py` is the module to watch -- it sat at 62% until the parsing
  helpers and the transport error paths were covered directly, and the
  gap was invisible until a coverage run rather than a review found it.
  `api_ipp.py` was the second one, at 90%, and covering it found a real
  bug: the parser skipped the four-octet value tag after an IPP extension
  tag instead of reading it, so every attribute behind one decoded as an
  unknown type and came back as raw bytes. The uncovered line was
  `index += 4` -- a line that looked obviously correct in isolation.
  `button.py` arrived at 97% because its tests are mostly about the paths
  that *refuse* to send, which is where the risk in a write path is.

## Architecture

- `api.py` is a read-only LEDM client; it fetches XML from the printer's `/DevMgmt/*` endpoints and maps it into immutable dataclasses in `models.py`.
- `coordinator.py` polls dynamic data; the default interval is 60 seconds and the static product configuration is refreshed every six hours.
- `sensor.py` and `binary_sensor.py` contain the actual entity descriptions. Parsing a field in `api.py` or `models.py` does not expose it in Home Assistant until an entity description is added there.
- `config_flow.py` validates the printer before creating an entry, supports mDNS discovery via IPP/IPPS advertisements, and keys entries by printer serial number so DHCP address changes do not duplicate devices.
- `coordinator.py` backs off on consecutive failures: `backoff_interval()` is a pure function so it is testable without hass, and `_apply_backoff` is the only place `update_interval` is mutated. The configured interval is a floor -- backoff must never make polling faster than the user asked for.
- `coordinator.py` exposes `async_fetch_update` so the polling logic can be unit-tested without standing up a Home Assistant instance.
- `strings.json` supplies config-flow and entity names; update it when adding or renaming user-visible entities.
- `quality_scale.yaml` tracks which HA quality-scale rules the integration currently meets; update it when adding or removing coverage.

## Test Layers

The unit suite is built from plain Python doubles in `tests/fakes.py`
plus `MagicMock` for any HA layer we can't avoid, so most tests run in
milliseconds. Use the right layer for the change:

- **Parser behavior** (`tests/test_api.py`, `tests/test_api_parsing.py`,
  `tests/test_api_fixtures.py`): pure XML parsing, no event loop.
- **Models** (`tests/test_models.py`): dataclass invariants.
- **Entity layer** (`tests/test_entity_value_fns.py`): instantiate each
  description against a `FakeCoordinator` and assert
  `native_value` / `is_on` / `extra_state_attributes` matches the README.
- **Coordinator** (`tests/test_coordinator.py`): exercises
  `async_fetch_update` directly.
- **Diagnostics** (`tests/test_diagnostics.py`): the redaction pipeline.
- **Config flow, unit** (`tests/test_config_flow_probe.py`,
  `tests/test_config_flow.py`): the probe helper, reauth, and `_flatten`
  normalization, called directly.
- **Parser edge cases** (`tests/test_api_edge_cases.py`): malformed and
  absent values, and the HTTP boundary in `_fetch` with a fake session.
- **Bootstrap, through hass** (`tests/test_init.py`,
  `tests/test_config_flow_hass.py`): setup, unload, reload, and every flow
  step driven through Home Assistant's own managers. These use
  `pytest-homeassistant-custom-component`, which blocks all sockets, so
  every network path must be mocked.
- **Real captures** (`tests/test_api_fixtures.py`): when
  `tests/fixtures/<host>/` is present, the fixture tests replay real XML.
  Tests skip gracefully when no fixture exists.

## Test fixtures and ordering

- `tests/__init__.py` exposes `setup_integration(hass, entry)` as a plain
  helper, called from the test body. It is deliberately **not** a fixture:
  as a fixture it is ordered against the mocks by the test signature, and a
  test listing it before the client patch runs setup against the real client
  and trips the socket block. This mirrors
  `homeassistant/tests/components/brother`.
- Patch `LEDMClient` in **both** binding namespaces:
  `custom_components.hp_printers.LEDMClient` and
  `custom_components.hp_printers.config_flow.LEDMClient`.
- Any test loading the integration through hass needs
  `enable_custom_integrations`.
- The mocked client must supply a real string for `base_url`; it reaches
  `DeviceInfo(configuration_url=...)`, which rejects a `MagicMock`.
- Unloading an entry does **not** remove its states. The entity registry
  writes an `unavailable` state for each registered entity instead, so
  assert on `STATE_UNAVAILABLE` rather than on absence.

- A real capture from an HP Color LaserJet MFP M182nw lives in
  `tests/fixtures/m182nw/`. `tests/test_api_fixtures.py` pins its known
  values, which is also the record of what that model does **not** report:
  no `ProductInformation/Manufacturer`, no refill counters, no ADF or
  duplex counters, and an `Installation/Date` of `1976-01-01` because the
  device has no real-time clock.
- `scripts/captures/` is git-ignored: raw captures contain the real serial
  number. Only the reviewed, anonymized copy under `tests/fixtures/`
  belongs in the repository.
- **The capture order is load-bearing**: raw into `scripts/captures/`,
  scrub, then copy the `-anon` directory into `tests/fixtures/`. Writing
  raw captures straight into `tests/fixtures/` is how a real serial number
  got one commit away from the repository.
- **Adding an endpoint to a capture script means adding its identity fields
  to the matching anonymizer in the same commit.** Both `ShopForSupplies`
  and the self-signed certificate's `commonName` leaked for exactly this
  reason -- neither field is named after what it holds. Verify by grepping
  the capture for the values you actually saw, not for the ones you thought
  of; every leak found this way was found by grep and none by reading.
- **A non-parse exception in an anonymizer must stop the run.** The
  command-line path once caught every exception, reported it as "not XML",
  and copied the file through unmodified -- so a broken regex in a scrubber
  shipped the identifier it was written to remove, under a message that said
  the file was not XML. The catch is narrowed to a parse error.

## Capturing fixtures from a real printer

Real LEDM payloads are the highest-value test input because they capture
quirks no XML hand-rolled in a test file will reproduce. To add a
fixture:

1. Run `./.venv/bin/python scripts/capture_ledm.py --host <printer-host>`
   from a machine on the same network. The script writes raw XML into
   `scripts/captures/<host>-<timestamp>/`.
2. Run `./.venv/bin/python scripts/anonymize_ledm.py scripts/captures/<dir>/`.
   The anonymizer replaces `SerialNumber`, `UUID`, `ServiceID`, hostnames,
   IPs, and cartridge serial numbers with stable dummies and swaps
   `ProductNumber` for the captured `MakeAndModel`. Review the diff.
3. Copy the `<dir>-anon` directory into `tests/fixtures/<short-name>/`
   and commit. The fixture tests will pick it up automatically.
4. The capture is read-only -- every request is a `GET`.

**Adding an endpoint to `capture_ledm.py` means auditing it for identity
fields.** Walk the new document, add every identifier tag to
`IDENTIFIER_TAGS` in `anonymize_ledm.py`, and add a case to
`tests/test_anonymize.py` asserting the raw value does not survive. The
anonymizer is a filter over tag names we have seen; it cannot infer a
field it has never met. `IOConfigDyn` needed four spellings of the
hostname plus the MAC in two places.

Two rules the anonymizer has already been bitten by:

- `main()` must not reimplement the pipeline. It calls `anonymize_file`,
  which is the only place the pipeline exists -- a second copy silently
  reverted a fix once already.
- A tag replaced by `IDENTIFIER_TAGS` is not then run through the
  free-text patterns, or the placeholder gets rewritten again (a netmask
  became an IP address that way).

## Device/API Traps

- **Every read is a `GET`. There is no exception to that, and a read that
  needs a credential is a bug.** All data on both protocols is served
  unauthenticated, and a read gated on the admin password would fail setup on
  a printer whose password was changed. The admin password is used for
  nothing except the maintenance buttons.
- **Writes exist, and they are a deliberate exception to the read-only
  design — not a drift from it.** The user asked for cleaning and printhead
  alignment as buttons they press themselves, on the grounds that these
  operations spend ink and paper and therefore have to be somebody's
  decision. Four rules keep that decision the user's:
  1. No write is reachable from the coordinator. Not from a poll, a restart,
     a reload, or a repair. Only from `ButtonEntity.async_press`.
  2. A button exists only for an operation the device lists in its own
     reports document. A model with no `cleaningPage` gets no clean button,
     because the alternative is a button that answers "this printer does not
     offer that".
  3. Refuse before sending whenever the device has told us something that
     would waste the cycle — the calibration type is not offered, the input
     tray reports empty.
  4. Log every write at warning level with the endpoint and body. That line
     is the audit trail for a request that costs ink and paper.
- **CDP writes do not authenticate, and the admin password is never sent.**
  Measured on the Smart Tank 580-590: every CDP document is served with no
  credential, and attaching a *correct* HTTP Basic header turns working 200s
  into 401s. `/AuthChk` does not discriminate — the right password, a wrong
  one, and none at all all answer 300 on that model (on a *LEDM* model the
  same endpoint does discriminate: 200 with the right password, 300 without).
  So `_auth_header` sends no Authorization header, and the password stays in
  memory. Do not "fix" this by adding Basic back: on CDP it is not merely
  useless, it turns working requests into 401s, and a value the protocol
  ignores is one leak away from appearing in a log.
- **`/cdm/remoteAuthentication/v1` is the cartridge-bay PIN, not EWS admin
  auth.** Its capabilities report `pinLabelLocation: "cartridgeAccessArea"`
  and `pushbuttonSupported: true`. POSTing to its `tokens` endpoint answers
  409, or 500 for an empty object, with an empty body — for every body shape
  tried, including the obvious `username`/`password`. It reveals nothing and
  is not the answer to "how does a CDP write authenticate".
- **The request body for a report is the `reportId` the device publishes** in
  `/cdm/report/v1/reports`, on the link that advertises `PATCH`. That part is
  evidence. The calibration body is **not**: `GET /cdm/calibration/v1/calibration`
  answers 400, so there is no representation to copy the request from, and the
  device answers 400 with an *empty* body for every body shape tried. The
  calibration request is the one unverified thing in the write path, and
  pressing the button is what will confirm it.
- **The only supported write path is CDP.** A model that speaks LEDM gets
  no buttons, because its maintenance surface is behind the EWS web
  application rather than in a documented LEDM resource — `DiscoveryTree.xml`
  on the measured LEDM model lists 24 resources and none of them is a
  maintenance endpoint, and the `MaintenanceManifest.xml` paths its own
  JavaScript bundle references all answer 404. Do not infer a write path from
  the EWS bundle: that bundle is shared across HP's whole product line and
  contains code for features the hardware does not have.
- **The EWS web application is a separate surface from the data
  interfaces, and it can have features neither of them exposes.** Three on
  one LEDM model, found only after concluding twice that they did not exist:
  the quiet-print flag, in the small CDP layer the printer answers alongside
  its XML; the whole maintenance interface, in
  `/DevMgmt/InternalPrintCap.xml`, which is in neither `DiscoveryTree.xml`
  nor any path the web page itself is mounted at; and the firmware update
  page, reachable *only* through the web app — every firmware manifest path
  answers 404, and the page path answers 403. **Do not conclude that a
  feature is absent from what the data interfaces return.** Check the
  bundle the printer ships its own browser, and check the paths the
  maintenance manifest chain implies.
- **Never invent a request body.** The device answers every malformed body
  with a 400 and an *empty* body, so probing cannot recover one and a wrong
  guess fails silently. The only source that has ever worked is the code the
  printer ships to its own browser: it is where both the CDP report and
  calibration bodies came from, and both were wrong when guessed. Where no
  such code exists — the firmware check endpoint — say so rather than
  guessing, because a guessed body that lands on firmware is a different
  proposition from one that lands on a cleaning cycle.
  not from the discovery document.** `/cdm/servicesDiscovery` names the
  endpoints and the methods and says nothing about the body — and the device
  answers every malformed body with a 400 and an **empty** body, so probing
  cannot recover it. The body is written down in the JavaScript the printer
  ships to its own browser (`/framework/Unified.js`, unauthenticated, ~650 kB),
  which is the only place either request exists:

  - report: `PATCH /cdm/report/v1/print` with
    `{state: "processing", version: <from the reports document>, reportId: <id>}`
    — the version is echoed from the device, so it is read, never hardcoded.
  - calibration: `PATCH /cdm/calibration/v1/calibration/<type>` with
    `{calibrationType: <type>, operationType: "calibration"}` — the **member**
    path, not the collection, plus a second field beside the type.

  Both were wrong in the first version, in ways that raised nothing. Read the
  bundle before changing either.
- **HP LEDM is self-describing but undocumented; only create entities for values the device actually reports, otherwise the setup omits them.**
- **Ask the device what it advertises instead of guessing paths.** LEDM
  publishes every resource in `DiscoveryTree.xml`; CDP publishes 89 links in
  `/cdm/servicesDiscovery` together with the HTTP methods each accepts. Both
  are self-describing and free to read. Guessing a path list is how the CDP
  endpoint table was built before `/cdm/servicesDiscovery` turned up, and
  it was missing 76 of them.
- **The `*Cap.xml` documents are the device's own specification.** Each
  declares the type, range, step and access mode of every field its `Dyn`
  partner carries — `typeof="dd:Int" min="0" max="14" step="1"
  access="readOnly"`, with an `elementXPath` back to the value. Reading them
  is the systematic way to find a parse gap; the alternative is inferring
  field names. `ProductUsageCap.xml` alone is 32 kB of declared counters.
- **An LEDM printer also serves a handful of `/cdm/` documents**, and two of
  them carry values LEDM has no equivalent for at all: the quiet-print flag
  and the control panel's language. Read them on the model that has them.
  Reporting them absent would be a different claim from "the printer has no
  such setting", and only the second one would be true if they were missing.
- **A field's name is not its meaning.** `GetCommunityNameConfig` is an
  enumeration whose only declared values are `publicAllowed` and
  `publicNotAllowed`, read from the device's own `NetAppsCap`. Neither
  contains "enabled", so a generic on/off test describes a printer that
  permits the default community string as having it switched off. Enums read
  from a capability document are mapped explicitly, and an unrecognised
  value is `None` — defaulting a security field to the safe-looking answer
  is the one way it can be quietly wrong.
- Printer HTTPS commonly uses a self-signed certificate and legacy static-RSA ciphers; use the existing `printer_ssl_context()` path rather than replacing it with default TLS settings.
- The zeroconf-announced IPP port is not the LEDM web-server port; discovery deliberately uses the printer hostname with the configured HTTP/HTTPS web port.
- Product and consumable fields can contain sentinel or historical values. Preserve the filtering and naming semantics in `api.py` and `models.py`, especially `PreviousCartridgeData`, which describes the cartridge removed from a slot rather than the installed cartridge.
- `IOConfigDyn` is fetched through `_fetch_optional`, which swallows failures: not every model serves it, and a missing optional resource must not fail the whole update. Its `NetworkStatus` cannot report an outage -- the document is read over the adaptor it describes -- so it is never wired into availability; the error counters are the part worth having.
- The network port type must be read from the `IOAdaptorConfig` that actually contains `NetworkAdaptorConfig`. The first one in the document is USB on every capture seen so far.
- No network identity (hostname, IP, MAC) is parsed at all. The anonymizer scrubs those from fixtures, and nothing should start reading them into entities or diagnostics.
- Diagnostics are intentionally redacted in `diagnostics.py`; do not expose host, serial, UUID, or user identifiers in new diagnostic output.

## Releases

- **Versioning is Home Assistant's own: `YEAR.MONTH.RELEASE`** -- `2026.8.0`, `2026.8.1`, then `2026.9.0` when the month turns over. The month is not zero-padded and `RELEASE` counts from 0 within each month. `tests/test_manifest.py` pins the format.
- **Releases are automatic.** `.github/workflows/release.yaml` runs after CI succeeds on `main`, computes the next calendar version, writes it into `manifest.json`, commits, tags and publishes. Nothing to bump by hand.
- Release notes are built from commit subjects, not GitHub's `--generate-notes`, which lists merged pull requests and therefore lists nothing here. **Commit subjects are the changelog** -- write them for someone reading the release page.
- A release is cut only when something under `custom_components/` changed since the last release. Documentation, CI and test-only commits ship nothing to a user and get no version.
- The release commit carries `[skip ci]`, which is what stops it triggering CI and looping back into this workflow. Do not remove it.
- The next version is computed from **both** existing releases and existing tags, and the job fails if the computed tag already exists. Deleting a release leaves its tag behind, and reusing that number would silently attach the new release to the old tag's commit.
- Releases are cut from the exact commit CI passed on, not from whatever `main` points at when the job runs.
- HACS reads **published** releases. A draft or a bare tag is invisible to it, and a draft that was never tagged only appears on the repository's Releases page, not at any tag URL.
- release-drafter was removed: it categorises merged pull requests, and this repository commits directly to `main`, so it produced empty drafts. If the workflow ever moves to PRs, it is worth restoring.
- Versions before `2026.8.0` used semver (`v0.1.0`). The calendar scheme sorts above them, so the switch needed no special handling.
