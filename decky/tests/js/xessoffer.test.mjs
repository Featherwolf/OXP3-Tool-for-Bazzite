// Tests for src/xessoffer.ts. Run from decky/: node --experimental-strip-types --test tests/js/*.test.mjs
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

import {
  checkText, installLines, installTitle, jobNext, offerLabel, pieceText, REBOOT_AFTER_FAILED_CHECK, repairText, tagNewer, xessOffer,
} from "../../src/xessoffer.ts";

const TAGS = ["20261031", "20261104", "20261104-2", "20261104-10", "20261102-2", "20251231", "v1.3", "v1.3.0",
  "v1.3.2", "v1.10", "v2", "custom", "2026-11-04"];

test("tagNewer orders tags as xess.newer does", () => {
  // the backend's own answer for every pair (None: not comparable, then the tags only differ)
  const py = fileURLToPath(new URL("../../py_modules", import.meta.url));
  const out = execFileSync("python3", ["-I", "-c", [
    "import json, sys", `sys.path.insert(0, ${JSON.stringify(py)})`, "from oxp3deck import xess",
    "tags = json.loads(sys.argv[1])",
    "print(json.dumps([[a, b, xess.newer(a, b)] for a in tags for b in tags]))",
  ].join("\n"), JSON.stringify(TAGS)], { encoding: "utf8" });
  for (const [a, b, newer] of JSON.parse(out)) {
    assert.equal(tagNewer(a, b), newer === null ? a !== b : newer, `${a} newer than ${b}`);
  }
});

test("tagNewer: the cases the buttons meet", () => {
  assert.equal(tagNewer("20261104", "20261031"), true);
  assert.equal(tagNewer("20261031", "20261104"), false);     // a stale stored tag is no update (FE-1)
  assert.equal(tagNewer("20261104", "20261104"), false);
  assert.equal(tagNewer("20261104-2", "20261104"), true);
  assert.equal(tagNewer("20261104", "20261104-1"), false);   // -1 is the plain tag
  assert.equal(tagNewer("20261102-2", "20261104"), false);
  assert.equal(tagNewer("20261031", "v1.3.2"), true);
  assert.equal(tagNewer("20261104", null), true);            // version unknown: check_update offers it
  assert.equal(tagNewer(null, "20261104"), false);
  assert.equal(tagNewer("", "20261104"), false);
});

const state = (over = {}) => ({
  installed: true, package_dir: "/home/jordan/Documents/xess-xmx-linux-20261031", version: "20261031",
  found_by: "env", managed: false, package_dir_kept: null, drivers: [], preferred_driver: null,
  status: { state: "ok", line: null, driver: null, notes: [], date: null, lib: null, driver_info: null, reason: null, switched_to: null },
  live: true, steam_running: false, relogin_needed: false, units: { check: "enabled", watcher: "active" },
  system_protons: [], system_protons_need_shim: false, user_protons: [], missing_tools: [], old_folders: [], old_folders_kept: [],
  flatpak_steam: false, latest: null, update_available: false, checked_at: null, missing_downloads: [], repair: null,
  features: { fsr: false, alt_driver_store: false }, ...over,
});
const check = (over = {}) => ({ ok: true, error: null, installed: "20261031", latest: "20261104", update_available: true,
  checked_at: 1, note: null, ...over });

test("xessOffer: install, update, nothing", () => {
  assert.equal(xessOffer(false, null, null).kind, "install");
  assert.equal(xessOffer(false, null, state({ installed: false, latest: "20261104" })).latest, "20261104");
  const up = xessOffer(true, "20261031", state({ latest: "20261104", update_available: true }));
  assert.equal(up.kind, "update");
  assert.equal(offerLabel(up), "Update to 20261104");
  assert.equal(xessOffer(true, "20261031", state()).kind, null);
  assert.equal(xessOffer(true, "20261031", null).kind, null);                    // xess_state not read (yet)
  assert.equal(xessOffer(true, "20261031", null, check()).kind, "update");       // a check tapped in this session
});

test("xessOffer never offers an older or the same release (FE-1, XESS-7)", () => {
  // the stored tag from an earlier check, older than a release installed by hand since
  assert.equal(xessOffer(true, "20261110", state({ version: "20261110", latest: "20261104", update_available: true })).kind, null);
  assert.equal(xessOffer(true, "20261104", state({ version: "20261104", latest: "20261104", update_available: true })).kind, null);
  // a backend without update_available: the tag order alone decides
  const old = state({ latest: "20261104" });
  delete old.update_available;
  assert.equal(xessOffer(true, "20261031", old).kind, "update");
  assert.equal(xessOffer(true, "20261110", { ...old, version: "20261110" }).kind, null);
  // the backend's check says no
  assert.equal(xessOffer(true, "20261031", state({ latest: "20261104" }), check({ update_available: false })).kind, null);
  // a failed check falls back to the state
  assert.equal(xessOffer(true, "20261031", state({ latest: "20261104", update_available: true }), check({ ok: false, latest: null })).kind, "update");
});

test("xessOffer: repair when the backend asks for install.sh again", () => {
  const x = state({ version: "20261104", repair: "the compiler (igc/) was not downloaded", missing_downloads: ["igc"] });
  const o = xessOffer(true, "20261104", x);
  assert.equal(o.kind, "repair");
  assert.equal(o.reason, "the compiler (igc/) was not downloaded");
  assert.equal(offerLabel(o), "Repair 20261104");
  assert.equal(installTitle(o), "Repair xess-xmx-linux?");
  const text = installLines(o, x, "jordan").join("\n");
  assert.match(text, /^The compiler \(igc\/\) was not downloaded\. Runs install\.sh of 20261104 again in \/home\/jordan\/Documents\/xess-xmx-linux-20261031 as jordan/);
  assert.match(text, /Nothing is uninstalled/);
  assert.doesNotMatch(text, /uninstall\.sh/);
  // an update wins over a repair
  assert.equal(xessOffer(true, "20261104", { ...x, latest: "20261110", update_available: true }).kind, "update");
  assert.equal(repairText(o), "The compiler (igc/) was not downloaded. Runs install.sh of 20261104 again in its folder; nothing is uninstalled.");
  assert.match(pieceText("igc"), /^igc\/, the compiler: /);
  assert.equal(pieceText("other"), "other/");
});

test("the update confirmation (FE-4, XESS-3, XESS-5)", () => {
  // FE-4: the kind comes from what the button showed, also without xess_state
  const o = xessOffer(true, "20261031", null, check());
  assert.equal(installTitle(o), "Update xess-xmx-linux?");
  let text = installLines(o, null, null).join("\n");
  assert.match(text, /First the installed 20261031 is uninstalled with its own uninstall\.sh\./);
  // XESS-3: no stored choice, so no promise to keep it
  text = installLines(o, state(), null).join("\n");
  assert.match(text, /starts with its own default Mesa driver/);
  assert.doesNotMatch(text, /is set again/);
  const x = state({ preferred_driver: "26.1", latest: "20261104", update_available: true,
    system_protons: [{ path: "/usr/share/steam/compatibilitytools.d/proton-cachyos-slr", name: "proton-cachyos-slr", shimmed: true, stock: true },
      { path: "/usr/share/steam/compatibilitytools.d/proton-cachyos", name: "proton-cachyos", shimmed: false, stock: false }] });
  text = installLines(xessOffer(true, "20261031", x), x, "jordan").join("\n");
  assert.match(text, /Your default Mesa driver \(Mesa 26\.1\) is set again afterwards\./);
  assert.match(text, /its folder \/home\/jordan\/Documents\/xess-xmx-linux-20261031 stays/);
  // XESS-5: the system Protons are changed as root
  assert.match(text, /system Protons proton-cachyos-slr and proton-cachyos as root \(keeping igdext64\.dll\.stock\)/);
  // a first install says nothing about uninstalling or a driver
  const none = state({ installed: false, package_dir: null, version: null });
  text = installLines(xessOffer(false, null, none), none, null).join("\n");
  assert.equal(installTitle(xessOffer(false, null, null)), "Install xess-xmx-linux?");
  assert.doesNotMatch(text, /uninstall|Mesa driver|system Proton/);
  assert.match(text, /Downloads xess-xmx-linux \(the newest release\)/);
});

test("the confirmation without xess_state still says what the job may do as root (RC-1)", () => {
  // the job sets settings.xess_driver again and shims every system Proton whatever the page knew
  for (const o of [xessOffer(true, "20261031", null, check()), xessOffer(false, null, null)]) {
    const text = installLines(o, null, "jordan").join("\n");
    assert.match(text, /A default Mesa driver chosen on the XeSS tab is set again afterwards; otherwise the release's own default is used\./, o.kind);
    assert.doesNotMatch(text, /starts with its own default Mesa driver/, o.kind);
    assert.match(text, /If system Protons \(CachyOS's proton-cachyos-\*\) are installed, also puts the XMX shim into them as root \(keeping igdext64\.dll\.stock\)/, o.kind);
  }
  // with xess_state the lines name what is there instead
  const x = state({ system_protons: [{ path: "/usr/share/steam/compatibilitytools.d/proton-cachyos", name: "proton-cachyos", shimmed: false, stock: false }] });
  const text = installLines(xessOffer(true, "20261031", x, check()), x, "jordan").join("\n");
  assert.doesNotMatch(text, /If system Protons|chosen on the XeSS tab/);
  assert.match(text, /system Proton proton-cachyos as root/);
});

test("an update from a folder the XeSS tab will not remove says it is the user's (RC-2)", () => {
  const why = "is a git checkout, not just an unpacked release; delete it yourself if you no longer need it";
  const x = state({ package_dir: "/home/jordan/src/xess-xmx-linux", package_dir_kept: why });
  const text = installLines(xessOffer(true, "20261031", x, check()), x, "jordan").join("\n");
  assert.match(text, /its folder \/home\/jordan\/src\/xess-xmx-linux stays: it is a git checkout, not just an unpacked release; delete it yourself if you no longer need it\. /);
  assert.doesNotMatch(text, /for you to remove after the re-login/);
  const managed = state({ managed: true, package_dir: "/home/jordan/.local/share/oxp3deck/xess/xess-xmx-linux-20261031" });
  assert.doesNotMatch(installLines(xessOffer(true, "20261031", managed, check()), managed, "jordan").join("\n"), /its folder/);
});

test("checkText (FE-5)", () => {
  assert.equal(checkText(check({ installed: null, update_available: false }), false, null), "20261104 is the newest release");
  assert.equal(checkText(check(), true, "20261031"), "20261104 is available");
  assert.equal(checkText(check({ installed: "20261104", update_available: false }), true, "20261104"), "Up to date: 20261104 is the newest release");
  assert.equal(checkText(check({ installed: "20261110", update_available: false }), true, "20261110"),
    "Up to date: the installed 20261110 is newer than GitHub's newest release (20261104)");
});

test("jobNext: reboot, not a log-out, after a failed driver check (XESS-4)", () => {
  // NEXT_INSTALL, read from xess.py so a changed wording breaks this test instead of the advice
  const py = readFileSync(new URL("../../py_modules/oxp3deck/xess.py", import.meta.url), "utf8");
  const relogin = /^NEXT_INSTALL = "(.*)"$/m.exec(py)[1];
  const failed = { state: "failed", reason: "vkCreateDevice failed" };
  assert.equal(jobNext("xess-install", { ok: true, next: relogin, status: failed }), REBOOT_AFTER_FAILED_CHECK);
  assert.equal(jobNext("xess-install", { ok: true, next: relogin, status: { state: "ok" } }), relogin);
  // the backend's own word wins: another next line, or reboot_needed false (the check ran again already)
  assert.equal(jobNext("xess-install", { ok: true, next: "Reboot now.", status: failed }), "Reboot now.");
  assert.equal(jobNext("xess-install", { ok: true, next: relogin, status: failed, reboot_needed: false }), relogin);
  assert.equal(jobNext("xess-install", { ok: true, next: null, status: failed }), null);
  assert.equal(jobNext("xess-uninstall", { ok: true, next: "Log out of Game Mode and back in (or reboot) so Steam starts without it." }),
    "Log out of Game Mode and back in (or reboot) so Steam starts without it.");
  assert.equal(jobNext("oxp3-apply", null), null);
  assert.equal(jobNext("oxp3-apply", "text"), null);
});
