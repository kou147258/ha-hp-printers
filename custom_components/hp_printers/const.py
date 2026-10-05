"""Constants for the HP Printers integration."""

from datetime import timedelta
from typing import Final

DOMAIN: Final = "hp_printers"

MANUFACTURER: Final = "HP"

# Polling. HP consumer printers sleep aggressively and at least some models
# (e.g. the M182nw) have firmware faults on the sleep/wake path, so we
# deliberately do not poll as fast as the device would allow.
DEFAULT_SCAN_INTERVAL: Final = timedelta(seconds=60)
DEFAULT_PORT: Final = 80
DEFAULT_PORT_SSL: Final = 443
DEFAULT_SSL: Final = False

# The IPP port is a separate question from the web-server port: the embedded
# web server answers on 80/443 while IPP answers on 631, and a printer
# configured for IPPS serves it on the same port. This is the same
# distinction the config flow already makes for the zeroconf-advertised IPP
# port, so the default stays fixed rather than following ``DEFAULT_PORT``.
DEFAULT_IPP_PORT: Final = 631
CONF_IPP_PORT: Final = "ipp_port"
CONF_IPP_SSL: Final = "ipp_ssl"

CONF_SCAN_INTERVAL_SECONDS: Final = "scan_interval_seconds"
MIN_SCAN_INTERVAL_SECONDS: Final = 15
MAX_SCAN_INTERVAL_SECONDS: Final = 3600

# LEDM ("Low End Data Model") endpoints. HP does not publish a specification
# for these; the device self-describes via DiscoveryTree.xml plus paired
# <Resource>Cap.xml / <Resource>Dyn.xml documents.
ENDPOINT_DISCOVERY: Final = "/DevMgmt/DiscoveryTree.xml"
ENDPOINT_PRODUCT_CONFIG: Final = "/DevMgmt/ProductConfigDyn.xml"
ENDPOINT_PRODUCT_STATUS: Final = "/DevMgmt/ProductStatusDyn.xml"
ENDPOINT_PRODUCT_USAGE: Final = "/DevMgmt/ProductUsageDyn.xml"
ENDPOINT_CONSUMABLE_CONFIG: Final = "/DevMgmt/ConsumableConfigDyn.xml"
ENDPOINT_PRODUCT_LOGS: Final = "/DevMgmt/ProductLogsDyn.xml"
# Network adaptor configuration and error counters. Not every model
# advertises it, so it is fetched tolerantly: a printer without it still
# updates normally, it just grows no network entities.
ENDPOINT_IO_CONFIG: Final = "/DevMgmt/IOConfigDyn.xml"

# Paper handling. TrayState/MediaState are the only paper signal LEDM offers:
# present or absent, with no level. The percentage lives in IPP and is read
# by api_ipp.py.
ENDPOINT_MEDIA_HANDLING: Final = "/DevMgmt/MediaHandlingDyn.xml"

# CDP ("Common Data Platform") endpoints. HP publishes no specification for
# these either, and the map was read off a live device: the paths appear in
# the printer's own web UI scripts and in the ``resourcePath`` fields of its
# own alert payloads. Models that serve this layer answer 404 on
# DiscoveryTree.xml, which is how the two protocols are told apart at setup.
#
# Every version segment here was measured, not assumed -- a wrong version
# returns 404 and looks exactly like an absent feature.
CDP_IDENTITY: Final = "/cdm/system/v1/identity"
CDP_SYSTEM_STATUS: Final = "/cdm/system/v1/status"
CDP_SYSTEM_STATISTICS: Final = "/cdm/system/v1/statistics"
CDP_DEVICE_USAGE: Final = "/cdm/deviceUsage/v1/lifetimeCounters"
CDP_DEVICE_SERVICE_COUNTERS: Final = "/cdm/deviceUsage/v1/serviceCounters"
CDP_SUPPLIES: Final = "/cdm/supply/v1/suppliesPublic"
CDP_SUPPLY_CONFIG: Final = "/cdm/supply/v1/configPublic"
CDP_PRINT_STATUS: Final = "/cdm/print/v2/status"
CDP_PRINT_CONFIG: Final = "/cdm/print/v2/configuration"
CDP_SCAN_STATUS: Final = "/cdm/scan/v1/status"
CDP_EVENTS: Final = "/cdm/diagnostic/v1/systemEvents"
CDP_SECURITY_CONFIG: Final = "/cdm/security/v1/deviceAdminConfig"

# Endpoints fetched once at setup rather than on every poll.
STATIC_ENDPOINTS: Final = (ENDPOINT_PRODUCT_CONFIG,)

# StatusCategory values observed across HP LEDM devices. The device may report
# a value outside this set; entities fall back to the raw string.
STATUS_OPTIONS: Final = [
    "cancelling",
    "closedoorcover",
    "copying",
    "inpowersave",
    "initializing",
    "nomediainstalled",
    "off",
    "outofpaper",
    "papermisfeed",
    "processing",
    "ready",
    "scanning",
    "shuttingdown",
    "trayempty",
    "unknown",
]

# ConsumableLifeState/Brand. "clone" is HP's term for a non-HP cartridge; it is
# reported even when GenuineHPSuppliesOnly enforcement is disabled.
BRAND_GENUINE: Final = "genuinehp"
BRAND_CLONE: Final = "clone"

# Noun used in a consumable's device name, chosen from ConsumableTypeEnum.
# "Cartridge" is a reasonable default for both toner and ink; a printhead is
# the case where it would be plainly wrong. Capability documents are
# device-specific -- a laser declares only "toner" -- so unknown values fall
# back rather than being guessed at.
#
# These are English words, and they are only needed to label a consumable
# whose colour is not one this integration recognises. Every recognised
# noun/colour pair is named by looking up a translation instead -- see
# consumable_device_key.
CONSUMABLE_NOUNS: Final = {
    "printhead": "Printhead",
    "inktank": "Ink Tank",
    "drum": "Drum",
    "maintenancekit": "Maintenance Kit",
    # Consumer ink-tank models report their **printheads** as
    # ``inkCartridge`` -- verified on Smart Tank 750 and 580-590. Without this
    # key the noun falls back to "Cartridge" and the entity is named "Black
    # Cartridge" for what is a printhead, and the printhead's remaining-life
    # percentage is presented as though it were ink left in a tank. The value
    # is capitalised for the translation key; the English label is rendered by
    # the translation itself.
    "inkcartridge": "Printhead",
    "inkcartridges": "Printhead",
}
DEFAULT_CONSUMABLE_NOUN: Final = "Cartridge"

# ConsumableLabelCode -> MarkerColor, as the two documents spell it.
COLOR_NAMES: Final = {
    "K": "black",
    "C": "cyan",
    "M": "magenta",
    "Y": "yellow",
    "CMY": "tricolor",
}
KNOWN_COLORS: Final = frozenset(COLOR_NAMES.values())

# Sub-device name keys, looked up from the ``subunit`` field rather than from
# a label the caller has to remember to pass alongside it.
SUBUNIT_KEYS: Final = {
    "scanner": "subunit_scanner",
    "copy": "subunit_copier",
}


def consumable_device_key(noun: str, color: str | None) -> str:
    """Return the device translation key that names a consumable sub-device.

    The printer reports both halves of the name in English, so a translated
    name has to be a lookup rather than a string assembled at runtime. Every
    known noun/colour pair is its own key under the ``device`` block, which is
    also what lets a translation put the two in its own order: English reads
    "Black Cartridge", Chinese reads "黑色墨盒".

    A colour outside ``KNOWN_COLORS`` is a model this integration has never
    seen, and guessing at a key would render the raw key as the device name.
    Those take the fallback, which interpolates the English label -- the same
    name the integration showed before there were any translations.
    """
    if color not in KNOWN_COLORS:
        return "consumable_other"
    return f"consumable_{'_'.join(noun.strip().lower().split())}_{color}"
