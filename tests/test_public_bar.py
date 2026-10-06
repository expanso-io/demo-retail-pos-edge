#!/usr/bin/env -S uv run -s
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Regression assertions for the public bar that need no browser or edge node.

* nothing the board or pipelines had is dropped without a recorded decision
  (docs/feature-history.json, public-features.json)
* the proof report and the explorer data describe the tree as it is now
* every palette pair the pages use clears WCAG AA in light and dark
* every environment variable a job reads is documented on the explorer page
* the explorer has its three sections and pages stages with the arrow keys
  without moving the page
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import fixtures  # noqa: E402


def read(name: str) -> str:
    return (ROOT / name).read_text()


def test_every_feature_is_retained_or_decided() -> None:
    manifest = json.loads(read("docs/feature-history.json"))
    seen = set()
    for feature in manifest["features"]:
        assert feature["id"] not in seen, feature["id"]
        seen.add(feature["id"])
        assert feature["status"] in {"retained", "restored", "replaced", "needs_decision"}, feature
        if feature["status"] in {"restored", "replaced", "needs_decision"}:
            assert feature.get("removed_by"), f"{feature['id']}: name the commit that removed it"
        if feature["status"] in {"replaced", "needs_decision"}:
            assert feature.get("note"), f"{feature['id']}: say what replaced it or why it waits"
        check = feature.get("check")
        if feature["status"] in {"retained", "restored"}:
            assert check, f"{feature['id']}: a retained feature needs a check"
        if check:
            assert check["contains"] in read(check["file"]), \
                f"{feature['id']}: {check['file']} no longer contains {check['contains']!r}"


def test_proof_matches_the_tree() -> None:
    latest = json.loads(read("docs/proof/latest.json"))
    now = fixtures.inputs_digest()
    assert latest["inputs_sha256"] == now["sha256"], (
        "the jobs, fixtures or the code they exercise changed since the last proof run: "
        "run `just proof` and commit docs/proof and dashboard/data")
    assert latest["passed"], "the recorded proof run did not pass"
    report = ROOT / "docs" / "proof" / latest["report"]
    assert report.is_file() and "**Result: PASS**" in report.read_text()
    assert latest["report"].startswith(latest["date"]), "the report's name carries its date"
    stages = json.loads(read("dashboard/data/stages.json"))
    assert stages["inputs_sha256"] == now["sha256"], "explorer data is from another revision"
    for stage in stages["stage_defs"]:
        lines = read(stage["file"]).splitlines()
        assert lines[stage["line"] - 1].strip().endswith(f"label: {stage['resource']}"), stage
    assert len(stages["scenarios"]) == len(json.loads(read("fixtures/swipes.json"))["cases"])
    for scenario in stages["scenarios"]:
        assert len(scenario["stages"]) == len(stages["stage_defs"]) == 6
        assert scenario["stages"][0]["output"] and scenario["stages"][4]["output"], scenario["id"]


def channel(hex_value: str) -> float:
    v = int(hex_value, 16) / 255
    return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4


def luminance(color: str) -> float:
    c = color.lstrip("#")
    return 0.2126 * channel(c[0:2]) + 0.7152 * channel(c[2:4]) + 0.0722 * channel(c[4:6])


def ratio(a: str, b: str) -> float:
    hi, lo = sorted([luminance(a), luminance(b)], reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def tokens(block: str) -> dict[str, str]:
    return dict(re.findall(r"--([a-z0-9-]+):\s*(#[0-9a-fA-F]{6})", block))


def test_palette_pairs_clear_aa() -> None:
    css = read("dashboard/tokens.css")
    light_block = css.split(':root[data-theme="dark"]')[0]
    dark_block = css.split(':root[data-theme="dark"]')[1]
    light = tokens(light_block)
    dark = {**light, **tokens(dark_block)}
    text_on = ["bg", "surface", "surface-2", "wall", "store-events", "accent-soft", "raw-soft",
               "ok-soft", "warn-soft", "err-soft"]
    inks = {"text": text_on, "text-dim": ["bg", "surface", "surface-2"],
            "accent": ["bg", "surface", "surface-2", "accent-soft"],
            "raw": ["bg", "surface", "surface-2", "raw-soft", "store-events", "wall"],
            "ok": ["bg", "surface", "surface-2", "ok-soft"],
            "warn": ["bg", "surface", "surface-2", "warn-soft", "wall"],
            "err": ["bg", "surface", "surface-2", "err-soft", "wall"],
            "window-ink": ["window"], "window-dim": ["window"]}
    for name, theme in (("light", light), ("dark", dark)):
        for ink, grounds in inks.items():
            for ground in grounds:
                got = ratio(theme[ink], theme[ground])
                assert got >= 4.5, f"{name}: {ink} on {ground} is {got:.2f}:1"


def test_every_job_variable_is_documented_on_the_explorer() -> None:
    page = read("dashboard/explorer.html")
    names = set()
    for job in ("pos-guard", "pos-uplink"):
        text = read(f"pipelines/{job}.yaml")
        names |= set(re.findall(r"\$\{([A-Z_]+)[:}]", text))
        names |= set(re.findall(r'env\("([A-Z_]+)"\)', text))
    assert names, "no variables found"
    missing = sorted(n for n in names if f"<code>{n}</code>" not in page)
    assert not missing, f"not documented on explorer.html: {missing}"


def test_explorer_structure_and_paging() -> None:
    page, script = read("dashboard/explorer.html"), read("dashboard/explorer.js")
    order = [page.index(f'id="{i}"') for i in ("explanation", "explorer", "run", "deploy")]
    assert order == sorted(order), "explanation, explorer, then run and deploy"
    stages = json.loads(read("dashboard/data/stages.json"))["stage_defs"]
    for i, stage in enumerate(stages):
        assert f'data-stage-id="{stage["id"]}"' in page, stage["id"]
        assert f"<b>{i + 1}. {stage['title']}</b>" in page, f"tab title for {stage['id']}"
    assert 'aria-current' in script_text()
    assert "ArrowLeft" in script and "ArrowRight" in script
    assert "preventScroll" in script and "replaceState" in script
    assert "scrollIntoView" not in script, "paging must not scroll the page"
    assert script.count("say(") >= 6, "copy and download report success and failure"
    assert "Copy failed" in script and "Download failed" in script


def script_text() -> str:
    return read("dashboard/explorer.js")


def test_kit_manifests_agree_with_the_pages() -> None:
    import tomllib
    manifest = tomllib.loads(read("public-bar.toml"))
    features = json.loads(read("public-features.json"))
    page = read("dashboard/explorer.html")
    for control in manifest["browser"]["controls"]:
        assert page.count(f'id="{control["selector"].lstrip("#")}"') == 1, control["id"]
        assert page.count(f'id="{control["feedback_selector"].lstrip("#")}"') == 1, control["id"]
    for stage in manifest["stages"]:
        for side in ("input", "output"):
            lines = read(stage[side]).splitlines()
            assert lines and all(json.loads(line)["message"] is not None for line in lines), stage["id"]
    ids = {f["id"] for f in features["features"]}
    assert {s["id"] for s in manifest["stages"]} <= ids
    for name in ("pos-guard", "pos-uplink"):
        assert any(p["path"] == f"pipelines/{name}.yaml" for p in manifest["pipelines"])


def test_pages_are_light_first_with_a_dark_toggle() -> None:
    assert "color-scheme: light;" in read("dashboard/tokens.css").split('[data-theme="dark"]')[0]
    for name in ("dashboard/index.html", "dashboard/explorer.html"):
        text = read(name)
        assert 'id="theme"' in text, f"{name}: no explicit theme toggle"
        assert 'dataset.theme = "dark"' not in text, f"{name}: must not default to dark"


def test_no_banned_decoration() -> None:
    for name in ("dashboard/styles.css", "dashboard/explorer.css", "dashboard/tokens.css"):
        css = read(name)
        assert not re.search(r"border-left:\s*\d+px\s+solid", css), f"{name}: side stripe"
    for name in ("dashboard/explorer.css",):
        css = read(name)
        assert "uppercase" not in css and "letter-spacing" not in css, "no eyebrow labels"
        assert "gradient" not in css


def main() -> int:
    tests = [v for k, v in globals().items() if k.startswith("test_")]
    for t in tests:
        t()
    print(f"ok: {len(tests)} public-bar checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
