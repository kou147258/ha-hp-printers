# HP Printers for Home Assistant

Local integration for HP printers. Reads the printer over HTTP/HTTPS — no
cloud, no account, no writes on any polling path.

Newer HP models no longer serve **LEDM**, the XML interface this integration
originally targeted; they serve a JSON API instead (**CDP**), and report paper
level over **IPP**. All three are supported, and which one a given printer
speaks is worked out at setup — there is no protocol setting to fill in.

An optional EWS admin password enables buttons for the cleaning and printhead
alignment operations the printer itself offers. Those spend ink and paper, so
they are buttons you press, never something a poll or an automation fires on
its own. Everything else works without the password.

## What you get

The printer itself is one device; each cartridge is a sub-device because
cartridges are independently replaceable and have their own serial numbers.
Depending on what the model reports, the integration exposes:

- **Printer**: status, page counters, jams, mispicks, paper level, ink
  consumed, printhead alignment result, firmware build date, event log, network
  link health, and diagnostic state.
- **Scanner and copier**: their own counters, when those capabilities exist.
- **Cartridges**: level, pages remaining, pages printed, part and serial
  information, dates, genuine/clone status, and problem state.

See [Entities](#entities) for the full list.

## Why this one

I started this integration because I wanted to **name my printer**. The
existing options created entity IDs like
`sensor.hp_color_laserjet_mfp_m182nw_192_168_0_64_status`, and there was no way
to clean them up short of manually renaming every entity in Home Assistant.
Setting a friendly name during setup, and having every entity ID follow that
name, was the original goal.

Once I had the printer responding, I started finding other things that were
missing or wrong in the existing integrations:

- The same `TotalImpressions` counter appearing under both the printer and
  the scanner because no one was parsing them in the right context.
- Cartridges that HP labeled `clone` showing up as the genuine part number
  (the chip lies about itself; the brand field is what tells the truth).
- A firmware crash from six months ago still showing as the device's current
  state, because nothing distinguished the recorded fault from a live one.
- The most recent event code buried in a thirty-page printed report rather
  than surfaced as a sensor.

This integration exists to fix that specific set of annoyances while keeping
the read-only, no-credentials, no-cloud design that the existing options got
right.

## Installation

### HACS

[![Open your Home Assistant instance and show the HP Printers integration in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=aljopro&repository=ha-hp-printers&category=integration)

If the button does not work, add the repository manually:

1. Open **HACS** in Home Assistant.
2. Open the three-dot menu and select **Custom repositories**.
3. Enter `https://github.com/aljopro/ha-hp-printers`.
4. Select **Integration** as the repository type and click **Add**.
5. Search HACS for **HP Printers**, open it, and click **Download**.
6. Restart Home Assistant.

**Manually**: copy `custom_components/hp_printers` into your `config/custom_components`
directory and restart.

Then *Settings → Devices & Services → Add Integration → HP Printers*.

## Requirements

- An HP printer with an Embedded Web Server (EWS) that exposes **LEDM** (XML)
  or **CDP** (JSON). Both are detected automatically; see
  [How it talks to your printer](#how-it-talks-to-your-printer).
- Home Assistant **2026.8.2** or newer.

The printer's web server must stay reachable from Home Assistant. Paper level
additionally needs port 631 open; it is the only feature that uses it, and
everything else works without it.

## Configuration

| Field | Notes |
|---|---|
| **Host** | Prefer the printer's mDNS name over its IP — HP sets one from the MAC, such as `NPI2E7F3D.local` (you'll find it on the printer's Network Summary page, or in the TLS certificate's common name). It resolves to a MAC-derived IPv6 address that cannot change on a lease renewal, so no DHCP reservation is needed. An IP works too; entries are keyed on serial number, so an address change will not orphan your entities either way. |
| **Name** | Optional. Drives the device name and every entity ID. Leave blank to use the model name. |
| **Port / HTTPS** | Under *Advanced settings*. Defaults to port 80. Printers serve a self-signed certificate, which is not verified. |
| **Admin password** | Under *Advanced settings*, and optional. It is used by the [maintenance buttons](#maintenance-buttons) and by nothing else — no sensor, no status, no counter needs it. Leave it blank and those buttons are simply not created. |

Polling defaults to **60 seconds** and is adjustable under *Configure*. Printers
sleep between jobs and polling wakes them, so slower is gentler on the hardware.
When the printer stops answering — asleep, powered off, or off the network —
the interval doubles per consecutive failure up to ten minutes, and snaps back
to the configured value on the first successful read. Backing off never polls
faster than you asked for: a 30-minute interval stays 30 minutes.

## How it talks to your printer

Three interfaces are in play, and which ones a printer uses varies by model and
firmware. None of this is configurable, because a wrong setting is the kind of
thing you cannot discover until something silently reports nothing.

| Interface | What it is | Used for |
|---|---|---|
| **LEDM** | XML under `/DevMgmt/` on the embedded web server. The oldest and best-documented-by-observation of the three — every value is paired with a capability document that names its type and legal range. | Most printers |
| **CDP** | JSON under `/cdm/`. The modern replacement; newer consumer models serve *only* this and answer 404 to every LEDM path. | Newer models |
| **IPP** | The standard print protocol, on port 631. Every printer already speaks it. | Paper level only |

At setup the integration asks for LEDM first, because a model that serves it
answers on the first request and the capability documents make the XML far
easier to read. A model that does not answers **404**, at which point it falls
back to CDP. It re-checks on every restart, so replacing a printer with a
different model under the same address needs no reconfiguration.

That 404 matters more than it looks. A model without LEDM returns 404 for
*every* endpoint, so treating "not found" as "cannot connect" would report a
printer that is online and printing as offline. The two are separate conditions
here, and only the second one puts the device on `unavailable`.

IPP is used for one thing: `MarkMediaLevel`/`MarkMediaMax` is the only place
either protocol reports how much paper is left, as opposed to merely whether
the tray holds any. A model that does not describe its tray gets no paper-level
entity rather than a permanently-100% one.

### A note on "pages printed"

Two different numbers both look like "how much has this printer printed", and
they are not the same:

- The **engine total** is every page the engine has ever turned. It includes
  jams, retries, copies, scans and calibration passes, and HP's own EWS states
  it never resets. On one of the test machines it reads 44,492 where the
  user-facing figure is 44,376.
- **Black and white + color pages** is what was actually printed.

Use the split, not the total, for anything you intend to chart. The
`Pages printed` sensor is the engine total on LEDM printers; on CDP printers
the protocol's equivalent counter *is* the split, so the integration reports it
directly. `Black and white pages` and `Color pages` mean the same thing on
both, and are the ones to add together.

## Devices

The printer is one device; each cartridge is a sub-device linked to it, since
cartridges are independently replaceable and carry their own serial numbers.

Entities whose data a given model does not report are not created at all —
a printer with no document feeder, duplexer or fax simply gets fewer entities
rather than a row of `unknown`.

## Entities

Entities are created only when the printer reports data for them. Diagnostic
and noisy entities are off by default; turn them on per-entity from the
device page.

### Printer

| Entity | Type | Notes |
|---|---|---|
| Status | sensor (enum) | Current state from the printer's `StatusCategory`. Localised in the integration. |
| Accepting jobs | binary_sensor | `on` when the printer is ready for a new job. On a CDP model this is the only "ready for work" signal there is. |
| Scanner status | sensor (diagnostic) | Scanner subunit state, separate from the printer's own status. |
| Printhead alignment | sensor (diagnostic) | How the last alignment went. A **failed** alignment is a real fault — the printer is online and prints, but output can be skewed or banded — and no counter here would otherwise reveal it. The failure reason is attached as an attribute. Needs a human at the machine; this integration never triggers one. |
| Paper level | sensor | Remaining paper in the main input tray, as a percentage, read over IPP. Only the main sheet-feed tray is watched: a document feeder is excluded, because "low" on it means nothing. Absent on models that do not describe their tray. |
| Paper low | binary_sensor | `on` when the tray is below the level the manufacturer defines as low. |
| Paper present | binary_sensor | `on` when the main input tray holds media. |
| Auto jam recovery | binary_sensor | Whether the printer retries a jam automatically. |
| Quiet mode | binary_sensor | Whether the printer is in its quieter mode. |
| Low-ink messaging | binary_sensor | Whether the firmware's low-ink warning is switched on. Recorded because a model with no ink level sensor has the feature but cannot act on it — that pair is the answer to "why is my printer not warning me". |
| Pages printed | sensor (total_increasing) | Lifetime page count. See [A note on "pages printed"](#a-note-on-pages-printed) — this is the engine total on LEDM printers. |
| Black and white pages | sensor (total_increasing) | Monochrome impressions. Add to **Color pages** for what was actually printed. |
| Color pages | sensor (total_increasing) | Color impressions. |
| Single-sided sheets | sensor (total_increasing) | Simplex sheets. |
| Double-sided sheets | sensor (total_increasing) | Duplex sheets. |
| Normal / Better / Draft quality pages | sensor (total_increasing) | Pages by the quality the job asked for. These are **sums across media types**, not the number the device reports for any one of them — `UsageByQuality` repeats each entry once per media type, and taking the first gives you only the plain-paper figure. |
| Photo pages | sensor (total_increasing) | Photo impressions. |
| Ink used | sensor (total_increasing) | Millilitres of ink the engine has drawn. The clearest available evidence that pages came from bottled refills. LEDM only. |
| Non-HP part count | sensor (diagnostic) | Times the device has seen a cartridge it could not authenticate. A record that this happened, not a verdict on any particular cartridge. |
| Panel button presses | sensor (total_increasing) | Presses on the front panel. A jump between polls usually means someone was at the machine cancelling jobs — usually a paper problem the jam and mispick counters do not show. |
| Panel cancel presses | sensor (total_increasing) | Presses specifically on cancel. |
| Paper jams | sensor (total_increasing) | Cumulative jam events. |
| Mispicks | sensor (total_increasing) | Cumulative mispick events. Watch for an upward trend — a pickup roller is glazing over long before paper starts jamming. |
| Power cycles | sensor (diagnostic) | Times the device has been power-cycled — a proxy for how often it loses power or is hard-reset, which nothing else reports. CDP only. |
| Color pages on genuine supplies | sensor (total_increasing) | Color impressions printed with HP-marked cartridges. |
| Black and white pages on genuine supplies | sensor (total_increasing) | Same, monochrome. |
| Installed | sensor (diagnostic, date) | When the printer was installed. Only the CDP identity document carries this, so it is absent on LEDM models rather than showing a placeholder date. |
| Firmware date | sensor (diagnostic) | Build date of the installed firmware — the only version marker LEDM exposes. |
| Auto-off time | sensor (diagnostic, disabled by default) | How long the printer waits before powering itself down, as free text the firmware chooses (`never`, `2minutes`). Kept as text on purpose: the accepted spellings are not an enumeration, and converting to minutes would invent precision the device does not offer. |
| Last event code | sensor (diagnostic) | Most recent fault code (`13.x` paper jams, `49.x` firmware faults, `10.x` supply-memory errors). Full history and any firmware assert text are attached as attributes. |
| Last event at page | sensor (diagnostic) | Page count at which the most recent event occurred. |
| Manufactured | sensor (diagnostic, disabled by default, date) | When the printer was built, from `ProductInformation/Manufacturer`. Many models — including the M182nw — do not report it, and then no entity is created. |
| Power save timeout | sensor (diagnostic, disabled by default) | The sleep delay the printer is configured to use. |
| Language pack version | sensor (diagnostic, disabled by default) | The revision of the language pack. |
| Last job source | sensor (diagnostic, disabled by default) | Application that initiated the most recent print job, with user, name, and page count attached as attributes. |
| Network errors | sensor (diagnostic) | Every network error counter added together — bad packets, framing errors, collisions, late collisions, unsendable packets — with each one attached as an attribute, along with the port type and link mode. A rising value means the link is degrading: a marginal cable or a failing switch port. Nothing else the printer exposes says so. Absent on models that do not serve `IOConfigDyn`. |
| Link mode | sensor (diagnostic, disabled by default) | Negotiated speed and duplex, e.g. `100TX_FULL`. |
| Bad packets received, Framing errors, Transmit collisions, Late collisions, Unsendable packets, Packets received, Packets transmitted | sensor (diagnostic, disabled by default) | The individual counters behind **Network errors**, for when one number is not enough. |
| Firmware fault recorded | binary_sensor (diagnostic) | `on` when the printer still has assert text from a recorded firmware crash. This is a *recorded* fault, not a live one — to catch new faults, trigger on the last event code changing. |
| Genuine supplies enforced | binary_sensor (diagnostic) | `on` when the printer refuses third-party cartridges. A firmware update can switch this back on and stop a working printer. |
| Admin password set | binary_sensor (diagnostic) | `on` when the EWS admin password is configured. It gates *writes* only — LEDM reads stay open either way, which is why this integration needs no credentials. |

### Scanner

These appear only when the printer has a scanner subunit.

| Entity | Type | Notes |
|---|---|---|
| Pages scanned | sensor (total_increasing) | All scan images. |
| Pages scanned from feeder | sensor (total_increasing) | Pages pulled through the ADF. |
| Pages scanned from glass | sensor (total_increasing) | Pages scanned from the flatbed. |
| Double-sided sheets scanned | sensor (total_increasing) | Duplex sheets pulled through the feeder. Feederless models do not report it. |
| Scan job pages | sensor (total_increasing) | Pages captured by a scan job. The four "scan job" counters come from the scan application, not the scanner engine: the engine counts every pass it makes, so its totals include copies. On an M182nw the engine's 962 flatbed images are 929 scan-job pages plus 35 copies. |
| Scan job pages from feeder | sensor (total_increasing) | Scan-job pages pulled through the ADF. |
| Scan job pages from glass | sensor (total_increasing) | Scan-job pages taken from the flatbed. |
| Double-sided scan job sheets | sensor (total_increasing) | Duplex sheets scanned as part of a scan job. |
| Scanner jams | sensor (total_increasing) | Jam events attributed to the scanner. |
| Scanner mispicks | sensor (total_increasing) | Mispick events attributed to the scanner. |

### Copier

These appear only when the printer has a copy subunit.

| Entity | Type | Notes |
|---|---|---|
| Pages copied | sensor (total_increasing) | All copy impressions. |
| Black and white copies | sensor (total_increasing) | Monochrome copy impressions. |
| Color copies | sensor (total_increasing) | Color copy impressions. |
| Pages copied from feeder | sensor (total_increasing) | Copy impressions sourced from the ADF. |
| Pages copied from glass | sensor (total_increasing) | Copy impressions sourced from the flatbed. |

### Cartridges

Created per cartridge; the cartridge is its own sub-device because each one
has its own serial number and is replaced independently. The cartridge label
("Cartridge black", "Cartridge cyan", …) is generated from the colour and
type the printer reports.

| Entity | Type | Notes |
|---|---|---|
| Level | sensor | Manufacturer-rounded remaining percentage. The cartridge's slot (`station`) and type (`consumable_type`) are attached as attributes — both are fixed for the life of the cartridge, so they are not sensors of their own. **Read `consumable_type` before you alert on this:** a refillable ink tank (`inkTank`) has no level sensor and reports a fixed 100, while a real cartridge (`inkCartridge`) does not. An Smart Tank's printhead is also reported as an `inkCartridge`, so a low reading there means printhead life, not ink. |
| State reason | sensor (diagnostic, disabled by default) | Why the cartridge is in its current state, when the device says. |
| Supplier manufacture date | sensor (diagnostic, disabled by default) | The date as the supplier recorded it, kept as text — the precision varies by firmware, and truncating it would lose information rather than add clarity. |
| Pages remaining | sensor | `EstimatedPagesRemaining` for the installed cartridge. |
| Pages printed | sensor (total_increasing) | Lifetime impressions on the installed cartridge. |
| Brand | sensor (diagnostic) | "genuinehp" or "clone". HP labels third-party cartridges "clone" even when enforcement is off. |
| Part number | sensor (diagnostic, disabled by default) | The HP part number the printer expects. |
| Manufactured | sensor (diagnostic, date) | When the installed cartridge was manufactured. Devices without a real-time clock report `1976-01-01`; the parser discards that. |
| Installed | sensor (diagnostic, date) | When the cartridge was installed. Printers without a real-time clock report `1976-01-01`, which the parser discards, so the entity is absent on those models. |
| Warranty expires | sensor (diagnostic, disabled by default, date) | Cartridge warranty expiration. |
| Level (raw) | sensor (diagnostic, disabled by default) | Unrounded percentage. Useful when the rounded level sits at 1% for weeks. |
| Low threshold | sensor (diagnostic, disabled by default) | The manufacturer's own low threshold, so automations use a real value rather than guessing. |
| Unauthenticated refills | sensor (diagnostic, disabled by default) | Refills the cartridge chip recorded but could not authenticate. Not by itself a fault. |
| Genuine refills | sensor (diagnostic, disabled by default) | Refills the chip recorded as genuine. |
| Previous cartridge developer life | sensor (diagnostic, disabled by default) | Wear counter for the cartridge that was *removed* from this slot, not the one installed. |
| Previous cartridge drum life | sensor (diagnostic, disabled by default) | Drum wear for the removed cartridge. |
| Previous cartridge part number | sensor (diagnostic, disabled by default) | Part number of the removed cartridge. |
| Problem | binary_sensor | `on` when the cartridge state is anything other than the healthy set (`ok`, `newgenuinehp`, `new`, `good`). |
| Genuine | binary_sensor (diagnostic) | Whether the brand is HP or a clone.
| Previously used | binary_sensor (diagnostic) | `on` when the cartridge was already used in another printer. This is HP's **anti-transfer** flag, *not* a claim that the part is not genuine — the same document reports them separately. |
| Refilled | binary_sensor (diagnostic) | `on` when the cartridge has been refilled. |

## Maintenance buttons

If you enter the EWS admin password under *Advanced settings*, the printer
grows buttons for the maintenance operations **it reports itself as having**.
On a CDP model that is six:

| Button | What it runs | Cost |
|---|---|---|
| Clean ink paths (light / medium / strong) | Three escalating purge strengths. A smear or banding usually only needs the weakest. | Each level is a longer purge. Level 3 is the expensive one — do not reach for it first. |
| Clean paper feed | Clears the path that paper travels, not the printhead. | Small. This is the one for repeated misfeeds. |
| Clean rib smear | Wipes the printhead surface where a smear builds up. | Small. |
| Align printhead | Re-runs the printhead alignment. **Needs paper in the input tray** — it prints a test pattern to align against. | A page or two of ink. |

The three ink strengths and the two mechanism-specific cycles are separate
operations on separate systems. A paper-feed clean does nothing for a smear on
the printhead, so they are separate buttons rather than one "clean" button
that hides the choice.

### What these buttons will and will not do

- **They only exist for operations your printer lists.** The list is read
  from the printer's own service document at setup. A model with no
  level-3 purge gets no level-3 button, because the alternative is a button
  that answers "this printer does not offer that".
- **They only exist if a password is configured.** Without one the write
  cannot succeed, and a visible button that quietly does nothing is worse
  than no button — you cannot tell it from a broken feature.
- **They never fire on their own.** No poll, restart, reload, or repair can
  reach them. They run when you press them, which is the point: each one
  spends ink and paper, and that should be a decision rather than a
  schedule.
- **A press is not a completion.** The printer acknowledges the request and
  runs the cycle on its own; a clean takes minutes. The button confirms the
  request was *accepted*. Watch the printer's own status for when it is done.
- **The printer's reason for refusing is passed through.** Busy, no paper and
  a wrong password all arrive as an error, and only the device can tell them
  apart — so its message is what you see.

Alignment is refused up front if the printer reports its input tray empty. A
printer that reports no paper level at all is not treated as empty, because
that would leave the button permanently unpressable on exactly the machines
where an alignment is most likely to have failed.

## Dashboard

`examples/dashboards/printers.yaml` is a ready-made view. It builds itself
from `auto-entities` filtered by device and translation key rather than from a
list of entity IDs, so it survives you renaming a printer in Home Assistant —
which, since entity IDs are derived from the name, is the thing most likely to
happen.

It needs [card-mods](https://github.com/thomasloven/lovelace-card-mod) for the
`auto-entities` card. The two `picture-entity` cards at the top are the only
part that needs editing, and only once: they show the printer itself, using the
512×512 render every HP printer already serves from its own web server.

```yaml
image: http://192.168.9.20/images/printer-large.png
```

Point that at your printer. There is nothing to host and nothing to update when
the firmware changes — the picture always comes from the machine it depicts. It
does mean Home Assistant has to be able to reach the printer's HTTP port, which
is the same requirement as setup itself.

## Troubleshooting

If setup cannot connect, confirm that the printer's EWS is reachable from the
Home Assistant host. Open the printer's host and port in a browser first; most
printers use HTTP on port 80. Enable **HTTPS** only when the printer's EWS is
configured for it.

**The device is online but shows no entities.** That is a different problem
from an unreachable printer, and the integration treats it as one: a model that
speaks neither LEDM nor CDP produces a device with a serial and nothing else.
Compare the host against the printer's own Network Summary page — the common
cause is a printer that was replaced by a different model under the same
address, or a captive portal on the network intercepting the request.

**Paper level never appears.** Only the main sheet-feed tray is watched, and
only over IPP on port 631. A model that does not describe its tray gets no
entity rather than a permanently-100% one. Check that 631 is reachable from
the Home Assistant host; if it is not, everything else still works.

**A cartridge level looks stuck at 100%.** Read the `consumable_type`
attribute. `inkTank` means a refillable reservoir with no sensor, and 100 is
the device's placeholder — there is nothing behind that number.

For logs, enable debug logging from the integration device page:

1. Open **Settings → Devices & Services**.
2. Open **HP Printers** and select the printer.
3. Open the three-dot menu and select **Enable debug logging**.
4. Reproduce the problem, then return to the menu and select **Disable debug
   logging**.

When reporting a problem, also download diagnostics from the same menu. The
diagnostic file redacts the printer host and serial identifiers while retaining
the parsed device data needed to investigate unsupported models and missing
entities.

## Compatibility

Developed and tested against real hardware, not just fixtures:

| Model | Interface | Notable |
|---|---|---|
| HP Color LaserJet MFP M182nw | LEDM | The original target. No install date, no refill counters, no ADF or duplex counters, and a `1976-01-01` manufacture date because it has no real-time clock. |
| HP Smart Tank 750 series | LEDM | Ink-tank AIO. Reports an engine total above its own printed page count, meters ink in millilitres, and serves paper level over IPP. |
| HP Smart Tank 580-590 series | CDP | Answers 404 to every LEDM path. This model is the reason the CDP client exists: it reports its install date, its power cycles and its last printhead alignment result, none of which LEDM carries — and none of which the other two report. |

Both consumer models have a refillable ink tank with no level sensor, so
**neither reports usable ink level**; the tank sensors read a fixed 100. This
is a hardware fact, not a parsing gap, and no amount of querying will change
it — the CDP model states it outright, with
`isMediaElectronicLevelSensingSupported: false`. Their printheads *are*
reported as `inkCartridge` and do carry a real percentage — which is life,
not ink.

The maintenance buttons need a CDP model. An LEDM model does not get them:
its `DiscoveryTree.xml` lists 24 resources and none of them is a maintenance
endpoint, and the `MaintenanceManifest.xml` paths that its own web interface
references all answer 404.

Reports of other models working (or not) are welcome.

## A note on LEDM and CDP

HP publishes no specification for either. The endpoint map here was derived by
reading live devices. For LEDM, `/DevMgmt/DiscoveryTree.xml` enumerates the
available resources, and each is exposed as a paired `<Resource>Cap.xml` —
describing types, access modes and legal values — and `<Resource>Dyn.xml`
carrying current values. The device is, in effect, its own documentation.

CDP has the same thing at `/cdm/servicesDiscovery`: 31 services, 90 links,
and each link carries the HTTP methods it accepts, so it is the authority on
both which endpoints exist and how they are called. An earlier version of this
README said no such document existed and that the endpoint list had been
recovered by exhausting namespaces by hand. That was wrong, and it was wrong
in a way that mattered — the same document is where the cleaning and alignment
operations come from.

### What is read, and what is not

Every read is a `GET`, and no read requires a credential. The only non-`GET`
requests this integration can make are the [maintenance buttons](#maintenance-buttons),
and those are reachable only when a person presses one.

The devices publish considerably more than is used here. 47 of the 90 links
the CDP model advertises accept `post`, `patch`, `put` or `delete`, including
factory reset, firmware upload, Wi-Fi reconfiguration, certificate management
and the password-change endpoint. None of it is touched.

## Contributing

Contributions are welcome — this integration exists because the existing
options were not great, and there is plenty left to fix on the devices I
cannot test against.

### Reporting a bug

Please open an issue using the **Bug report** template. To make it actionable
include:

- Your Home Assistant version (Settings → About).
- Your printer model and firmware date (the **Firmware date** sensor).
- The integration's debug log.
- The diagnostics file (Devices & Services → HP Printers → ⋮ → Download
  diagnostics).

Diagnostics intentionally redact the printer host, serial, UUID, and user
identifiers, but keep the parsed payloads — that is what makes it possible to
investigate unsupported models and missing entities without seeing your
network.

### Proposing a feature

Open an issue using the **Feature request** template. LEDM is undocumented,
so the most useful contributions are *evidence first*: capture the relevant
endpoint response from your printer and describe what you would surface from
it. A feature without the source data usually has to wait for someone with
the same printer to confirm the field.

> [!IMPORTANT]
> **Raw LEDM XML and CDP JSON both identify your device and your network** —
> serial number, UUID, hostname, MAC address, IP addresses, and your
> cartridges' serial numbers. Do not paste a raw `curl` response into a
> public issue.

Two ways to share it safely:

- **Diagnostics file** (easiest): Devices & Services → HP Printers → ⋮ →
  Download diagnostics. Already redacted, and it carries the parsed LEDM
  payloads.
- **Anonymized capture** (best, if you can run Python on your network):

  ```bash
  ./.venv/bin/python scripts/capture_ledm.py --host <your-printer>
  ./.venv/bin/python scripts/anonymize_ledm.py scripts/captures/<dir>/
  ```

  For a CDP printer, use `scripts/capture_cdp.py` and
  `scripts/anonymize_cdp.py` instead — the JSON documents need a different
  scrubber, and the anonymizer skips non-JSON files rather than mangling them.

  The first is read-only — every request is a `GET`. The second replaces
  identifiers with stable dummies and prints every replacement it makes.
  Read that output before you attach anything: it is a best-effort filter
  over the fields we know about, not a guarantee about a model we have
  never seen. Anonymized captures are also what `tests/fixtures/` is made
  of, so a good one can ship as a permanent regression test for your
  model.

### Opening a pull request

1. Fork the repository and create a branch from `main`.
2. Optionally install the hooks so the checks run on every commit:
   `./.venv/bin/uv pip install --python .venv/bin/python pre-commit` then
   `./.venv/bin/pre-commit install`. The hooks are the CI commands, run
   against this repository's venv.
3. Make the change. Run the verification commands from `AGENTS.md`:
   - `./.venv/bin/ruff check --config ruff_ha.toml custom_components/hp_printers tests`
   - `./.venv/bin/ruff format --check --config ruff_ha.toml custom_components/hp_printers tests`
   - `./.venv/bin/python -m compileall -q custom_components/hp_printers tests`
   - `./.venv/bin/python -m pytest -q`
4. **Any change that alters functionality must ship with tests.** A bug fix
   gets a regression test, a new entity gets coverage of the parser path and
   the entity description it depends on, and a refactor keeps the existing
   tests green. A PR that changes behaviour without touching `tests/` will
   be asked to add tests before it can merge.
5. Update `custom_components/hp_printers/translations/en.json` and the
   matching `entity:` block in `strings.json` for any new or renamed
   user-visible entity.
6. Use the **Pull request** template; label the PR with `bug`, `enhancement`,
   `documentation`, `breaking`, or `chore`. Labels group the change in the
   generated release notes.
7. CI must be green on the PR before review. Do not touch `version` in
   `manifest.json`: releases are cut automatically on merge, using Home
   Assistant's `YEAR.MONTH.RELEASE` scheme, and the workflow sets that
   field itself.

## License

MIT

The integration icon is original artwork. `brand-icon-mdi.svg` is an unused
alternative derived from the `printer` glyph in
[Material Design Icons](https://pictogrammers.com/library/mdi/) by the
Pictogrammers group, used under the Apache License 2.0.
