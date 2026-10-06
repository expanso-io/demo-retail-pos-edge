"use strict";
/* Step explorer. Shows the real message each pipeline stage received and
 * produced for one fixture swipe. The messages are recorded by
 * scripts/fixture_run.py from the shipped jobs running on Expanso Edge nodes
 * and loaded from data/stages.json. Nothing is generated in the browser. */

const $ = (id) => document.getElementById(id);

const FLASH_MS = 2600;

let data = null;

let scenarioIndex = 0;

let stageIndex = 0;

/* ------------------------------------------------------------- helpers */

function h(tag, cls, text) {
  const node = document.createElement(tag);

  if (cls) node.className = cls;

  if (text !== undefined) node.textContent = text;

  return node;
}

function pretty(value) {
  return JSON.stringify(value, null, 2);
}

function highlight(target, value) {
  target.textContent = "";

  if (value === null || value === undefined) return;
  const json = pretty(value);
  const tokens = /"(?:\\.|[^"\\])*"\s*:|"(?:\\.|[^"\\])*"|\b(?:true|false|null)\b|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/g;
  let end = 0;

  for (const match of json.matchAll(tokens)) {
    target.append(document.createTextNode(json.slice(end, match.index)));
    const token = match[0];

    const kind = token.endsWith(":") ? "key"
      : token.startsWith('"') ? "string"
        : /^(true|false|null)$/.test(token) ? "literal" : "number";

    target.append(h("span", `json-${kind}`, token));
    end = match.index + token.length;
  }

  target.append(document.createTextNode(json.slice(end)));
}

/* Every path a value contains, with array positions folded into []. */
function paths(value, prefix = "", out = new Set()) {
  if (Array.isArray(value)) {
    for (const item of value) paths(item, `${prefix}[]`, out);

    if (!value.length) out.add(prefix);
  } else if (value?.constructor === Object) {
    for (const [key, item] of Object.entries(value)) paths(item, prefix ? `${prefix}.${key}` : key, out);
  } else {
    out.add(prefix);
  }

  return out;
}

function changes(before, after) {
  const was = paths(before);
  const now = paths(after);
  const added = [...now].filter((p) => !was.has(p));
  const removed = [...was].filter((p) => !now.has(p));

  return { added, removed };
}

function listPaths(items) {
  const shown = items.slice(0, 10).join(", ");

  return items.length > 10 ? `${shown} and ${items.length - 10} more` : shown;
}

/* ------------------------------------------------------------ feedback */

/* Every copy and download says what happened, next to the button that was
 * pressed, and keeps saying it for a few seconds. */
function say(box, state, text) {
  box.dataset.state = state;
  box.textContent = text;
  clearTimeout(box.timer);
  box.timer = setTimeout(() => {
    box.textContent = "";
    box.dataset.state = "";
  }, FLASH_MS);
}

function legacyCopy(text) {
  const area = h("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  area.style.position = "fixed";
  area.style.opacity = "0";
  document.body.append(area);
  area.select();
  let done = false;

  try {
    done = document.execCommand("copy");
  } catch {
    done = false;
  }

  area.remove();

  return done;
}

async function copyText(text, box, what) {
  let done = false;

  if (navigator.clipboard) {
    try {
      await navigator.clipboard.writeText(text);
      done = true;
    } catch {
      done = false;
    }
  } else {
    done = legacyCopy(text);
  }

  if (done) say(box, "ok", `Copied ${what}`);
  else say(box, "fail", "Copy failed. Select the text and copy it yourself.");
}

function saveBlob(name, blob) {
  const url = URL.createObjectURL(blob);
  const link = h("a");
  link.href = url;
  link.download = name;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function download(name, value, box) {
  try {
    saveBlob(name, new Blob([`${pretty(value)}\n`], { type: "application/json" }));
    say(box, "ok", `Saved ${name}`);
  } catch {
    say(box, "fail", "Download failed. Use Copy instead.");
  }
}

/* The whole recorded run, fetched again so a missing file is reported here. */
async function downloadRun(box) {
  try {
    const response = await fetch("data/stages.json", { cache: "no-store" });

    if (!response.ok) throw new Error(String(response.status));
    saveBlob("pos-edge-recorded-run.json", await response.blob());
    say(box, "ok", "Saved pos-edge-recorded-run.json");
  } catch {
    say(box, "fail", "Download failed. The recorded run could not be fetched.");
  }
}

/* --------------------------------------------------------------- theme */

function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  $("theme").textContent = theme === "dark" ? "Light" : "Dark";
  $("theme").setAttribute("aria-pressed", String(theme === "dark"));

  try {
    localStorage.setItem("pos-theme", theme);
  } catch {
    /* storage blocked: the toggle still works for this visit */
  }
}

$("theme").addEventListener("click", () =>
  setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark"));

/* ------------------------------------------------------------ explorer */

function currentScenario() {
  return data.scenarios[scenarioIndex];
}

/* The address records the swipe and stage. replaceState does not scroll. */
function remember() {
  const hash = `#swipe=${currentScenario().id}&stage=${stageIndex + 1}`;

  try {
    history.replaceState(null, "", hash);
  } catch {
    /* some embedded browsers refuse: paging still works */
  }
}

function buildScenarios() {
  const root = $("scenarios");
  root.textContent = "";

  const groups = [
    ["clean", "Sent to the warehouse"],
    ["quarantined", "Kept in the store"],
  ];

  for (const [outcome, title] of groups) {
    const members = data.scenarios.map((s, i) => [s, i]).filter(([s]) => s.outcome === outcome);
    const group = h("div", "group");
    group.append(h("div", "group-title", `${title} (${members.length})`));
    const chips = h("div", "chips");

    for (const [scenario, index] of members) {
      const chip = h("button", "chip", scenario.title);
      chip.type = "button";
      chip.dataset.index = String(index);
      chip.setAttribute("aria-pressed", "false");
      chip.addEventListener("click", () => selectScenario(index));
      chips.append(chip);
    }

    group.append(chips);
    root.append(group);
  }
}

function wireStages() {
  data.stage_defs.forEach((def, i) => {
    $(`tab-${i}`).addEventListener("click", () => selectStage(i));
  });
}

function pane(label, value) {
  const box = h("div", "pane");
  box.append(h("h4", "", label));

  if (value === null || value === undefined) {
    box.append(h("p", "empty", "Nothing here: this stage did not run for this swipe."));

    return box;
  }

  const code = h("pre", "json");
  code.dataset.publicJson = "";
  code.tabIndex = 0;
  code.setAttribute("aria-label", `${label} JSON`);
  highlight(code, value);
  box.append(code);

  return box;
}

function buildPanels() {
  const scenario = currentScenario();
  const root = $("panels");
  root.textContent = "";

  data.stage_defs.forEach((def, i) => {
    const stage = scenario.stages[i];
    const panel = h("section", "panel");
    panel.id = `panel-${i}`;
    panel.setAttribute("role", "tabpanel");
    panel.setAttribute("aria-labelledby", `tab-${i}`);
    panel.dataset.active = "false";
    panel.append(h("h3", "", `${i + 1}. ${def.title}`));
    panel.append(h("p", "", def.what));
    panel.append(h("p", "where", `Job step: ${def.resource} in ${def.file}, line ${def.line}.`));

    if (stage.skipped) panel.append(h("p", "notice", `Not reached: ${stage.skipped}.`));

    const io = h("div", "io");
    io.append(pane("Input", stage.input), pane("Output", stage.output));
    const diff = changes(stage.input, stage.output);

    if (stage.input && stage.output && (diff.added.length || diff.removed.length)) {
      const summary = h("p", "changes");

      if (diff.added.length) {
        summary.append(h("span", "added", `Added: ${listPaths(diff.added)}. `));
      }

      if (diff.removed.length) {
        summary.append(h("span", "removed", `Removed: ${listPaths(diff.removed)}.`));
      }

      panel.append(summary);
    }

    panel.append(io);
    root.append(panel);
  });
}

function showStage() {
  const scenario = currentScenario();

  data.stage_defs.forEach((def, i) => {
    const active = i === stageIndex;
    const tab = $(`tab-${i}`);
    const panel = $(`panel-${i}`);
    tab.setAttribute("aria-selected", String(active));

    if (active) tab.setAttribute("aria-current", "step");
    else tab.removeAttribute("aria-current");

    tab.tabIndex = active ? 0 : -1;
    const stage = scenario.stages[i];
    tab.dataset.state = stage.skipped ? "skipped" : "ran";
    tab.querySelector(".state").textContent = stage.skipped ? "not reached" : "ran";
    panel.dataset.active = String(active);
    panel.inert = !active;
  });

  $("prev").disabled = stageIndex === 0;
  $("next").disabled = stageIndex === data.stage_defs.length - 1;
  const now = scenario.stages[stageIndex];
  $("copy-input").disabled = !now.input;
  $("copy-output").disabled = !now.output;
}

/* The toolbar acts on the stage on screen. */
function stageFile(which) {
  return `${currentScenario().id}-stage-${stageIndex + 1}-${which}.json`;
}

$("copy-input").addEventListener("click", () =>
  copyText(pretty(currentScenario().stages[stageIndex].input), $("stage-feedback"), "the input"));

$("copy-output").addEventListener("click", () =>
  copyText(pretty(currentScenario().stages[stageIndex].output), $("stage-feedback"), "the output"));

$("download-stage").addEventListener("click", () =>
  download(stageFile("messages"), {
    stage: data.stage_defs[stageIndex].title,
    input: currentScenario().stages[stageIndex].input,
    output: currentScenario().stages[stageIndex].output,
  }, $("stage-feedback")));

$("download-run").addEventListener("click", () => downloadRun($("stage-feedback")));

/* Paging keeps the reader exactly where they are: no scrolling, no focus
 * jump, and the section does not change height. */
function selectStage(index, focusTab) {
  const top = window.scrollY;
  stageIndex = Math.max(0, Math.min(data.stage_defs.length - 1, index));
  showStage();
  remember();

  if (focusTab) $(`tab-${stageIndex}`).focus({ preventScroll: true });

  if (window.scrollY !== top) window.scrollTo(0, top);
}

function selectScenario(index) {
  const top = window.scrollY;
  scenarioIndex = index;

  for (const chip of document.querySelectorAll(".chip")) {
    chip.setAttribute("aria-pressed", String(Number(chip.dataset.index) === index));
  }

  buildPanels();
  showStage();
  remember();

  if (window.scrollY !== top) window.scrollTo(0, top);
}

document.addEventListener("keydown", (e) => {
  if (!data || e.metaKey || e.ctrlKey || e.altKey || e.shiftKey) return;
  const target = e.target;

  if (target instanceof HTMLElement && target.closest("input, textarea, select, [contenteditable]")) return;

  const moves = { ArrowLeft: -1, ArrowRight: 1 };

  if (moves[e.key]) {
    e.preventDefault();
    selectStage(stageIndex + moves[e.key], document.activeElement?.getAttribute("role") === "tab");
  } else if (e.key === "Home" || e.key === "End") {
    if (!(target instanceof HTMLElement) || !target.closest("#stages")) return;
    e.preventDefault();
    selectStage(e.key === "Home" ? 0 : data.stage_defs.length - 1, true);
  }
});

$("prev").addEventListener("click", () => selectStage(stageIndex - 1));

$("next").addEventListener("click", () => selectStage(stageIndex + 1));

/* ------------------------------------------------------- command blocks */

for (const button of document.querySelectorAll("button[data-copy]")) {
  button.addEventListener("click", () => {
    const block = button.closest(".cmd");
    copyText(block.querySelector("code").textContent, block.querySelector(".feedback"), "the commands");
  });
}


/* ---------------------------------------------------------------- start */

function fromAddress() {
  const found = new URLSearchParams(location.hash.slice(1));
  const index = data.scenarios.findIndex((s) => s.id === found.get("swipe"));
  scenarioIndex = index >= 0 ? index : 0;
  const stage = Number(found.get("stage"));
  stageIndex = Number.isInteger(stage) && stage >= 1 && stage <= data.stage_defs.length ? stage - 1 : 0;
}

function failed(message) {
  $("proof-line").textContent = message;
  $("scenarios").textContent = "";
  $("panels").replaceChildren(h("p", "notice", `${message} Run just proof to record it, then reload this page.`));
}

async function start() {
  try {
    const t = localStorage.getItem("pos-theme");
    setTheme(t === "dark" ? "dark" : "light");
  } catch {
    setTheme("light");
  }

  try {
    const response = await fetch("data/stages.json", { cache: "no-store" });

    if (!response.ok) throw new Error(String(response.status));
    data = await response.json();
  } catch {
    failed("The recorded run could not be loaded.");

    return;
  }

  const where = data.revision?.commit ? data.revision.short : "unknown";

  const when = data.generated ? new Date(data.generated).toLocaleString() : "an unknown time";

  $("proof-line").textContent = `Real messages from the fixture run on ${when}, revision ${where}, `
    + `${data.checks} checks, ${data.passed ? "all passing" : "not all passing"}. Each stage shows what the `
    + "shipped job received and produced.";
  fromAddress();
  buildScenarios();
  wireStages();
  selectScenario(scenarioIndex);
  selectStage(stageIndex);
}

start();
