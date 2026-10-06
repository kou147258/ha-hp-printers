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
CDP_CALIBRATION: Final = "/cdm/calibration/v1/calibration/penAlignSemiauto"

# --- write endpoints -------------------------------------------------------
#
# These are the only requests this integration ever makes that are not a GET,
# and none of them are made on a poll: each one is a button the user presses.
# The paths and the methods come from the device's own service discovery
# document (/cdm/servicesDiscovery), which lists every link together with the
# HTTP methods that link accepts. That document is the authority here -- it is
# how "PATCH this" was established rather than assumed.
CDP_REPORTS: Final = "/cdm/report/v1/reports"
CDP_REPORT_PRINT: Final = "/cdm/report/v1/print"
CDP_CALIBRATION_TRIGGER: Final = "/cdm/calibration/v1/calibration"
CDP_CALIBRATION_CAPABILITIES: Final = "/cdm/calibration/v1/capabilities"

# --- the device's own setup, alerts, firmware, security ---------------------
#
# Each of these was found by reading /cdm/servicesDiscovery and then opening
# what it listed, not by guessing a path. The list is deliberately not a walk
# of the discovery tree: a poll that fetched 89 documents every minute to
# surface six values would be a worse integration than the one it replaced.
# These are the ones that answer a question a user actually asks.
CDP_SETUP_STATUS: Final = "/cdm/deviceSetup/v1/status"
CDP_ALERTS: Final = "/cdm/alert/v1/alerts"
CDP_FIRMWARE_STATUS: Final = "/cdm/firmwareUpdate/v2/updateStatus"
CDP_FIRMWARE_CHECK: Final = "/cdm/firmwareUpdate/v2/updateCheck"
CDP_FIRMWARE_CONFIG: Final = "/cdm/firmwareUpdate/v2/configuration"
CDP_CERTIFICATE: Final = "/cdm/certificate/v1/certificates/selfSignedCertificate"
CDP_ADAPTER_STATS: Final = "/cdm/ioConfig/v2/adapterStats"
CDP_INTERNET_DIAGNOSTICS: Final = "/cdm/network/v1/internetDiagnostics"
CDP_PRINT_SERVICES: Final = "/cdm/network/v1/printServices"
CDP_SNMP_CONFIG: Final = "/cdm/network/v1/snmpConfig"
CDP_BLUETOOTH: Final = "/cdm/ble/v1/configuration"
CDP_SERVICE_CONFIG: Final = "/cdm/system/v1/serviceConfig"
CDP_SUPPLY_LIFETIME: Final = "/cdm/supply/v1/lifetimeCounters"
CDP_SUPPLY_CONFIG_PRIVATE: Final = "/cdm/supply/v1/configPrivate"
CDP_SUPPLY_REGION_RESET: Final = "/cdm/supply/v1/regionReset"
CDP_PRINT_SETUP_STATUS: Final = "/cdm/print/v2/setupStatus"

# --- CDP documents that were served and never opened ------------------------
#
# Each of these answers 200 on the model measured and was not in the list
# above, because the list was built from what the integration already needed
# rather than from what the device offers. They are grouped by what they add.
#
# The wireless configuration is the one to read carefully. It carries the
# network's SSID and its pass phrase in clear text, and neither is read here:
# an SSID is the user's network name and a pass phrase is a credential, and
# neither belongs in a state attribute or a diagnostics download. What is read
# from it is the security posture -- band, authentication and encryption mode.
CDP_SUPPLY_ALERTS: Final = "/cdm/supply/v1/alerts"
CDP_FIRMWARE_HISTORY: Final = "/cdm/firmwareUpdate/v2/updateHistory"
CDP_WIRELESS_CONFIG: Final = "/cdm/ioConfig/v2/wirelessConfig"
CDP_MEDIA_CONFIG: Final = "/cdm/media/v1/configuration"
CDP_SYSTEM_CONFIGURATION: Final = "/cdm/system/v1/configuration"
CDP_PROXY_CONFIG: Final = "/cdm/network/v1/proxyConfig"

# Pause before retrying a static document that came back empty.
#
# The CDP models fail the TLS handshake with BAD_SIGNATURE when too many
# handshakes overlap, and _fetch_optional turns that into an empty document
# rather than an error -- so on the slow path, where a miss costs six hours,
# the request is worth repeating once. The pause is long enough for the burst
# that dropped it to finish. See _fetch_retrying in api_cdp.py.
CDP_SLOW_RETRY_DELAY_SECONDS: Final = 2.0


# An LEDM printer answers a handful of /cdm/ documents alongside its XML, and
# two of them carry values LEDM itself does not expose: the quiet-print flag
# and the control panel's language. Fetching them on an LEDM printer is not a
# fallback -- it is the only place those two values exist.
CDP_LEDM_QUIET_MODE: Final = "/cdm/print/v1/printModeConfiguration"
CDP_LEDM_PANEL: Final = "/cdm/controlPanel/v1/configuration"
CDP_LEDM_INSTANT_INK: Final = "/cdm/consumableSubscription/v1/info"

# --- LEDM documents that were advertised but never opened ------------------
#
# The *Cap.xml documents are the device's own specification: each one declares
# the type, range, step and access mode of every field its Dyn partner
# carries. They are the systematic way to find what the parser is not reading
# -- the alternative is guessing field names, which is how the CDP endpoint
# list was built before /cdm/servicesDiscovery turned up.
ENDPOINT_PRINT_CONFIG: Final = "/DevMgmt/PrintConfigDyn.xml"
ENDPOINT_MEDIA_DYN: Final = "/DevMgmt/MediaDyn.xml"
ENDPOINT_NET_APPS: Final = "/DevMgmt/NetAppsDyn.xml"
ENDPOINT_SHOP_FOR_SUPPLIES: Final = "/DevMgmt/ShopForSupplies.xml"

# --- the LEDM maintenance interface ---------------------------------------
#
# Not in DiscoveryTree.xml, not in the web application's own paths, and the
# only way it was found: the page the printer ships to its browser reads the
# capability document from here before it offers a button. The document
# itself enumerates the 19 job types this model supports, three of which are
# cleaning cycles and one of which is the printhead clean.
#
# GET on the Dyn resource answers 404 with an empty body -- it exists and it
# only accepts the write. The client's own bundle POSTs here with an XML body
# whose single element names the job.
ENDPOINT_INTERNAL_PRINT_CAP: Final = "/DevMgmt/InternalPrintCap.xml"
ENDPOINT_INTERNAL_PRINT_DYN: Final = "/DevMgmt/InternalPrintDyn.xml"
ENDPOINT_USAGE_CAP: Final = "/DevMgmt/ProductUsageCap.xml"
ENDPOINT_CONSUMABLE_CAP: Final = "/DevMgmt/ConsumableConfigCap.xml"

# --- the LEDM calibration interface ----------------------------------------
#
# Unlike the internal-print surface, this one *is* in DiscoveryTree.xml -- at
# "/Calibration/CalibrationManifest.xml", with the manifest owner as a path
# segment of its own. The printer's own page addresses the same resources as
# "/Calibration/Session", dropping the manifest name, which is the same
# shorthand "/DevMgmt/InternalPrintDyn.xml" is.
#
# That prefix is the whole reason this interface was written off as absent:
# 324 candidate paths built on the pattern "/CalibrationManifest.xml/..."
# every one of them 404, because the real document is one segment deeper. A
# manifest found in the discovery tree beats a path pattern, and reading it
# first would have cost one request.
ENDPOINT_CALIBRATION_CAP: Final = "/Calibration/Capabilities"
ENDPOINT_CALIBRATION_STATE: Final = "/Calibration/State"
ENDPOINT_CALIBRATION_SESSION: Final = "/Calibration/Session"

# The namespace the printer declares on its own calibration manifest, read out
# of the manifest's root element rather than guessed. It is the only
# namespace on this device outside the ".../con/ledm/..." tree -- it sits
# under "cnx" -- so it could not have been inferred from the others, and a
# plausible-looking URI in the wrong namespace is accepted-looking enough to
# survive review.
NS_CALIBRATION: Final = (
    "http://www.hp.com/schemas/imaging/con/cnx/markingagentcalibration/2009/04/08"
)

# The element /Calibration/Session accepts, and the state it takes to start an
# alignment. Both come from the manifest's own resource map, which pairs the
# URI with the element the body is expected to carry. The routine this starts
# is advertised in /Calibration/Capabilities under "Alignment"; that token is a
# button identity, so it lives with the buttons rather than here.
CALIBRATION_SESSION_ELEMENT: Final = "CalibrationState"
CALIBRATION_ALIGNMENT_STATE: Final = "Printing"

# The cleaning operations are deliberately NOT named here. The device lists
# them in /cdm/report/v1/reports with its own identifiers, and button.py is
# the single place those identifiers are mapped onto buttons. A second table
# in constants would be a second thing to keep in step with the hardware, and
# the two would drift exactly the way a hand-written endpoint list drifts.

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
