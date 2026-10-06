#!/usr/bin/env -S uv run -s
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Rendered usability audit: page width and text contrast, in a real browser.

For every page, theme and viewport it measures what the browser actually
drew, not what the stylesheet says:

* horizontal overflow: the document must not scroll sideways at 320, 400,
  768 and 1440 px, in light and in dark
* contrast: every visible text node, link and button label must reach
  WCAG AA (4.5:1, or 3:1 for large text) against the background that is
  really behind it, with colour-mix and opacity resolved

It drives `agent-browser` with its own named session and closes only that
session. The pages must already be served; this script starts nothing.

    uv run -s scripts/ui_audit.py http://localhost:8640/ \\
        http://localhost:8640/explorer.html
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.request

WIDTHS = (320, 400, 768, 1440)
THEMES = ("light", "dark")
THEME_KEY = "pos-theme"

# Evaluated inside the page. Returns plain JSON.
MEASURE = r"""
(() => {
  const clamp = (n) => Math.max(0, Math.min(255, n));
  const parse = (value) => {
    let m = value.match(/^rgba?\(([^)]+)\)/);
    if (m) {
      const p = m[1].split(/[ ,\/]+/).filter(Boolean).map(Number);
      return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 };
    }
    m = value.match(/^color\(srgb ([^)]+)\)/);
    if (m) {
      const p = m[1].split(/[ \/]+/).filter(Boolean).map(Number);
      return { r: clamp(p[0] * 255), g: clamp(p[1] * 255), b: clamp(p[2] * 255), a: p.length > 3 ? p[3] : 1 };
    }
    return null;
  };
  const over = (top, under) => {
    const a = top.a + under.a * (1 - top.a);
    if (a === 0) return { r: 0, g: 0, b: 0, a: 0 };
    const mix = (t, u) => (t * top.a + u * under.a * (1 - top.a)) / a;
    return { r: mix(top.r, under.r), g: mix(top.g, under.g), b: mix(top.b, under.b), a };
  };
  const lum = ({ r, g, b }) => {
    const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
  };
  const ratio = (a, b) => {
    const [hi, lo] = [lum(a), lum(b)].sort((x, y) => y - x);
    return (hi + 0.05) / (lo + 0.05);
  };
  const name = (el) => {
    const id = el.id ? "#" + el.id : "";
    const cls = typeof el.className === "string" && el.className.trim()
      ? "." + el.className.trim().split(/\s+/).slice(0, 2).join(".") : "";
    return el.tagName.toLowerCase() + id + cls;
  };
  const backdrop = (el) => {
    const layers = [];
    let unknown = false;
    for (let n = el; n; n = n.parentElement) {
      const cs = getComputedStyle(n);
      if (cs.backgroundImage !== "none") unknown = true;
      const bg = parse(cs.backgroundColor);
      if (bg && bg.a > 0) {
        layers.push(bg);
        if (bg.a >= 0.999) break;
      }
    }
    let base = { r: 255, g: 255, b: 255, a: 1 };
    for (let i = layers.length - 1; i >= 0; i--) base = over(layers[i], base);
    return { color: base, unknown };
  };
  const fades = (el) => {
    let o = 1;
    for (let n = el; n; n = n.parentElement) o *= Number(getComputedStyle(n).opacity);
    return o;
  };
  const fails = [];
  const unknown = [];
  let checked = 0;
  for (const el of document.body.querySelectorAll("*")) {
    if (["SCRIPT", "STYLE", "CANVAS", "SVG", "svg", "PATH"].includes(el.tagName)) continue;
    const own = [...el.childNodes].some((c) => c.nodeType === 3 && c.textContent.trim());
    if (!own) continue;
    const rect = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    if (rect.width < 1 || rect.height < 1 || cs.visibility === "hidden" || cs.display === "none") continue;
    if (rect.right < 0 || rect.bottom < 0 || rect.left > document.documentElement.scrollWidth) continue;
    if (el.closest("[hidden], [inert]") || el.disabled) continue;
    const fg = parse(cs.color);
    if (!fg) continue;
    const { color: bg, unknown: busy } = backdrop(el);
    const opacity = fades(el);
    const paint = over({ ...fg, a: fg.a * opacity }, bg);
    const r = ratio(paint, bg);
    const px = parseFloat(cs.fontSize);
    const large = px >= 24 || (px >= 18.66 && Number(cs.fontWeight) >= 700);
    const need = large ? 3 : 4.5;
    checked += 1;
    const text = el.textContent.trim().replace(/\s+/g, " ").slice(0, 40);
    if (busy) { unknown.push({ el: name(el), text }); continue; }
    if (r < need) fails.push({ el: name(el), text, ratio: Math.round(r * 100) / 100, need, size: px });
  }
  const width = window.innerWidth;
  const wide = [];
  for (const el of document.body.querySelectorAll("*")) {
    const rect = el.getBoundingClientRect();
    if (rect.width > 0 && Math.round(rect.right) > width && getComputedStyle(el).position !== "fixed") {
      let clipped = false;
      for (let n = el.parentElement; n && n !== document.documentElement; n = n.parentElement) {
        const ox = getComputedStyle(n).overflowX;
        if (ox === "hidden" || ox === "auto" || ox === "scroll" || ox === "clip") { clipped = true; break; }
      }
      if (!clipped) wide.push({ el: name(el), right: Math.round(rect.right) });
    }
  }
  return {
    width,
    scrollWidth: document.documentElement.scrollWidth,
    bodyScrollWidth: document.body.scrollWidth,
    theme: document.documentElement.dataset.theme || "light",
    checked,
    contrast_failures: fails.slice(0, 40),
    contrast_unknown: unknown.length,
    overflowing: wide.slice(0, 12),
  };
})()
"""


def browser(session: str, *args: str) -> str:
    done = subprocess.run(["agent-browser", "--session", session, *args],
                          capture_output=True, text=True, timeout=60)
    if done.returncode:
        raise RuntimeError(f"agent-browser {args[0]} failed: {done.stderr.strip()[:200]}")
    return done.stdout.strip()


# Every colour the board uses for a fault, so the audit sees them all.
FAULTS = [
    ("fault", {"register_id": "s1-r1", "kind": "tamper"}),
    ("fault", {"register_id": "s2-r1", "kind": "leak"}),
    ("fault", {"register_id": "s3-r2", "kind": "malformed"}),
    ("fault", {"register_id": "s4-r2", "kind": "inject"}),
    ("fault", {"register_id": "s1-r3", "kind": "crash"}),
    ("link", {"store_id": "s3", "cut": True}),
    ("sensor", {"store_id": "s4", "on": False}),
    ("register", {"register_id": "s2-r2", "state": "closed"}),
]


def control(base: str, verb: str, body: dict) -> None:
    request = urllib.request.Request(f"{base}/api/control/{verb}", method="POST",
                                     data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
    urllib.request.urlopen(request, timeout=5).read()


def audit(session: str, url: str, settle_ms: int, state: str) -> list[dict]:
    """`state` is "idle" (as served) or "faults" (every fault injected and the
    record dialog open), so every colour the board can show is measured."""
    rows = []
    base = url.rsplit("/", 1)[0] if url.count("/") > 2 else url
    if state == "faults":
        for verb, body in FAULTS:
            control(base, verb, body)
        time.sleep(14)  # the faults travel the real pipeline and show up on the board
    browser(session, "open", url)
    try:
        for theme in THEMES:
            for width in WIDTHS:
                browser(session, "set", "viewport", str(width), "900")
                browser(session, "eval", f"localStorage.setItem('{THEME_KEY}', '{theme}')")
                browser(session, "reload")
                browser(session, "wait", str(settle_ms))
                if state == "faults":
                    browser(session, "eval", "(() => { const b = document.querySelector("
                            "'.feed-record'); document.getElementById('console').hidden = false; "
                            "if (b) b.click(); return !!b; })()")
                    browser(session, "wait", "400")
                result = json.loads(browser(session, "eval", MEASURE))
                result.update(url=url, requested_theme=theme, state=state)
                rows.append(result)
    finally:
        if state == "faults":
            control(base, "reset", {})
    return rows


PAGING = r"""
(() => {
  const stage = () => Number(document.querySelector('.step[aria-selected="true"]').id.split('-')[1]) + 1;
  const hash = () => location.hash;
  window.scrollTo(0, 1200);
  return { y: window.scrollY, stage: stage(), hash: hash() };
})()
"""

STATE = "(() => ({ y: window.scrollY, stage: Number(document.querySelector('.step[aria-selected=\"true\"]').id.split('-')[1]) + 1, hash: location.hash, doc: document.documentElement.scrollHeight }))()"

FEEDBACK = r"""
(() => new Promise((resolve) => {
  const copy = document.getElementById('copy-output');
  const note = document.getElementById('stage-feedback');
  copy.click();
  setTimeout(() => resolve({ text: note.textContent, state: note.dataset.state }), 600);
}))()
"""


def paging(session: str, url: str, settle_ms: int) -> list[str]:
    """Left and Right page the stages without moving the page; copying says so."""
    problems = []
    browser(session, "open", url + "#swipe=clean-hot-drinks&stage=3")
    for width in (320, 1440):
        browser(session, "set", "viewport", str(width), "700")
        browser(session, "reload")
        browser(session, "wait", str(settle_ms))
        start = json.loads(browser(session, "eval", PAGING))
        for key, want in (("ArrowRight", 1), ("ArrowRight", 1), ("ArrowLeft", -1), ("ArrowLeft", -1)):
            before = json.loads(browser(session, "eval", STATE))
            browser(session, "press", key)
            after = json.loads(browser(session, "eval", STATE))
            if after["stage"] != before["stage"] + want:
                problems.append(f"{width}px: {key} moved stage {before['stage']} to {after['stage']}")
            if abs(after["y"] - before["y"]) > 1:
                problems.append(f"{width}px: {key} scrolled the page from {before['y']} to {after['y']}")
            if f"stage={after['stage']}" not in after["hash"]:
                problems.append(f"{width}px: the address did not follow the stage")
        if start["y"] < 100:
            problems.append(f"{width}px: page too short to test scroll retention")
        note = json.loads(browser(session, "eval", FEEDBACK))
        if note["state"] not in ("ok", "fail") or not note["text"]:
            problems.append(f"{width}px: Copy showed no success or failure message")
    return problems


def verdict(row: dict) -> list[str]:
    problems = []
    if row["scrollWidth"] > row["width"] or row["bodyScrollWidth"] > row["width"]:
        problems.append(f"sideways scroll: document {row['scrollWidth']} px wide in a "
                        f"{row['width']} px window")
    if row["theme"] != row["requested_theme"]:
        problems.append(f"theme is {row['theme']}, wanted {row['requested_theme']}")
    for fail in row["contrast_failures"]:
        problems.append(f"contrast {fail['ratio']}:1 (needs {fail['need']}) "
                        f"{fail['el']} \"{fail['text']}\"")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("urls", nargs="+")
    ap.add_argument("--session", default="pos-edge-ui-audit")
    ap.add_argument("--settle-ms", type=int, default=2500)
    ap.add_argument("--json", help="write every measurement to this file")
    ap.add_argument("--paging", help="explorer URL: also test arrow-key paging and copy feedback")
    ap.add_argument("--faults", action="store_true",
                    help="also audit the board with every fault injected (needs the demo up)")
    args = ap.parse_args()
    everything: list[dict] = []
    failed = 0
    try:
        runs = [(url, "idle") for url in args.urls]
        if args.faults:
            runs.append((args.urls[0], "faults"))
        for url, state in runs:
            for row in audit(args.session, url, args.settle_ms, state):
                everything.append(row)
                problems = verdict(row)
                tag = "FAIL" if problems else "ok  "
                print(f"{tag} {url} {state:6s} {row['theme']:5s} {row['width']:4d}px  "
                      f"scroll {row['scrollWidth']:4d}  text nodes {row['checked']:4d}  "
                      f"unresolved backdrops {row['contrast_unknown']}")
                for problem in problems:
                    print(f"       {problem}")
                failed += bool(problems)
        if args.paging:
            problems = paging(args.session, args.paging, args.settle_ms)
            print(f"{'FAIL' if problems else 'ok  '} {args.paging} arrow-key paging keeps the "
                  "scroll position, copy reports its result")
            for problem in problems:
                print(f"       {problem}")
            failed += bool(problems)
            everything.append({"paging": problems})
    finally:
        subprocess.run(["agent-browser", "--session", args.session, "close"],
                       capture_output=True, text=True)
    if args.json:
        with open(args.json, "w") as handle:
            json.dump(everything, handle, indent=2)
    print(f"{len(everything) - failed} of {len(everything)} checks pass")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
