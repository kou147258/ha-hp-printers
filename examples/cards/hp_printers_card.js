/**
 * HP Printers -- a standalone Lovelace card.
 *
 * No dependencies. Not card-mod, not a build step, not a token.
 *
 * Why it can work without a token
 * -------------------------------
 * A dashboard card is handed the whole `hass` object, including `hass.states`
 * with every entity state the user can already see, and `hass.callWS` for the
 * registries. So the card needs no credential of its own and no polling of its
 * own -- it reads what Home Assistant already refreshed. That matters here
 * because this integration talks to printers on a local network and holds an
 * EWS password for the maintenance buttons; a card that asked for a token to
 * do the same job would be a second, worse way into the same machine.
 *
 * It finds its entities the same way the integration names them rather than by
 * entity id, so renaming a printer does not break the card:
 *
 *   entities from config/entity_registry/list, filtered on
 *   platform == "hp_printers" and, if given, the device's friendly name.
 *
 * Install
 * -------
 *   1. Copy this file to <config>/www/hp_printers_card.js
 *      (in the UI: Settings -> Files, the www folder, upload)
 *   2. Settings -> Dashboards -> (top right) Resources -> Add
 *      URL: /local/hp_printers_card.js
 *      Type: JavaScript module
 *   3. Add the card:
 *
 *      type: custom:hp-printers-card
 *      device: 办公室 750        # optional; omit to show every printer
 *
 * Config keys
 * -----------
 *   device        string, optional   friendly name of the device; omit for all
 *   image         string, optional   image URL; defaults to the one the
 *                                    printer serves itself
 *   host          string, optional   the printer's address, used to build that
 *                                    default image URL
 *   show_buttons  boolean, default true
 *   rows          number,  default 3   metrics per row
 */

const PLATFORM = "hp_printers";

/* States worth colouring, from the device's own vocabulary rather than ours. */
const STATE_CLASS = {
  ready: "ok",
  idle: "ok",
  inpowersave: "idle",
  off: "idle",
  shutingdown: "idle",
  initializing: "busy",
  processing: "busy",
  copying: "busy",
  scanning: "busy",
  calibrating: "busy",
  outofpaper: "bad",
  trayempty: "bad",
  trayemptyoropen: "warn",
  papermisfeed: "bad",
  closeoorcover: "warn",
  carriagejam: "bad",
  error: "bad",
};

class HpPrintersCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = {};
    this._hass = null;
    this._registry = null;
    this._registryFor = null;
  }

  /* ---------------------------------------------------------- configuration */

  setConfig(config) {
    if (config && config.type && config.type.indexOf("custom:hp-printers-card") !== 0) {
      throw new Error("hp-printers-card: wrong type in config");
    }
    this._config = Object.assign({ show_buttons: true, rows: 3 }, config || {});
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this._ensureRegistry();
    this._render();
  }

  get hass() {
    return this._hass;
  }

  getCardSize() {
    const printers = this._printers();
    return 3 + (printers.length ? printers.length * 4 : 1);
  }

  getGridOptions() {
    return { columns: 12, rows: Math.max(4, this.getCardSize()) };
  }

  /* ------------------------------------------------------------- discovery */

  /**
   * Fetch the entity registry once, then never again unless the entity count
   * changes.
   *
   * The registry is where ``platform`` and ``translation_key`` live -- a state
   * object carries neither, so there is no way to recognise this integration's
   * entities from ``hass.states`` alone. Fetching it on every render is what
   * makes a card feel slow, because a card re-renders on every state change.
   *
   * ``hass.callWS`` needs no token: the dashboard is already authenticated, and
   * a card asking for a credential to re-read data the user can already see
   * would be a second, worse way into the same machine.
   */
  _ensureRegistry() {
    if (!this._hass || typeof this._hass.callWS !== "function") return;
    const count = Object.keys(this._hass.states || {}).length;
    if (this._registry && this._registryFor === count) return;
    if (this._registryPending) return;
    this._registryPending = true;

    this._hass
      .callWS({ type: "config/entity_registry/list" })
      .then((entries) => {
        this._registry = new Map();
        for (const e of entries || []) {
          if (e && e.platform === PLATFORM && e.entity_id) {
            this._registry.set(e.entity_id, e);
          }
        }
        this._registryFor = count;
        this._registryPending = false;
        this._render();
      })
      .catch(() => {
        /* No registry, no card. The empty state says so rather than
           rendering an empty box with no explanation. */
        this._registryPending = false;
        this._registry = new Map();
        this._registryFor = count;
        this._render();
      });
  }

  /**
   * Group this integration's entities per device, each carrying its state.
   *
   * Grouped by device rather than by entity id so that renaming a printer in
   * Home Assistant does not break the card, which is the same reason the
   * integration's own dashboard filters on translation keys.
   */
  _printers() {
    if (!this._hass) return [];
    const states = this._hass.states || {};
    const devices = this._hass.devices || {};
    const registry = this._registry;

    const groups = new Map();
    for (const [id, stateObj] of Object.entries(states)) {
      const entry = registry && registry.get(id);
      if (!entry) continue;
      const attrs = stateObj.attributes || {};
      let device = groups.get(entry.device_id);
      if (!device) {
        device = {
          id: entry.device_id,
          name: (devices[entry.device_id] && devices[entry.device_id].name) || "",
          entities: [],
          byKey: new Map(),
        };
        groups.set(entry.device_id, device);
      }
      const item = { id, state: stateObj.state, attrs, entry };
      device.entities.push(item);
      const key = entry.translation_key;
      if (key) {
        if (!device.byKey.has(key)) device.byKey.set(key, []);
        device.byKey.get(key).push(item);
      }
    }

    const wanted = (this._config.device || "").trim();
    const all = Array.from(groups.values()).filter((g) => g.name);
    const picked = wanted
      ? all.filter((g) => g.name === wanted || g.name.indexOf(wanted) !== -1)
      : all;
    for (const g of picked) {
      g.image = g.entities.map((e) => e.attrs.entity_picture).find(Boolean) || "";
    }
    return picked;
  }

  /* --------------------------------------------------------------- rendering */

  _render() {
    if (!this._hass) return;
    const printers = this._printers();
    this.shadowRoot.innerHTML = `<style>${CSS}</style>` + (printers.length
      ? printers.map((p) => this._one(p)).join("")
      : this._empty());
    this._wire();
  }

  _empty() {
    const wanted = this._config.device ? `名为「${this._config.device}」的` : "";
    if (this._registryPending) {
      return `<div class="wrap"><div class="none"><b>正在读取实体…</b></div></div>`;
    }
    if (!this._registry) {
      return `<div class="wrap"><div class="none">
        <b>读不到实体注册表</b>
        <div>这张卡片通过 Home Assistant 的 websocket 读取注册表来认出本集成的实体，
        不需要 token。如果这里出现，通常是资源没有以「JavaScript 模块」类型添加，
        或浏览器控制台有报错。</div>
      </div></div>`;
    }
    return `<div class="wrap"><div class="none">
      <b>没有找到${wanted} HP 打印机实体</b>
      <div>集成是否已添加？本卡片只读取已经建好的实体，不会自己去连打印机。</div>
      <div>如果刚添加完集成，等一次刷新即可（默认 60 秒），或到「设备与服务」里确认实体已生成。</div>
    </div></div>`;
  }

  _one(g) {
    const cfg = this._config;
    const name = cfg.name || g.name || "HP 打印机";
    const image =
      cfg.image ||
      (cfg.host ? `http://${cfg.host}/images/printer-large.png` : "") ||
      g.entities.map((e) => e.attrs.entity_picture).find(Boolean) ||
      "";

    const status = this._first(g, ["status"]);
    const cls = STATE_CLASS[String(status && status.state).toLowerCase()] || "idle";

    const pills = [this._pill(status && status.state, cls)];
    for (const key of ["paper_present", "paper_level", "calibration_state"]) {
      const e = this._first(g, [key]);
      if (!e) continue;
      if (key === "paper_level") {
        const pct = Number(e.state);
        if (!isNaN(pct)) pills.push(this._pill(`纸 ${pct}%`, pct < 20 ? "bad" : "ok"));
      } else if (key === "paper_present") {
        pills.push(this._pill(`纸 ${e.state === "on" ? "在" : "无"}`, e.state === "on" ? "ok" : "bad"));
      } else {
        pills.push(this._pill(`校准 ${e.state}`, "busy"));
      }
    }
    /* Binary switches: shown by label, coloured by whether they are on.
       A binary sensor that is off is not news, so it is left out -- except
       the safety ones, where "off" is the reassuring reading and its absence
       would be ambiguous. */
    for (const [key, label, showWhenOff] of [
      ["firmware_fault", "固件故障", false],
      ["setup_incomplete", "设置未完成", false],
      ["genuine_supplies_only", "仅原装", false],
      ["snmp_public", "SNMP public", true],
      ["low_ink_messaging", "低墨提示", true],
    ]) {
      const e = this._first(g, [key]);
      if (!e || e.state === "unknown" || e.state === "unavailable") continue;
      const on = e.state === "on";
      if (!on && !showWhenOff) continue;
      pills.push(this._pill(label, on ? "bad" : "ok"));
    }

    /* Sensor facts: the value is the message, so it is always shown. These
       are never switches, and running them through the switch rule above hid
       every one of them -- a firmware failure reason of "manifestNotFound" is
       not "off", so it was dropped. */
    for (const [key, label, kind] of [
      ["supply_alert_colors", "告警", "warn"],
      ["firmware_update_failure_reason", "固件", "bad"],
      ["wifi_encryption", "Wi-Fi", "idle"],
      ["https_redirection", "EWS 明文", "warn"],
    ]) {
      const e = this._first(g, [key]);
      if (!e || !e.state || e.state === "unknown" || e.state === "unavailable") continue;
      pills.push(this._pill(`${label} ${e.state}`, kind));
    }

    const metrics = [];
    for (const [key, label] of [
      ["printer_mono_pages", "黑白页"],
      ["printer_color_pages", "彩色页"],
      ["printer_jams", "卡纸"],
      ["printer_mispicks", "误取"],
      ["scanner_flatbed_images", "扫描"],
      ["copy_total_pages", "复印"],
      ["printer_duplex_sheets", "双面"],
      ["printhead_hp_drops", "累计墨滴"],
      ["printhead_non_hp_drops", "非HP墨滴"],
      ["cloud_printed_pages", "云打印"],
      ["ews_accesses", "网页访问"],
      ["printer_total_pages", "引擎总页"],
    ]) {
      const e = this._first(g, [key]);
      if (!e) continue;
      metrics.push(`<div class="m"><div class="k">${esc(label)}</div><div class="v">${esc(fmt(e.state))}</div></div>`);
    }

    const inks = [];
    const level = g.byKey.get("cartridge_level") || [];
    const brand = g.byKey.get("cartridge_brand") || [];
    for (let i = 0; i < Math.max(level.length, brand.length); i++) {
      const l = level[i];
      const b = brand[i];
      const pct = l ? Number(l.state) : NaN;
      const width = isNaN(pct) ? 0 : Math.max(0, Math.min(100, pct));
      const low = !isNaN(pct) && pct < 20;
      const name = (l && l.attrs.friendly_name) || (b && b.attrs.friendly_name) || `墨盒 ${i + 1}`;
      const sub = b ? esc(b.state) : "";
      inks.push(
        `<div class="i"><div class="n">${esc(name)}</div>` +
          `<div class="bar"><i class="${low ? "low" : ""}" style="width:${width}%"></i></div>` +
          `<div class="p">${isNaN(pct) ? "-" : pct + "%"}${sub ? " · " + sub : ""}</div></div>`
      );
    }

    const buttons = [];
    if (cfg.show_buttons) {
      const order = [
        "clean_ink_light",
        "clean_ink_medium",
        "clean_ink_strong",
        "ledm_clean_ink_light",
        "ledm_clean_ink_medium",
        "ledm_clean_ink_strong",
        "calibrate_printhead",
        "ledm_calibrate_printhead",
      ];
      for (const key of order) {
        const e = this._first(g, [key]);
        if (!e) continue;
        buttons.push(
          `<button class="btn" data-entity="${esc(e.id)}">${esc(e.attrs.friendly_name || key)}</button>`
        );
      }
    }

    const rows = Math.max(2, cfg.rows || 3);
    return `<div class="wrap">
      <div class="head">
        <div class="photo">${image ? `<img src="${esc(image)}" alt="">` : ""}</div>
        <div class="who">
          <div class="nm">${esc(name)}</div>
          <div class="pills">${pills.join("")}</div>
        </div>
      </div>
      ${metrics.length ? `<div class="sec"><div class="grid" style="--cols:${rows}">${metrics.join("")}</div></div>` : ""}
      ${inks.length ? `<div class="sec"><div class="inks">${inks.join("")}</div></div>` : ""}
      ${buttons.length ? `<div class="sec"><div class="btns">${buttons.join("")}</div></div>` : ""}
    </div>`;
  }

  _first(g, keys) {
    for (const key of keys) {
      const list = g.byKey.get(key);
      if (list && list.length) return list[0];
    }
    return null;
  }

  _pill(text, kind) {
    if (text === undefined || text === null || text === "") return "";
    return `<span class="p ${kind || "idle"}">${esc(text)}</span>`;
  }

  _wire() {
    for (const button of this.shadowRoot.querySelectorAll("button.btn")) {
      button.addEventListener("click", () => {
        const entity = button.getAttribute("data-entity");
        if (!entity || !this._hass) return;
        const [domain] = entity.split(".");
        this._hass.callService("button", "press", { entity_id: entity });
        void domain;
      });
    }
  }
}

const CSS = `
:host{display:block}
*{box-sizing:border-box}
.wrap{background:var(--ha-card-background,var(--card-background-color,#1c1f24));
border:1px solid var(--ha-card-border-radius,var(--divider-color,#2a2f37));
border-radius:var(--ha-card-border-radius,14px);
padding:14px;color:var(--primary-text-color,#e8eaed);font-size:14px;line-height:1.5}
.head{display:flex;gap:14px;align-items:center;margin-bottom:10px}
.photo{flex:0 0 150px;display:flex;align-items:center;justify-content:center;
background:linear-gradient(160deg,#232830,#181b20);border-radius:10px;padding:8px;min-height:120px}
.photo img{max-width:100%;max-height:130px;object-fit:contain}
.who{flex:1;min-width:0}
.nm{font-size:17px;font-weight:500;margin-bottom:7px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pills{display:flex;flex-wrap:wrap;gap:5px}
.p{padding:2px 9px;border-radius:999px;font-size:11.5px;font-weight:500;white-space:nowrap}
.ok{background:rgba(76,175,80,.16);color:#4caf50}
.idle{background:rgba(154,160,166,.14);color:#9aa0a6}
.busy{background:rgba(33,150,243,.16);color:#2196f3}
.warn{background:rgba(255,179,0,.16);color:#ffb300}
.bad{background:rgba(244,67,54,.16);color:#f44336}
.sec{border-top:1px solid var(--divider-color,#2a2f37);padding-top:10px;margin-top:10px}
.grid{display:grid;grid-template-columns:repeat(var(--cols,3),1fr);gap:8px}
.m{background:rgba(127,127,127,.06);border:1px solid var(--divider-color,#2a2f37);
border-radius:9px;padding:7px 10px;min-width:0}
.m .k{font-size:11.5px;color:var(--secondary-text-color,#9aa0a6);
overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.m .v{font-size:15px;font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.inks{display:grid;grid-template-columns:repeat(auto-fit,minmax(96px,1fr));gap:8px}
.i{background:rgba(127,127,127,.06);border:1px solid var(--divider-color,#2a2f37);
border-radius:9px;padding:7px 9px;min-width:0}
.i .n{font-size:12px;color:var(--secondary-text-color,#9aa0a6);
overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.bar{height:5px;border-radius:3px;background:rgba(127,127,127,.25);margin-top:6px;overflow:hidden}
.bar i{display:block;height:100%;background:var(--primary-color,#2196f3);border-radius:3px}
.bar i.low{background:#f44336}
.i .p{font-size:11px;color:var(--secondary-text-color,#9aa0a6);margin-top:5px;
overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.btns{display:flex;flex-wrap:wrap;gap:6px}
.btn{border:1px solid var(--divider-color,#2a2f37);border-radius:8px;padding:6px 11px;
font-size:12.5px;color:var(--primary-text-color,#e8eaed);background:rgba(127,127,127,.08);
cursor:pointer;font-family:inherit}
.btn:hover{background:rgba(33,150,243,.18);border-color:rgba(33,150,243,.4)}
.btn:active{transform:translateY(1px)}
.none{text-align:center;padding:18px 8px;color:var(--secondary-text-color,#9aa0a6)}
.none b{display:block;color:var(--primary-text-color,#e8eaed);margin-bottom:6px}
.none div{margin-top:4px;font-size:12.5px}
`;

function esc(value) {
  return String(value === undefined || value === null ? "" : value).replace(
    /[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

function fmt(value) {
  const n = Number(value);
  if (!isNaN(n) && value !== "" && value !== "unknown" && value !== "unavailable") {
    return n.toLocaleString();
  }
  return value === "" || value === undefined ? "-" : value;
}

if (!customElements.get("hp-printers-card")) {
  customElements.define("hp-printers-card", HpPrintersCard);
}
window.customCards = window.customCards || [];
if (!window.customCards.some((c) => c.type === "hp-printers-card")) {
  window.customCards.push({
    type: "hp-printers-card",
    name: "HP 打印机卡片",
    description: "显示 HP 打印机的状态、用量、墨水和维护按钮。零依赖。",
  });
}

window.customElements.whenDefined("hp-printers-card").then(() => {
  window.dispatchEvent(
    new CustomEvent("ll-rebuild", { detail: { card_type: "hp-printers-card" } })
  );
});
