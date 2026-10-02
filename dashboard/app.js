"use strict";
/* Card data stays in the store.
 *
 * Polls /api/state and draws what the running system measured: every
 * particle is one real event. A blue particle is one swipe a register sent,
 * a green one is one record the warehouse stored, a red one is one record a
 * store kept, an amber one is one record waiting on the store's disk. Nothing
 * is generated here. The board never starts or stops a job; the presenter
 * controls drive only the registers, sensors and network links. */

const POLL_MS = 700;

const INSPECT_MS = 6000;

const RING_MS = 900;

const ALARM_MS = 2600;

const $ = (id) => document.getElementById(id);

const stage = $("stage");

const canvas = $("flow");

const ctx = canvas.getContext("2d");

const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

const SPEED = reduced ? 0.35 : 1;

const refs = { stores: new Map(), tills: new Map() };

const ringUntil = new Map();

const alarmUntil = new Map();

const seenQuarantine = new Set();

const particles = [];

let colors = {};

let current = null;

let previous = null;

let focusStore = null;

let inspectHover = false;

let inspectPinnedTxn = null;

let quarantineLoaded = false;

/* ------------------------------------------------------------- helpers */

function h(tag, cls, text) {
  const node = document.createElement(tag);

  if (cls) node.className = cls;

  if (text !== undefined) node.textContent = text;

  return node;
}

function svgUse(cls, symbol) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", cls);
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#${symbol}`);
  svg.append(use);

  return svg;
}

const fmt = (n) => Number(n || 0).toLocaleString("en-US");

const money = (cents, currency) =>
  `${currency === "EUR" ? "€" : ""}${(Number(cents) / 100).toFixed(2)}`;

const spaced = (pan) => String(pan || "").replace(/(.{4})/g, "$1 ").trim();

const tillName = (id) => `till ${String(id).split("-r")[1] || "?"}`;

function readColors() {
  const css = getComputedStyle(document.documentElement);
  const pick = (name) => css.getPropertyValue(name).trim();
  colors = {
    raw: pick("--raw"), ok: pick("--ok"), err: pick("--err"), warn: pick("--warn"),
    accent: pick("--accent"), line: pick("--line-strong"),
  };
}

async function control(verb, body) {
  try {
    await fetch(`/api/control/${verb}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch {
    /* the next poll shows whatever state the stores are really in */
  }
}

/* --------------------------------------------------------------- theme */

function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  $("theme").textContent = theme === "dark" ? "LIGHT" : "DARK";
  $("theme").setAttribute("aria-pressed", String(theme === "dark"));

  try {
    localStorage.setItem("pos-theme", theme);
  } catch {
    /* storage blocked: the toggle still works for this visit */
  }

  readColors();
}

$("theme").addEventListener("click", () =>
  setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark"));

/* --------------------------------------------------------- build stores */

function buildStores(stores) {
  const root = $("stores");
  root.textContent = "";
  refs.stores.clear();
  refs.tills.clear();

  for (const s of stores) {
    const row = h("div", "store");
    row.dataset.store = s.store_id;
    const front = h("div", "front");
    const body = h("div", "front-body");
    const sign = h("div", "sign");
    const therm = h("span", "therm", "—");
    sign.append(h("b", "", s.name), h("span", "cc", `${s.store_id} · ${s.country}`), therm);
    const win = h("div", "window");
    const wk = h("span", "w-k", "window display");
    const wv = h("span", "w-v", "waiting for sales");
    const wn = h("span", "w-n", "");
    const wa = h("span", "w-alert", "");
    win.append(wk, wv, wn, wa);
    const tills = h("div", "tills");

    for (const r of s.registers) {
      const btn = h("button", "till");
      btn.type = "button";
      btn.dataset.face = "idle";
      btn.setAttribute("aria-label", `${s.name} ${tillName(r.id)}: switch on or off`);
      btn.append(svgUse("", "till"), h("span", "t-name", tillName(r.id)), h("span", "t-flag", ""));
      btn.addEventListener("click", () => {
        const reg = findRegister(r.id);
        control("register", {
          register_id: r.id, state: reg && reg.state === "open" ? "closed" : "open",
        });
      });
      tills.append(btn);
      refs.tills.set(r.id, { btn, flag: btn.querySelector(".t-flag"), svg: btn.querySelector("svg") });
    }

    body.append(sign, win, tills);
    front.append(h("div", "awning"), body);

    const edge = h("div", "edge");
    edge.dataset.state = "off";
    const head = h("div", "e-head");
    head.append(h("span", "light"), svgUse("mark", "mark"), h("span", "", "Expanso"));
    const jobs = h("div", "e-jobs", "standby");
    const queue = h("div", "e-row queue");
    const qIcon = svgUse("", "disk");
    const qNum = h("span", "num", "0");
    queue.append(qIcon, qNum, h("span", "", "on disk"));
    const bin = h("div", "e-row bin");
    const bIcon = svgUse("", "bin");
    const bNum = h("span", "num", "0");
    bin.append(bIcon, bNum, h("span", "", "kept here"));
    const link = h("div", "e-link", "");
    edge.append(head, jobs, queue, bin, link);

    row.append(front, edge);
    root.append(row);
    refs.stores.set(s.store_id, {
      row, front, therm, win, wk, wv, wn, wa, edge, jobs, qNum, qIcon, queue, bNum, bIcon, link,
    });
  }

  if (!focusStore && stores.length) focusStore = stores[0].store_id;
  markFocus();
}

function findRegister(id) {
  for (const s of current ? current.stores : []) {
    const reg = s.registers.find((r) => r.id === id);

    if (reg) return reg;
  }

  return null;
}

function markFocus() {
  for (const [sid, ref] of refs.stores) ref.row.dataset.focus = sid === focusStore ? "1" : "0";
  $("console-store").textContent = focusStore || "";
}

/* -------------------------------------------------------------- render */

function edgeState(s) {
  const on = Object.values(s.edge.jobs).filter(Boolean).length;

  if (on === 2) return "on";

  return on === 1 ? "half" : "off";
}

function renderHeader(state) {
  const pill = $("pill-mode");
  const cloud = $("cloud");
  const n = state.stores.length;
  const guard = state.stores.filter((s) => s.edge.jobs["pos-guard"]).length;
  const uplink = state.stores.filter((s) => s.edge.jobs["pos-uplink"]).length;
  const connected = state.stores.filter((s) => s.edge.connected).length;

  if (state.mode === "cloud") {
    pill.dataset.state = "cloud";
    pill.textContent = `EXPANSO CLOUD · ${connected}/${n} NODES`;
    cloud.querySelector(".cloud-name").textContent = "Expanso Cloud";
    $("cloud-jobs").textContent = guard || uplink
      ? `pos-guard ${guard}/${n} · pos-uplink ${uplink}/${n} running`
      : "pos-guard · pos-uplink deployed, stopped";
  } else if (state.mode === "local") {
    pill.dataset.state = "on";
    pill.textContent = "LOCAL CONTROL PLANE";
    cloud.querySelector(".cloud-name").textContent = "Local control plane";
    $("cloud-jobs").textContent = `pos-guard ${guard}/${n} · pos-uplink ${uplink}/${n}`;
  } else {
    pill.dataset.state = "off";
    pill.textContent = "EXPANSO OFF";
    $("cloud-jobs").textContent = "no store node connected";
  }

  cloud.dataset.state = connected ? "on" : "off";
}

function renderStore(s, now) {
  const ref = refs.stores.get(s.store_id);

  if (!ref) return;
  const sensor = s.sensor || {};

  if (sensor.on && sensor.temp_c !== null) {
    ref.therm.textContent = `${sensor.temp_c.toFixed(1)}°C`;
    ref.therm.dataset.state = "on";
  } else {
    ref.therm.textContent = "sensor off";
    ref.therm.dataset.state = "off";
  }

  const display = s.display || {};
  const w = display.window;
  const fresh = w && now / 1000 - w.received_at < 25;

  if (fresh && w.by_category && w.by_category.length) {
    const top = w.by_category[0];

    const before = (display.previous && display.previous.by_category || [])
      .find((c) => c.category === top.category);

    ref.wv.textContent = top.category;
    ref.wk.textContent = "top seller now";
    ref.wn.textContent = !before || top.units > before.units ? "↑ rising" : "steady";
  } else {
    ref.wk.textContent = "window display";
    ref.wv.textContent = "waiting for sales";
    ref.wn.textContent = "";
  }

  const silent = Object.values(display.roster || {})
    .filter((r) => r.status === "silent").map((r) => tillName(r.register_id));

  ref.wa.textContent = silent.length ? `${silent.join(", ")} silent` : "";

  const es = edgeState(s);
  ref.edge.dataset.state = es;
  ref.jobs.textContent = es === "off"
    ? "standby"
    : `${s.edge.jobs["pos-guard"] ? "pos-guard" : "—"} · ${s.edge.jobs["pos-uplink"] ? "pos-uplink" : "—"}`;
  ref.qNum.textContent = s.queue === null ? "—" : fmt(s.queue);
  ref.queue.dataset.state = s.queue > 0 && s.link.cut ? "warn" : "ok";
  ref.bNum.textContent = fmt(s.quarantined);
  ref.link.textContent = s.link.cut ? "network down" : "";

  for (const r of s.registers) {
    const t = refs.tills.get(r.id);

    if (!t) continue;
    const roster = (display.roster || {})[r.id];

    if (r.state === "closed") t.flag.textContent = "closed";
    else if (roster && roster.status === "silent") t.flag.textContent = `silent ${roster.silent_s}s`;
    else t.flag.textContent = "";
  }
}

function tillFace(r, now) {
  if (r.state === "closed") return "off";

  if (r.state === "crashed") return "crash";

  if ((alarmUntil.get(r.id) || 0) > now) return "alarm";

  if ((ringUntil.get(r.id) || 0) > now) return "ring";

  return "idle";
}

function renderFaces() {
  if (!current) return;
  const now = performance.now();

  for (const s of current.stores) {
    for (const r of s.registers) {
      const t = refs.tills.get(r.id);

      if (t) t.btn.dataset.face = tillFace(r, now);
    }
  }
}

function renderWarehouse(wh) {
  $("wh-rows").textContent = fmt(wh.rows);
  const scan = wh.card_scan || { card_numbers: 0, rows_scanned: 0 };
  $("wh-cards").textContent = fmt(scan.card_numbers);
  $("wh-zero").dataset.state = scan.card_numbers ? "bad" : "ok";
  $("wh-scanned").textContent = scan.rows_scanned ? "every row scanned" : "nothing landed yet";
  $("wh-multi").textContent = fmt(wh.shoppers_multi_store);
  $("wh-online").textContent = fmt(wh.online_matched);
}

const REASONS = [
  ["signature", "tampered"], ["card number", "card number"], ["malformed", "malformed"],
  ["injection", "injection"], ["unknown register", "unknown till"], ["stale", "replay"],
  ["totals", "totals"], ["range", "range"],
];

function reasonTag(reason) {
  const hit = REASONS.find(([k]) => String(reason).startsWith(k));

  return hit ? hit[1] : "kept";
}

function renderQuarantine(state) {
  const list = $("qlist");
  const total = state.stores.reduce((a, s) => a + (s.quarantined || 0), 0);
  $("q-total").textContent = total ? fmt(total) : "";
  const items = state.quarantine || [];
  const now = performance.now();

  for (const q of items) {
    const key = `${q.txn_id}|${q.quarantined_at}`;

    if (seenQuarantine.has(key)) continue;
    seenQuarantine.add(key);

    if (quarantineLoaded) alarmUntil.set(q.register_id, now + ALARM_MS);
  }

  quarantineLoaded = true;

  if (!items.length) return;
  list.textContent = "";
  const names = Object.fromEntries(state.stores.map((s) => [s.store_id, s.name]));

  for (const q of items.slice(0, 7)) {
    const li = h("li", "");
    const age = (Date.now() - Date.parse(q.quarantined_at)) / 1000;

    if (age < 8) li.classList.add("fresh");
    li.append(h("span", "qtag", reasonTag(q.reason)), h("span", "qwhy", q.reason),
      h("span", "qwhere", `${names[q.store_id] || q.store_id} · ${tillName(q.register_id)}`));
    li.tabIndex = 0;
    li.addEventListener("click", () => showInspect(q.txn_id, true));
    li.addEventListener("keydown", (e) => { if (e.key === "Enter") showInspect(q.txn_id, true); });
    list.append(li);
  }
}

function render(state) {
  const ids = state.stores.map((s) => s.store_id).join(",");

  if (ids && ids !== [...refs.stores.keys()].join(",")) buildStores(state.stores);
  const now = performance.now();
  renderHeader(state);

  for (const s of state.stores) renderStore(s, Date.now());
  renderWarehouse(state.warehouse || {});
  renderQuarantine(state);
  $("auto-btn").setAttribute("aria-pressed", String(Boolean(state.auto)));
  laneCache = new Map();

  if (previous) flows(state, previous, now);
}

/* --------------------------------------------------------- inspector */

function dl(target, rows) {
  target.textContent = "";

  for (const [k, v, cls] of rows) {
    if (v === undefined || v === null || v === "") continue;
    const dt = h("dt", cls === "gone" ? "gone" : "", k);
    const dd = h("dd", cls || "", v);
    target.append(dt, dd);
  }
}

function basketText(items) {
  return (items || []).map((i) => `${i.qty}× ${i.name}`).join(", ");
}

function renderInspect(data) {
  const raw = data.raw;
  const shared = data.shared;
  $("ins-txn").textContent = data.txn_id || "";

  if (!raw && !shared) return;
  const stripped = new Set(shared ? shared.stripped : ["pan", "cardholder", "expiry", "cashier", "loyalty_email"]);
  const gone = (field) => (stripped.has(field) || field === "pan" ? "gone" : "");

  if (raw) {
    dl($("ins-raw"), [
      ["card number", spaced(raw.pan), gone("pan")],
      ["cardholder", raw.cardholder, gone("cardholder")],
      ["expiry", raw.expiry, gone("expiry")],
      ["cashier", raw.cashier, gone("cashier")],
      ["loyalty email", raw.loyalty_email, gone("loyalty_email")],
      ["basket", basketText(raw.items)],
      ["total", money(raw.total_cents, raw.currency)],
      ["note", raw.note],
      ["till", raw.register_id],
      ["swiped", String(raw.ts || "").slice(11, 19) + " UTC"],
      ["signature", `${String(raw.sig || "").slice(0, 16)}…`],
    ]);
  }

  if (shared) {
    const c = shared.context || {};

    const temp = c.temp_c === null || c.temp_c === undefined
      ? ["store temp", c.sensor || "sensor missing", "warn"]
      : ["store temp", `${Number(c.temp_c).toFixed(1)}°C`, "added"];

    const rows = [
      ["join id", shared.join_id, "key"],
      ["basket", basketText(shared.basket)],
      ["total", money(shared.total_cents, shared.currency)],
      ["store", `${c.store_name} · ${c.country}`, "added"],
      ["till", c.register_id, "added"],
      ["local time", `${String(c.local_time || "").slice(11, 16)} ${String(c.weekday || "").slice(0, 3)}`, "added"],
      ["location", `${c.lat}, ${c.lon}`, "added"],
      temp,
      ["stripped", (shared.stripped || []).join(", ")],
    ];

    if (data.queued_s > 3) rows.push(["waited", `${Math.round(data.queued_s)}s on the store's disk`, "warn"]);
    dl($("ins-shared"), rows);
  } else {
    const q = (current && current.quarantine || []).find((x) => x.txn_id === data.txn_id);
    dl($("ins-shared"), [
      ["never left", q ? q.reason : "kept in the store", "gone"],
      ["kept at", q ? `${q.store_id} · ${tillName(q.register_id)}` : ""],
    ]);
  }
}

async function showInspect(txn, pin) {
  if (pin) inspectPinnedTxn = txn;

  try {
    const r = await fetch(`/api/inspect${txn ? `?txn=${encodeURIComponent(txn)}` : ""}`);

    if (r.ok) renderInspect(await r.json());
  } catch {
    /* keep the last pair on screen */
  }
}

const inspector = $("inspector");

inspector.addEventListener("mouseenter", () => { inspectHover = true; });

inspector.addEventListener("mouseleave", () => { inspectHover = false; inspectPinnedTxn = null; });

setInterval(() => {
  if (inspectHover || inspectPinnedTxn) return;
  showInspect(null, false);
}, INSPECT_MS);

/* --------------------------------------------------------------- flow */

function rel(node) {
  const s = stage.getBoundingClientRect();
  const r = node.getBoundingClientRect();

  return { x: r.left - s.left, y: r.top - s.top, w: r.width, h: r.height };
}

function cubic(a, b, bend) {
  const dx = b.x - a.x;

  return [a, { x: a.x + dx * 0.5, y: a.y + (bend || 0) }, { x: b.x - dx * 0.5, y: b.y + (bend || 0) }, b];
}

/* Geometry is measured once per frame (and once per poll) and reused. */
let laneCache = new Map();

function cached(key, fn) {
  if (!laneCache.has(key)) laneCache.set(key, fn());

  return laneCache.get(key);
}

function lanes(sid) {
  return cached(`L${sid}`, () => measureLanes(sid));
}

function measureLanes(sid) {
  const ref = refs.stores.get(sid);

  if (!ref) return null;
  const e = rel(ref.edge);
  const wh = rel($("warehouse"));
  const win = rel(ref.win);
  const order = [...refs.stores.keys()].indexOf(sid);
  const n = refs.stores.size;
  const whY = wh.y + wh.h * ((order + 0.5) / n);
  const q = rel(ref.qIcon);
  const b = rel(ref.bIcon);
  const narrow = wh.y > e.y + e.h;
  const edgeOut = narrow ? { x: e.x + e.w / 2, y: e.y + e.h } : { x: e.x + e.w, y: e.y + e.h / 2 };
  const whIn = narrow ? { x: wh.x + wh.w * ((order + 0.5) / n), y: wh.y } : { x: wh.x, y: whY };

  return {
    edgeIn: { x: e.x, y: e.y + e.h / 2 },
    toWarehouse: narrow
      ? [edgeOut, { x: edgeOut.x, y: edgeOut.y + 60 }, { x: whIn.x, y: whIn.y - 60 }, whIn]
      : cubic(edgeOut, whIn, 0),
    toBin: cubic({ x: e.x, y: e.y + e.h * 0.4 }, { x: b.x + b.w / 2, y: b.y + b.h / 2 }, -18),
    toDisk: cubic({ x: e.x + e.w, y: e.y + e.h * 0.4 }, { x: q.x + q.w / 2, y: q.y + q.h / 2 }, -14),
    toWindow: cubic({ x: e.x, y: e.y + 10 }, { x: win.x + win.w, y: win.y + 8 }, -26),
    edge: e,
  };
}

function tillLane(id, sid) {
  return cached(`T${id}`, () => measureTillLane(id, sid));
}

function measureTillLane(id, sid) {
  const t = refs.tills.get(id);
  const L = lanes(sid);

  if (!t || !L) return null;
  const r = rel(t.svg);
  const start = { x: r.x + r.w / 2, y: r.y + r.h * 0.35 };

  return cubic(start, L.edgeIn, -22);
}

function cloudLane(sid, up) {
  return cached(`C${sid}${up}`, () => measureCloudLane(sid, up));
}

function measureCloudLane(sid, up) {
  const ref = refs.stores.get(sid);

  if (!ref) return null;
  const c = rel($("cloud"));
  const e = rel(ref.edge);
  const top = { x: e.x + e.w - 22, y: e.y };
  const from = { x: Math.min(c.x + c.w - 22, top.x), y: c.y + c.h };
  const pts = [from, { x: from.x, y: from.y + 10 }, { x: top.x, y: top.y - 10 }, top];

  return up ? pts.slice().reverse() : pts;
}

function bez(p, t) {
  const u = 1 - t;

  return {
    x: u * u * u * p[0].x + 3 * u * u * t * p[1].x + 3 * u * t * t * p[2].x + t * t * t * p[3].x,
    y: u * u * u * p[0].y + 3 * u * u * t * p[1].y + 3 * u * t * t * p[2].y + t * t * t * p[3].y,
  };
}

/* One particle per measured event, released across the poll interval so a
 * steady rate looks steady. A drain after a restored link arrives as a burst
 * because that is what the warehouse saw. */
function spawn(count, lane, color, opts) {
  if (!lane || count <= 0) return;
  const o = opts || {};
  const n = Math.min(count, o.cap || 80);
  const window = n > 12 ? 450 : POLL_MS;

  for (let i = 0; i < n; i++) {
    particles.push({
      pts: lane, color, t: 0,
      delay: performance.now() + Math.random() * window,
      dur: (o.dur || 1100) / SPEED,
      r: o.r || 2.4,
      stopAt: o.stopAt || 1,
      soft: o.soft || 0,
    });
  }
}

function flows(cur, old, now) {
  const before = new Map(old.stores.map((s) => [s.store_id, s]));

  for (const s of cur.stores) {
    const o = before.get(s.store_id);

    if (!o) continue;
    const L = lanes(s.store_id);
    const oldRegs = new Map(o.registers.map((r) => [r.id, r]));

    for (const r of s.registers) {
      const or = oldRegs.get(r.id);

      if (!or) continue;
      const sent = r.sent - or.sent;
      const refused = r.refused - or.refused;

      if (sent + refused > 0) ringUntil.set(r.id, now + RING_MS);
      const lane = tillLane(r.id, s.store_id);
      spawn(sent, lane, colors.raw, { dur: 900, soft: 0.15 });
      spawn(refused, lane, colors.raw, { dur: 900, stopAt: 0.9, soft: 0.15 });
    }

    if (!L) continue;
    const landed = s.warehouse_rows - o.warehouse_rows;
    spawn(landed, L.toWarehouse, colors.ok, { dur: landed > 12 ? 900 : 1500, r: 2.6 });
    spawn(s.quarantined - o.quarantined, L.toBin, colors.err, { dur: 700, r: 3.2 });

    if (s.link.cut) spawn((s.queue || 0) - (o.queue || 0), L.toDisk, colors.warn, { dur: 700, r: 2.6 });
    const shown = (s.display.updates || 0) - (o.display.updates || 0);
    spawn(shown, L.toWindow, colors.ok, { dur: 1000, r: 3 });

    if (cur.cloud && old.cloud && cur.cloud.at !== old.cloud.at && s.edge.connected) {
      spawn(1, cloudLane(s.store_id, false), colors.accent, { dur: 1300, r: 2 });
      spawn(1, cloudLane(s.store_id, true), colors.accent, { dur: 1300, r: 2 });
    }
  }
}

function guide(pts, color, alpha, dash) {
  ctx.save();
  ctx.globalAlpha = alpha;
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.2;
  ctx.setLineDash(dash || []);
  ctx.beginPath();
  ctx.moveTo(pts[0].x, pts[0].y);
  ctx.bezierCurveTo(pts[1].x, pts[1].y, pts[2].x, pts[2].y, pts[3].x, pts[3].y);
  ctx.stroke();
  ctx.restore();
}

function drawGuides() {
  if (!current) return;

  for (const s of current.stores) {
    const L = lanes(s.store_id);

    if (!L) continue;
    const on = edgeState(s) !== "off";

    for (const r of s.registers) {
      const lane = tillLane(r.id, s.store_id);

      if (lane) guide(lane, colors.raw, r.state === "open" ? 0.22 : 0.08);
    }

    if (s.link.cut) guide(L.toWarehouse, colors.warn, 0.7, [3, 6]);
    else guide(L.toWarehouse, colors.ok, on ? 0.3 : 0.1);
    guide(L.toWindow, colors.ok, on ? 0.18 : 0.06, [2, 4]);
    const cl = cloudLane(s.store_id, false);

    if (cl) guide(cl, colors.accent, s.edge.connected ? 0.45 : 0.12, [2, 3]);
  }
}

function frame() {
  const w = stage.clientWidth;
  const hgt = stage.clientHeight;
  ctx.clearRect(0, 0, w, hgt);
  laneCache = new Map();
  drawGuides();
  const now = performance.now();

  for (let i = particles.length - 1; i >= 0; i--) {
    const p = particles[i];

    if (now < p.delay) continue;
    p.t = Math.min(1, (now - p.delay) / p.dur);

    if (p.t >= p.stopAt) { particles.splice(i, 1); continue; }

    const pos = bez(p.pts, p.t);
    let alpha = 0.95;

    if (p.t < 0.12) alpha = p.soft ? p.soft + (0.95 - p.soft) * (p.t / 0.12) : alpha;

    if (p.stopAt < 1 && p.t > p.stopAt - 0.25) alpha *= Math.max(0, (p.stopAt - p.t) / 0.25);
    ctx.globalAlpha = alpha;
    ctx.fillStyle = p.color;
    ctx.beginPath();
    ctx.arc(pos.x, pos.y, p.r, 0, Math.PI * 2);
    ctx.fill();
  }

  ctx.globalAlpha = 1;
  requestAnimationFrame(frame);
}

function resize() {
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(stage.clientWidth * dpr);
  canvas.height = Math.round(stage.clientHeight * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
}

new ResizeObserver(resize).observe(stage);

/* --------------------------------------------------------------- poll */

async function poll() {
  try {
    const r = await fetch("/api/state", { cache: "no-store" });

    if (!r.ok) throw new Error(String(r.status));
    const state = await r.json();
    previous = current;
    current = state;
    render(state);
  } catch {
    $("pill-mode").dataset.state = "warn";
    $("pill-mode").textContent = "BOARD OFFLINE";
  }
}

/* ---------------------------------------------------------- presenter */

function openTill(sid, wantState) {
  const s = current && current.stores.find((x) => x.store_id === sid);

  if (!s) return null;
  const pool = s.registers.filter((r) => r.state === wantState);

  return pool.length ? pool[Math.floor(Math.random() * pool.length)] : null;
}

function act(name) {
  if (!current) return;
  const s = current.stores.find((x) => x.store_id === focusStore);

  if (name === "reset") { control("reset", {});

 return; }

  if (name === "auto") { control("auto", { on: !current.auto });

 return; }

  if (!s) return;

  if (["tamper", "leak", "malformed", "inject"].includes(name)) {
    const r = openTill(s.store_id, "open");

    if (r) control("fault", { register_id: r.id, kind: name });
  } else if (name === "crash") {
    const r = openTill(s.store_id, "open");

    if (r) control("fault", { register_id: r.id, kind: "crash" });
  } else if (name === "sensor") {
    control("sensor", { store_id: s.store_id, on: !s.sensor.on });
  } else if (name === "link") {
    control("link", { store_id: s.store_id, cut: !s.link.cut });
  }
}

const KEYS = { t: "tamper", l: "leak", m: "malformed", i: "inject", x: "crash", s: "sensor", n: "link", a: "auto", r: "reset" };

document.addEventListener("keydown", (e) => {
  if (e.metaKey || e.ctrlKey || e.altKey) return;

  if (e.target instanceof HTMLInputElement) return;
  const k = e.key.toLowerCase();

  if (k === "p") { $("console").hidden = !$("console").hidden;

 return; }

  if (k === "d") { setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");

 return; }

  if (/^[1-9]$/.test(k)) {
    const ids = [...refs.stores.keys()];

    if (ids[Number(k) - 1]) { focusStore = ids[Number(k) - 1]; markFocus(); }

    return;
  }

  if (KEYS[k]) act(KEYS[k]);
});

$("console").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-act]");

  if (btn) act(btn.dataset.act);
});

/* --------------------------------------------------------------- start */

try {
  const t = localStorage.getItem("pos-theme");
  setTheme(t === "dark" ? "dark" : "light");
} catch {
  setTheme("light");
}

resize();

poll();

setInterval(poll, POLL_MS);

setInterval(renderFaces, 120);

showInspect(null, false);

requestAnimationFrame(frame);

document.fonts.ready.then(resize);
