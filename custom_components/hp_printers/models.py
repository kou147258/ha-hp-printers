"""Data models for the HP Printers integration."""

from dataclasses import dataclass, field
from datetime import date, datetime


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
    # How long the printer waits before powering itself down, as free text the
    # firmware chooses ("never", "2minutes"). Kept as text: the accepted
    # spellings are not an enumeration, and mapping them to minutes would
    # invent precision the device does not offer.
    auto_off_time: str | None = None
    quiet_mode: bool | None = None

    # --- LEDM product configuration -----------------------------------
    #
    # Read from the document already fetched on the slow cadence, and kept
    # here rather than on PrinterData so that a 60-second poll does not have
    # to re-read a configuration that does not change.
    available_memory_kb: int | None = None
    total_memory_kb: int | None = None
    country_region: str | None = None
    device_language: str | None = None
    product_derivative_number: str | None = None
    # The out-of-box setup phase, folded onto CDP's vocabulary so that one
    # sensor means one thing on either protocol. On the machine measured this
    # reads "complete", which is why its printhead alignment is not the
    # pending-step failure the CDP model reports.
    setup_phase: str | None = None
    # Fitted hardware and a setting, kept apart because they disagree on a
    # real machine: the Smart Tank 750 measured has an installed duplexer with
    # ten thousand double-sided sheets printed, while its auto-duplex setting
    # reads "disabled". One field would report a duplexer that does not exist.
    duplexer_installed: bool | None = None
    auto_duplex_enabled: bool | None = None
    # Failed sign-in attempts left before the EWS locks: a password-guessing
    # budget, and the reason a factory-default admin password matters.
    failed_attempts_remaining: int | None = None


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
class ActiveAlert:
    """One alert the device is currently raising.

    Distinct from :class:`EventLogEntry`, which is history. An alert is
    something the machine is saying right now and may clear on its own, so
    the pair of them answers "is anything wrong" and "is anything *happening*"
    separately.
    """

    alert_id: int | None = None
    category: str | None = None
    severity: str | None = None
    priority: int | None = None
    sequence: int | None = None

    # The detail block, which the device nests one level down rather than
    # putting beside Severity. It is where the actionable part of a supply
    # alert lives: an alert saying "genuineHP" is a complaint with no subject,
    # and ``marker_color`` is the subject. Without these, the most common alert
    # this integration sees arrives without saying which colour it is about.
    #
    # Each is optional because the block only carries the details that apply to
    # the alert's category -- a jam alert has no marker colour, and a colour
    # alert has no jam location.
    marker_color: str | None = None
    marker_location: str | None = None
    consumable_type: str | None = None
    # What the device says the user should do about it. Verbatim rather than
    # mapped onto advice: it is a device vocabulary, and inventing friendly
    # wording for a token nobody has read would be a claim the printer never
    # made.
    user_action: str | None = None
    string_id: int | None = None
    # The document the detail lives in, so "go look" is one request away.
    resource_uri: str | None = None


@dataclass(frozen=True, slots=True)
class AdapterStats:
    """Traffic and error counters for one network interface.

    Per interface rather than aggregate, because the aggregate cannot tell a
    printer that is working over Wi-Fi from one whose cable is unplugged: both
    report a small number, and only the split shows which port is live.
    """

    name: str
    received_bytes: int | None = None
    transmitted_packets: int | None = None
    received_unicast: int | None = None
    received_multicast: int | None = None
    receiver_errors: int | None = None
    transmitter_errors: int | None = None
    transmitter_collisions: int | None = None
    transmitter_late_collisions: int | None = None

    @property
    def error_total(self) -> int:
        """Return every error counter added together.

        Collisions and late collisions are counted separately by the device
        but are the same physical fault seen twice, so they are summed here
        rather than left for a caller to remember to include.
        """
        return sum(
            value or 0
            for value in (
                self.receiver_errors,
                self.transmitter_errors,
                self.transmitter_collisions,
                self.transmitter_late_collisions,
            )
        )


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

    # --- selected facts the devices report but this integration had not read ---
    # Whether the printer will take a job right now. More directly useful
    # than the status word for a dashboard, and on a CDP model it is the only
    # "ready for work" signal available.
    accepting_jobs: bool | None = None
    # Panel button presses. A jump between polls means someone was standing
    # at the machine cancelling jobs -- usually a paper problem the jam and
    # mispick counters do not show.
    panel_button_presses: int | None = None
    # Pages by the quality the job asked for. ``UsageByQuality`` repeats
    # these once per media type, so these are **sums across media types**,
    # not the single number the device reports for any one of them.
    normal_quality_pages: int | None = None
    better_quality_pages: int | None = None
    draft_quality_pages: int | None = None
    # Photo impressions appear once, in the print application's own block.
    photo_quality_pages: int | None = None
    # Images the scan application sent to a host. Sits between the scanner
    # engine's total (which includes copies) and the scan job's own count.
    scan_to_host_images: int | None = None
    # Times the device has seen a cartridge it could not authenticate. A
    # record that this happened, not a verdict on any particular cartridge.
    non_hp_flag_count: int | None = None
    quiet_mode: bool | None = None
    auto_jam_recovery: bool | None = None
    # How the last printhead alignment went. A failed alignment is a real
    # fault -- the printer is online and prints, but its output can be skewed
    # or banded -- and no other counter here would reveal it.
    calibration_last_result: str | None = None
    calibration_failure_reason: str | None = None
    calibration_status: str | None = None

    # Where an alignment currently is, as the device's own state machine
    # reports it (``ScanRequested``, ``Printing``, ...).
    #
    # This is the other half of the maintenance button. An alignment is a
    # two-party job: the printer prints a pattern and then waits for the user
    # to put it on the scanner glass. The button can only say the request was
    # accepted, and without this the wait is invisible -- the printer sits on
    # "ScanRequested" looking idle while the user is told it is working.
    #
    # Kept as the raw token on purpose. The vocabulary is the device's, the
    # set is not published, and mapping known words onto friendlier ones would
    # hide a state this integration has not been taught rather than report it.
    calibration_state: str | None = None

    # ------------------------------------------------------------------
    # Setup progress.
    #
    # A device reports the first-time setup checklist and marks each step.
    # On the CDP model measured, the alignment step is still ``pending``
    # while ``calibration_last_result`` is ``failed`` -- so the failure is
    # not a broken printhead but a setup step that was never completed. That
    # is a different problem with a different fix, and the result field on
    # its own cannot tell them apart.
    # ------------------------------------------------------------------
    setup_operation_state: str | None = None
    setup_pending_steps: tuple[str, ...] = ()

    # ------------------------------------------------------------------
    # Firmware.
    #
    # The measured CDP model has auto-update enabled, no update available,
    # and a history in which every attempt failed. None of that is visible
    # from the firmware build date, which is the only version marker the
    # read path had before.
    # ------------------------------------------------------------------
    firmware_update_result: str | None = None
    firmware_update_available: str | None = None
    auto_update_enabled: bool | None = None

    # ------------------------------------------------------------------
    # Alerts currently raised, as opposed to the event log, which is a
    # record of what happened. The two answer different questions and the
    # gap is the useful part: a healthy event log with a live alert is a
    # machine that is fine and is complaining right now.
    # ------------------------------------------------------------------
    active_alerts: tuple[ActiveAlert, ...] = ()

    # ------------------------------------------------------------------
    # Security and health facts that have no other home.
    # ------------------------------------------------------------------
    # The self-signed certificate the EWS is reached over. It is issued for
    # ten years and nothing warns when it runs out; on that day HTTPS access
    # stops working and the reason is not obvious.
    certificate_expires: date | None = None
    certificate_valid_from: date | None = None
    # Per-interface counters. The LEDM side reports one aggregate set; CDP
    # separates them, which is what tells a printer that is on Wi-Fi from
    # one whose ethernet port is dead.
    adapter_stats: tuple[AdapterStats, ...] = ()
    internet_diagnostics_result: str | None = None
    carriage_status: str | None = None
    # How many cartridges have occupied each slot, ever.
    cartridge_changes: int | None = None
    # A consumable-protection counter with a countdown. Some region-reset
    # schemes allow a fixed number of attempts and then stop.
    region_reset_remaining: int | None = None
    anti_theft_enabled: bool | None = None
    holo_enabled: bool | None = None
    low_messaging_enabled: bool | None = None
    # Network services that are on are attack surface, so "on" is the state
    # worth surfacing. The values are the keys, so a model with a service
    # this one lacks simply does not get that entity.
    print_services: tuple[str, ...] = ()
    snmp_enabled: bool | None = None
    snmp_public_allowed: bool | None = None
    bluetooth_beaconing: bool | None = None
    service_id: str | None = None

    # ------------------------------------------------------------------
    # The LEDM printers' small CDP layer.
    #
    # An LEDM model answers a handful of /cdm/ documents, and two of them
    # carry values the LEDM side does not expose at all -- quiet mode and
    # the control panel's language. Read on the model that has them rather
    # than reported as absent, because "absent" would be wrong.
    # ------------------------------------------------------------------
    quiet_print_mode: bool | None = None
    panel_language: str | None = None
    # LEDM's print configuration. The quality and resolution are the
    # settings, not the measured output, so they describe how the machine is
    # configured rather than what it has produced.
    print_quality: str | None = None
    resolution_setting: str | None = None
    default_copies: int | None = None
    borderless_printing: bool | None = None
    current_media_type: str | None = None
    current_media_size: str | None = None
    # The instant-ink programme, when the model offers one. Empty on a
    # printer that was never enrolled.
    instant_ink_status: str | None = None
    # Model name with the SKU and region code appended, e.g.
    # "Smart Tank 750 series:28B72A:0". The identity document carries only the
    # model half, and the region code is what makes two otherwise identical
    # machines distinguishable in a device list.
    full_model_string: str | None = None

    # ------------------------------------------------------------------
    # LEDM: jobs.
    #
    # The usage document counts jobs per subunit and breaks each one into an
    # outcome. ``JobDuration`` and ``PagesPerJob`` are not numbers at all --
    # the device reports a bucket ("lessthanTwoMinutes", "sixToTen") -- so
    # they are left as the device's own words rather than turned into a mean
    # that the distribution does not support.
    # ------------------------------------------------------------------
    print_job_count: int | None = None
    job_successes: int | None = None
    job_failures: int | None = None
    job_cancelled: int | None = None
    job_skipped: int | None = None
    # How pages reached the printer. The split matters when someone asks
    # whether the wireless path is actually being used.
    network_printed_pages: int | None = None
    wireless_printed_pages: int | None = None
    subscription_printed_pages: int | None = None
    # How many times the printer's own web interface has been opened.
    ews_access_count: int | None = None

    # ------------------------------------------------------------------
    # LEDM: hardware and identity, from the product configuration.
    #
    # On ProductInfo, not PrinterData: the document is already read there on
    # the slow cadence and none of these changes between polls.
    # ------------------------------------------------------------------
    # Kilobytes, as the device reports them.
    available_memory_kb: int | None = None
    total_memory_kb: int | None = None
    country_region: str | None = None
    device_language: str | None = None
    product_derivative_number: str | None = None
    # The duplexer being fitted is a hardware fact; auto-duplex being on is a
    # setting. They are separate fields because they disagree on a real
    # machine: the Smart Tank 750 measured has an installed duplexer with
    # 10,216 double-sided sheets printed, while its auto-duplex setting reads
    # "disabled". Collapsing them would report a duplexer that does not exist.
    duplexer_installed: bool | None = None
    auto_duplex_enabled: bool | None = None
    # Failed sign-in attempts still remaining before the EWS locks. A
    # password-guessing budget, and the reason the factory default matters.
    failed_attempts_remaining: int | None = None

    # ------------------------------------------------------------------
    # LEDM: trays and what else is switched on that another host can reach.
    # ------------------------------------------------------------------
    # Portrait or Landscape: the orientation a job gets when the driver says
    # nothing. A settings value, not a measurement of what came out.
    default_orientation: str | None = None
    input_tray_count: int | None = None
    output_bin_count: int | None = None
    default_input_tray: str | None = None
    default_output_bin: str | None = None
    # The LEDM spelling of the exposure the CDP side reports as print
    # services. Both printers measured answer raw printing on port 9100.
    port_9100_enabled: bool | None = None
    direct_print_enabled: bool | None = None
    https_redirection_enabled: bool | None = None
    web_scan_enabled: bool | None = None
    llmnr_enabled: bool | None = None

    @property
    def setup_incomplete(self) -> bool | None:
        """Return whether the device is still waiting on a setup step.

        ``None`` when the device does not publish a checklist at all, which is
        every LEDM model measured. Reporting False there would be the
        integration asserting that setup is complete on a machine that never
        said anything about it -- and "off" reads as a positive answer, not
        as an absence of one.
        """
        if self.setup_operation_state is None and not self.setup_pending_steps:
            return None
        return bool(self.setup_pending_steps)

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
