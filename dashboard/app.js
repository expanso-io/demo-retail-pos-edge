"use strict";
/* Polls /api/state at 1000ms; falls back to synthetic ticks so the page
   still moves while being designed. Particle density scales with the
   measured rate — motion means throughput, never decoration. */

const POLL_MS = 1000;

const stage = document.getElementById("stage");

const canvas = document.getElementById("flow");

const ctx = canvas.getContext("2d");

const state = { src: 0, edge: 0, rows: 0, nodes: 0, synthetic: true };

function resize() {
  const dpr = window.devicePixelRatio || 1;
  canvas.width = stage.clientWidth * dpr;
  canvas.height = stage.clientHeight * dpr;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
}

window.addEventListener("resize", resize);

resize();

function anchor(sel, side) {
  const el = stage.querySelector(sel);
  const s = stage.getBoundingClientRect();
  const r = el.getBoundingClientRect();

  return { x: (side === "r" ? r.right : r.left) - s.left, y: r.top - s.top + r.height / 2 };
}

// Lane colours are the palette's --accent and --ok, stamped from the tokens
// file so a light demo does not inherit dark-tuned particles.
const LANES = [
  { from: [".n-src", "r"], to: [".n-edge", "l"], color: "#2b63c7", speed: 0.55, rate: () => state.src / 18 },
  { from: [".n-edge", "r"], to: [".n-sink", "l"], color: "#0d8577", speed: 0.9, rate: () => state.edge / 4 },
];

const particles = [];

function draw() {
  ctx.clearRect(0, 0, stage.clientWidth, stage.clientHeight);

  for (const lane of LANES) {
    if (Math.random() < lane.rate() / 60) particles.push({ lane, t: 0 });
  }

  for (let i = particles.length - 1; i >= 0; i--) {
    const p = particles[i];
    p.t += p.lane.speed / 100;

    if (p.t >= 1) { particles.splice(i, 1); continue; }

    const a = anchor(...p.lane.from), b = anchor(...p.lane.to);
    const cx = (a.x + b.x) / 2, cy = Math.min(a.y, b.y) - 40;
    const u = 1 - p.t;
    const x = u * u * a.x + 2 * u * p.t * cx + p.t * p.t * b.x;
    const y = u * u * a.y + 2 * u * p.t * cy + p.t * p.t * b.y;
    ctx.globalAlpha = 0.9 - p.t * 0.4;
    ctx.fillStyle = p.lane.color;
    ctx.beginPath();
    ctx.arc(x, y, 2, 0, Math.PI * 2);
    ctx.fill();
  }

  ctx.globalAlpha = 1;
  requestAnimationFrame(draw);
}

requestAnimationFrame(draw);

const fmt = (n) => Number(n).toLocaleString("en-US");

function render() {
  document.getElementById("src-rate").textContent = fmt(state.src);
  document.getElementById("edge-rate").textContent = fmt(state.edge);
  document.getElementById("sink-rows").textContent = fmt(state.rows);
  document.getElementById("k1").textContent = fmt(state.src);
  document.getElementById("k2").textContent =
    state.src ? (100 - (state.edge / state.src) * 100).toFixed(1) + "%" : "0%";
  document.getElementById("k3").textContent = fmt(Math.max(0, state.src - state.edge));
  document.getElementById("k4").textContent = fmt(state.nodes);
}

async function poll() {
  try {
    const r = await fetch("/api/state");

    if (!r.ok) throw new Error(String(r.status));
    Object.assign(state, await r.json(), { synthetic: false });
  } catch {
    // design-time fallback: synthetic motion, clearly representative
    state.src = 380 + Math.floor(Math.random() * 70);
    state.edge = 14 + Math.floor(Math.random() * 7);
    state.rows += 5 + Math.floor(Math.random() * 12);
    state.nodes = 14;
  }

  render();
}

setInterval(poll, POLL_MS);

poll();

const feed = document.getElementById("feed");

setInterval(() => {
  if (!state.src) return;
  const empty = feed.querySelector(".empty");

  if (empty) empty.remove();
  feed.querySelectorAll(".fresh").forEach((li) => li.classList.remove("fresh"));
  const li = document.createElement("li");
  li.className = "fresh";
  const msg = document.createElement("span");
  msg.textContent = "event normalized \u2192 forwarded";
  const age = document.createElement("span");
  age.className = "age";
  age.textContent = "just now";
  li.append(msg, age);
  feed.prepend(li);

  while (feed.children.length > 6) feed.lastChild.remove();
}, 2600);
