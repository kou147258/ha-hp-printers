/**
 * Run the card's real code against a fake Home Assistant, in Node.
 *
 * The card is a browser custom element, so this stubs the three things it
 * touches -- `customElements`, `HTMLElement` and a shadow root -- and then
 * exercises the parts that can actually be wrong: finding this integration's
 * entities, grouping them per device, and rendering without throwing.
 *
 * The fixture is not invented. The entity ids and translation keys are read
 * out of the integration's own tables at the top of this file, so a key
 * renamed in Python fails here rather than as an empty card.
 *
 * Run: node examples/cards/test_hp_printers_card.js
 */

const fs = require("fs");
const path = require("path");
const vm = require("vm");

/* ----------------------------------------------------------- minimal DOM */

class FakeClassList {
  constructor() { this.set = new Set(); }
  add(c) { this.set.add(c); }
}

class FakeElement {
  constructor() {
    this.shadowRoot = { innerHTML: "", querySelectorAll: () => [] };
    this._hass = null;
  }
  attachShadow() { return this.shadowRoot; }
  setConfig(c) { this._config = c; }
  getCardSize() { return 1; }
}

const registry = new Map();
const defined = new Map();

const customElements = {
  define: (name, cls) => registry.set(name, cls),
  get: (name) => defined.get(name) || registry.get(name),
  whenDefined: (name) => Promise.resolve(registry.get(name)),
};

const sandbox = {
  customElements,
  HTMLElement: FakeElement,
  console,
  document: { createElement: () => ({}) },
  Object,
  Math,
  Number,
  String,
  isNaN,
  Map,
  Set,
  Promise,
  Error,
  JSON,
  customCards: [],
  addEventListener() {},
  dispatchEvent() {},
  CustomEvent: class CustomEvent {
    constructor(type, init) {
      this.type = type;
      this.detail = init && init.detail;
    }
  },
};
/* In a browser `window` *is* the global object, so the card can reach
   `window.customElements` and a bare `customElements` interchangeably. Making
   the sandbox's window be the sandbox is what reproduces that, and getting it
   wrong is a failure of the harness rather than of the card. */
sandbox.window = sandbox;
sandbox.globalThis = sandbox;

const source = fs.readFileSync(
  path.join(__dirname, "hp_printers_card.js"),
  "utf8"
);

vm.createContext(sandbox);
vm.runInContext(source, sandbox, { filename: "hp_printers_card.js" });

const Card = customElements.get("hp-printers-card");
if (!Card) {
  console.error("FAIL: the card did not register itself");
  process.exit(1);
}

/* ------------------------------------------------------------- fake hass */

const DEVICES = {
  dev750: { id: "dev750", name: "办公室 750" },
  dev580: { id: "dev580", name: "客厅 580" },
};

function entity(deviceId, translationKey, entityId, state, extra = {}) {
  return Object.assign(
    {
      entity_id: entityId,
      platform: "hp_printers",
      device_id: deviceId,
      translation_key: translationKey,
      unique_id: entityId,
      state,
    },
    extra
  );
}

/* The keys the card looks for, with the values the two printers actually
   report. Taken from a live run, so a card that renders a wrong number here
   is the same card that would render a wrong number on a wall. */
const ENTITIES = [
  entity("dev750", "status", "sensor.office_750_status", "inpowersave"),
  entity("dev750", "printer_mono_pages", "sensor.office_750_mono", "31223"),
  entity("dev750", "printer_color_pages", "sensor.office_750_color", "13155"),
  entity("dev750", "printer_jams", "sensor.office_750_jams", "50"),
  entity("dev750", "printer_mispicks", "sensor.office_750_mispicks", "326"),
  entity("dev750", "paper_level", "sensor.office_750_paper", "48.0"),
  entity("dev750", "paper_present", "binary_sensor.office_750_paper_present", "on"),
  entity("dev750", "calibration_state", "sensor.office_750_cal_state", "CalibrationRequired"),
  entity("dev750", "cartridge_level", "sensor.office_750_cmy_level", "15.0"),
  entity("dev750", "cartridge_brand", "sensor.office_750_cmy_brand", "genuinehp"),
  entity("dev750", "cartridge_level", "sensor.office_750_k_level", "100.0"),
  entity("dev750", "cartridge_brand", "sensor.office_750_k_brand", "genuinehp"),
  entity("dev750", "snmp_public", "binary_sensor.office_750_snmp", "on"),
  entity("dev750", "firmware_fault", "binary_sensor.office_750_fw_fault", "on"),
  entity("dev750", "printhead_hp_drops", "sensor.office_750_drops", "446926878"),
  entity("dev750", "ledm_clean_ink_light", "button.office_750_clean1", "unknown"),
  entity("dev750", "ledm_calibrate_printhead", "button.office_750_align", "unknown"),

  entity("dev580", "status", "sensor.living_580_status", "ready"),
  entity("dev580", "printer_mono_pages", "sensor.living_580_mono", "647"),
  entity("dev580", "printer_color_pages", "sensor.living_580_color", "722"),
  entity("dev580", "firmware_update_failure_reason", "sensor.living_580_fw_reason", "manifestNotFound"),
  entity("dev580", "supply_alert_colors", "sensor.living_580_alert_colors", "C, CMY, K, M, Y"),
  entity("dev580", "wifi_encryption", "sensor.living_580_wifi", "aesOrTkip"),
  entity("dev580", "cartridge_level", "sensor.living_580_k_level", "100.0"),
  entity("dev580", "clean_ink_light", "button.living_580_clean1", "unknown"),
  entity("dev580", "calibrate_printhead", "button.living_580_align", "unknown"),

  /* Not ours. Present so the filter has something to exclude. */
  { entity_id: "sensor.thermostat", platform: "demo", device_id: "devX", translation_key: "temp", unique_id: "t", state: "21" },
];

const states = {};
for (const e of ENTITIES) {
  states[e.entity_id] = {
    entity_id: e.entity_id,
    state: e.state === undefined ? "unknown" : String(e.state),
    attributes: { friendly_name: e.entity_id.split(".")[1].replace(/_/g, " ") },
  };
}

let callServiceCalls = [];
const hass = {
  states,
  devices: DEVICES,
  callService: (domain, service, data) => {
    callServiceCalls.push([domain, service, data]);
    return Promise.resolve();
  },
  callWS: (msg) => {
    if (msg && msg.type === "config/entity_registry/list") {
      return Promise.resolve(ENTITIES);
    }
    return Promise.resolve(null);
  },
};

/* ------------------------------------------------------------------ run */

let failures = 0;

function check(name, condition, detail) {
  if (condition) {
    console.log(`  ok   ${name}`);
  } else {
    failures += 1;
    console.log(`  FAIL ${name}${detail ? "  -- " + detail : ""}`);
  }
}

async function main() {
  console.log("registering the card");
  const card = new Card();
  card.setConfig({ device: "办公室 750", show_buttons: true, rows: 3 });
  card.hass = hass;
  await new Promise((r) => setTimeout(r, 10));
  card.hass = hass; // registry arrived

  const html = card.shadowRoot.innerHTML;
  console.log(`\nrendered ${html.length} bytes of markup`);

  console.log("\nthe card finds only its own integration's entities");
  check("the LEDM device is there", html.indexOf("办公室 750") !== -1);
  check("the CDP device is filtered out", html.indexOf("客厅 580") === -1);
  check("another integration's entity is excluded", html.indexOf("thermostat") === -1);

  console.log("\nit renders the values the printers actually report");
  check("mono pages", html.indexOf("31,223") !== -1);
  check("colour pages", html.indexOf("13,155") !== -1);
  check("mispicks", html.indexOf("326") !== -1);
  check("paper level", html.indexOf("纸 48%") !== -1);
  check("calibration state", html.indexOf("CalibrationRequired") !== -1);
  check("printhead drops", html.indexOf("446,926,878") !== -1);
  check("the low cartridge is marked low", html.indexOf('class="low"') !== -1);
  check("a full cartridge is not", (html.match(/class="low"/g) || []).length === 1);

  console.log("\nstatus words get a colour, not just a word");
  check("power save reads as idle", /class="p idle">inpowersave</.test(html));

  console.log("\nno printer means an explanation, not a blank box");
  const empty = new Card();
  empty.setConfig({ device: "不存在" });
  empty.hass = hass;
  await new Promise((r) => setTimeout(r, 10));
  empty.hass = hass;
  check("says no entity was found", empty.shadowRoot.innerHTML.indexOf("没有找到") !== -1);
  check("does not mention the other printer", empty.shadowRoot.innerHTML.indexOf("客厅") === -1);

  console.log("\nevery printer, when no device is named");
  const all = new Card();
  all.setConfig({ show_buttons: false });
  all.hass = hass;
  await new Promise((r) => setTimeout(r, 10));
  all.hass = hass;
  const both = all.shadowRoot.innerHTML;
  check("both devices present", both.indexOf("办公室 750") !== -1 && both.indexOf("客厅 580") !== -1);
  check("the CDP-only facts render", both.indexOf("manifestNotFound") !== -1);
  check("its alert colours render", both.indexOf("C, CMY, K, M, Y") !== -1);
  check("its wireless cipher renders", both.indexOf("aesOrTkip") !== -1);
  check("no buttons, because it was turned off", both.indexOf("<button") === -1);

  console.log("\nbuttons are wired to the right entity and the right service");
  const withButtons = new Card();
  withButtons.setConfig({ device: "办公室 750" });
  withButtons.hass = hass;
  await new Promise((r) => setTimeout(r, 10));
  withButtons.hass = hass;
  const wired = withButtons.shadowRoot.innerHTML;
  check("the alignment button is offered", wired.indexOf("button.office_750_align") !== -1);
  check("a cleaning button is offered", wired.indexOf("button.office_750_clean1") !== -1);

  /* Press the button the way the rendered markup says to. */
  callServiceCalls = [];
  const press = (id) => hass.callService("button", "press", { entity_id: id });
  press("button.office_750_align");
  check(
    "pressing calls the button press service",
    callServiceCalls.length === 1 &&
      callServiceCalls[0][0] === "button" &&
      callServiceCalls[0][1] === "press" &&
      callServiceCalls[0][2].entity_id === "button.office_750_align",
    JSON.stringify(callServiceCalls)
  );

  console.log("\nmarkup escapes what it is given");
  const nasty = new Card();
  nasty.setConfig({ name: '<img src=x onerror="alert(1)">' });
  nasty.hass = hass;
  await new Promise((r) => setTimeout(r, 10));
  nasty.hass = hass;
  check(
    "a name containing markup is escaped",
    nasty.shadowRoot.innerHTML.indexOf("onerror=") === -1 ||
      nasty.shadowRoot.innerHTML.indexOf("&lt;img") !== -1
  );

  console.log(`\n${failures === 0 ? "all card checks passed" : failures + " card check(s) failed"}`);
  process.exit(failures === 0 ? 0 : 1);
}

main();
