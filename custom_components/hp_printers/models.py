"""Data models for the HP Printers integration."""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True, slots=True)
class ProductInfo:
    """Static device information, read once at setup.

    Sourced from /DevMgmt/ProductConfigDyn.xml.
    """

    make_and_model: str | None = None
    make_and_model_family: str | None = None
    serial_number: str | None = None
    product_number: str | None = None
    sku_identifier: str | None = None
    # When the printer itself was built, from ProductInformation/Manufacturer.
    # Devices without a real-time clock report a placeholder the parser drops.
    manufactured_at: datetime | None = None
    uuid: str | None = None
    service_id: str | None = None
    # Firmware build date. Exposed by the device but not surfaced by any other
    # HA integration; it is the only firmware version marker LEDM offers.
    firmware_date: str | None = None
    language_pack_version: str | None = None
    # Whether the EWS admin password has been set. Note this gates *writes*
    # only -- LEDM reads stay open either way.
    password_set: bool | None = None
    # When the printer was installed. Only the CDP identity document carries
    # this; LEDM's ProductConfigDyn has no equivalent for the consumer models
    # measured so far, so it is absent rather than zero on those.
    installed_at: datetime | None = None
    duplex_unit: str | None = None
    friendly_name: str | None = None
    power_save: str | None = None
    power_save_timeout: str | None = None
    shutdown_delay: str | None = None


@dataclass(frozen=True, slots=True)
class Consumable:
    """A single cartridge / consumable."""

    label_code: str
    color_name: str | None = None
    consumable_type: str | None = None
    brand: str | None = None
    state: str | None = None
    level_percent: float | None = None
    pages_remaining: int | None = None
    total_impressions: int | None = None
    station: int | None = None
    serial_number: str | None = None
    part_number: str | None = None
    max_capacity: int | None = None
    installed_at: datetime | None = None
    manufactured_at: datetime | None = None
    warranty_expires_at: datetime | None = None
    counterfeit_refills: int | None = None
    genuine_refills: int | None = None
    family_name: str | None = None
    # Finer-grained than ConsumablePercentageLevelRemaining, which is rounded.
    # The device reports a negative sentinel when it does not know.
    raw_level_percent: float | None = None
    # Wear counters for the cartridge that was REMOVED from this slot, not the
    # one currently installed. ConsumableConfigCap places all three under
    # ConsumableInfo/PreviousCartridgeData. They are declared as plain
    # integers rather than percentages, and use 127 as an unknown sentinel.
    previous_drum_life: int | None = None
    previous_developer_life: int | None = None
    previous_engine_toner_remaining: int | None = None
    previous_part_number: str | None = None
    previous_serial_number: str | None = None
    # The manufacturer's own low threshold, so automations need not guess.
    low_threshold_percent: float | None = None
    measured_state: str | None = None

    # --- Provenance, for the consumable kinds that carry it. ---
    # The CDP supply service states these as strings ("true"/"false"). They
    # are the fields that separate a genuine HP part from a refill and from a
    # part that has been in service before, which is exactly the distinction
    # an owner of an ink-tank printer cannot otherwise make.
    #
    # ``state_reasons`` is the device's own explanation, e.g.
    # ``usedConsumableInfo`` -- a used part that has been acknowledged, which
    # is NOT the same claim as "not genuine".
    is_genuine_reported: bool | None = None
    is_refilled: bool | None = None
    is_used: bool | None = None
    is_trial: bool | None = None
    is_setup: bool | None = None
    state_reasons: tuple[str, ...] = ()
    # ISO timestamp as the device reports it. Kept as text because the
    # precision varies by firmware and truncating it would lose information
    # rather than add clarity.
    manufacture_date: str | None = None

    @property
    def is_genuine(self) -> bool | None:
        """Return True when the device reports a genuine HP cartridge.

        Two sources, in order of trust. The CDP supply service states this
        outright as ``isGenuineHP``. LEDM does not, and has to be inferred
        from ``Brand`` -- which is where the ``clone`` wording comes from, and
        why the derivation is only as good as the brand string. A part that
        reports a brand of ``unknown`` is deliberately not called genuine.
        """
        if self.is_genuine_reported is not None:
            return self.is_genuine_reported
        if self.brand is None:
            return None
        return self.brand.lower().replace(" ", "") not in ("clone", "unknown")


@dataclass(frozen=True, slots=True)
class SubunitUsage:
    """Counters for one usage subunit (printer, scanner, copy...)."""

    total_impressions: int | None = None
    monochrome_impressions: int | None = None
    color_impressions: int | None = None
    simplex_sheets: int | None = None
    duplex_sheets: int | None = None
    jam_events: int | None = None
    mispick_events: int | None = None
    scan_images: int | None = None
    adf_images: int | None = None
    flatbed_images: int | None = None


@dataclass(frozen=True, slots=True)
class NetworkHealth:
    """The printer's network adaptor state and its error counters.

    ``status`` cannot report a network outage: the document is read over the
    same adaptor it describes, so an unreachable printer fails the fetch
    instead. What earns its place here are the error counters -- rising bad
    packets or collisions are evidence of a failing cable or switch port,
    which nothing else in LEDM reveals.
    """

    port_type: str | None = None
    status: str | None = None
    link_mode: str | None = None
    packets_received: int | None = None
    packets_transmitted: int | None = None
    bad_packets_received: int | None = None
    framing_errors: int | None = None
    transmit_collisions: int | None = None
    transmit_late_collisions: int | None = None
    unsendable_packets: int | None = None

    @property
    def error_counts(self) -> dict[str, int | None]:
        """Return the individual error counters, keyed for attributes."""
        return {
            "bad_packets_received": self.bad_packets_received,
            "framing_errors": self.framing_errors,
            "transmit_collisions": self.transmit_collisions,
            "transmit_late_collisions": self.transmit_late_collisions,
            "unsendable_packets": self.unsendable_packets,
        }

    @property
    def total_errors(self) -> int | None:
        """Return every error counter added together.

        ``None`` when the device reports none of them, so no entity is
        created rather than one that reads a misleading zero.
        """
        values = [value for value in self.error_counts.values() if value is not None]
        if not values:
            return None
        return sum(values)


@dataclass(frozen=True, slots=True)
class PaperTray:
    """One input tray as IPP describes it.

    ``level`` is a count in ``unit`` (sheets, or percent when the device
    measures it), never a fraction. ``level`` is None whenever the device
    does not report one -- including the ``-2`` sentinel a model without a
    paper sensor sends. "I cannot tell" and "the tray is empty" must stay
    distinct, because only the second is worth waking someone for.
    """

    name: str | None = None
    type: str | None = None
    level: int | None = None
    max_capacity: int | None = None
    unit: str | None = None

    @property
    def is_percent(self) -> bool:
        """Return True when the level is a percentage of capacity."""
        return (self.unit or "").strip().lower() == "percent"

    @property
    def has_level(self) -> bool:
        """Return True when the device reported a usable level."""
        return self.level is not None

    @property
    def level_percent(self) -> float | None:
        """Return the level as a percentage, or None when it cannot be derived.

        Only defined when both a level and a capacity are reported, or when
        the unit is already percent. A tray reporting "12 sheets" out of an
        unknown capacity has no percentage, and inventing one from a
        guessed capacity is how a level sensor ends up lying.
        """
        if self.level is None:
            return None
        if self.is_percent:
            return float(self.level)
        if self.max_capacity:
            return 100.0 * self.level / self.max_capacity
        return None


@dataclass(frozen=True, slots=True)
class EventLogEntry:
    """One entry from the device event log.

    ``severity`` is only reported by the CDP event service; LEDM's EventLog
    carries no severity and leaves it None rather than guessing one from the
    code.
    """

    sequence: int | None = None
    code: str | None = None
    impressions: int | None = None
    severity: str | None = None


@dataclass(frozen=True, slots=True)
class JobEntry:
    """One entry from the device's print job log."""

    application_id: str | None = None
    user_id: str | None = None
    name: str | None = None
    monochrome_impressions: int | None = None
    color_impressions: int | None = None
    total_impressions: int | None = None


@dataclass(frozen=True, slots=True)
class PrinterData:
    """Everything fetched on a single coordinator refresh."""

    status: str | None = None
    status_message: str | None = None
    consumables: dict[str, Consumable] = field(default_factory=dict)
    printer: SubunitUsage = field(default_factory=SubunitUsage)
    scanner: SubunitUsage = field(default_factory=SubunitUsage)
    # ScanApplicationSubunit counts pages captured by a scan job. The scanner
    # engine counts every pass it makes, so it also includes copies: on the
    # M182nw the engine's 962 flatbed images are the scan application's 929
    # plus 35 copies (a few passes predate the copy counter).
    scan: SubunitUsage = field(default_factory=SubunitUsage)
    copy: SubunitUsage = field(default_factory=SubunitUsage)
    events: list[EventLogEntry] = field(default_factory=list)
    jobs: list[JobEntry] = field(default_factory=list)
    genuine_color_impressions: int | None = None
    genuine_mono_impressions: int | None = None
    assert_text: str | None = None
    genuine_supplies_only: bool | None = None
    network: NetworkHealth = field(default_factory=NetworkHealth)
    # --- CDP-only ---
    # Times the device has been power-cycled. Useful as a proxy for how often
    # it loses power or is hard-reset, which nothing else reports.
    power_cycles: int | None = None
    scanner_status: str | None = None
    scanner_error: str | None = None
    # Whether the firmware's low-ink messaging is switched on. Recorded
    # because a model with no ink level sensor has the feature but cannot act
    # on it, and that pair is the answer to "why is my printer not warning me".
    low_ink_messaging: bool | None = None
    # Input trays, read over IPP. Empty on a model whose only tray is not
    # described, which is different from a model reporting zero trays.
    paper_trays: tuple[PaperTray, ...] = ()
    # --- LEDM-only, and the reason some of these cannot be cross-checked ---
    # Millilitres of ink the engine has drawn. The captured Smart Tank has
    # drawn 1.2 litres while reporting 0 ml ever shipped in a cartridge, which
    # is the clearest available evidence that the pages came from bottled
    # refills. None on a model that does not meter it.
    marking_agent_used_ml: float | None = None
    marking_agent_inserted_ml: float | None = None
    # Cancels pressed on the front panel. A jump here with no matching job
    # count usually means paper problems, which is what the jam and mispick
    # counters do not show.
    panel_cancel_presses: int | None = None
    # Whether the main input tray holds media. LEDM reports presence, not a
    # level, so this is the only paper signal available without IPP.
    paper_present: bool | None = None
    input_trays: tuple[str, ...] = ()

    @property
    def main_paper_tray(self) -> PaperTray | None:
        """Return the tray worth alerting on.

        The main tray is the one whose ``type`` names a sheet feed, or --
        failing that, simply the first the device lists. An automatic
        document feeder is excluded: it holds originals, is refilled
        constantly, and "low" on it means nothing.
        """
        for tray in self.paper_trays:
            tray_type = (tray.type or "").lower()
            if "sheetfeed" in tray_type or "cassette" in tray_type:
                return tray
        return self.paper_trays[0] if self.paper_trays else None

    @property
    def last_job(self) -> JobEntry | None:
        """Return the most recently recorded print job, if any."""
        return self.jobs[0] if self.jobs else None

    @property
    def last_event(self) -> EventLogEntry | None:
        """Return the most recent event log entry, if any."""
        if not self.events:
            return None
        return max(
            self.events,
            key=lambda e: e.sequence if e.sequence is not None else -1,
        )
