// Tests for src/launchopts.ts. Run from decky/: node --experimental-strip-types --test tests/js/*.test.mjs
import assert from "node:assert/strict";
import { test } from "node:test";

import { driverPrefix, fixTypo, fsrSupported, managed, parse, setManaged } from "../../src/launchopts.ts";

test("parse splits variables, wrappers and arguments", () => {
  const p = parse(`PROTON_LOG=1 XESS="latest" gamemoderun %command% -dx11  --skip`);
  assert.deepEqual(p.vars, [["PROTON_LOG", "1"], ["XESS", "latest"]]);
  assert.deepEqual(p.others, ["gamemoderun"]);
  assert.equal(p.after, "-dx11  --skip");
  assert.equal(p.hasCommand, true);
  assert.equal(p.typo, false);
  assert.deepEqual(managed(p), { XESS: "latest" });
});

test("quoted values keep their spaces", () => {
  const p = parse(`FOO='a b' BAR="c \\"d\\"" BAZ=e\\ f %command%`);
  assert.deepEqual(p.vars, [["FOO", "a b"], ["BAR", "c \"d\""], ["BAZ", "e f"]]);
});

test("empty and plain options", () => {
  assert.equal(setManaged("", { XESS: "latest" }), "XESS=latest %command%");
  assert.equal(setManaged("   ", { XESS_FG: "3" }), "XESS_FG=3 %command%");
  assert.equal(setManaged("%command%", { XESS: null }), "%command%");
  assert.equal(setManaged("", { XESS: null }), "");
});

test("text without %command% is game arguments", () => {
  assert.equal(setManaged("-dx11 -novid", { XESS: "dp4a" }), "XESS=dp4a %command% -dx11 -novid");
  assert.equal(setManaged("-dx11", { XESS: null }), "-dx11");
});

test("removing the last managed variable leaves nothing", () => {
  assert.equal(setManaged("XESS=latest %command%", { XESS: null }), "");
  assert.equal(setManaged("XESS=latest XESS_FG=3 %command%", { XESS: null, XESS_FG: null }), "");
  assert.equal(setManaged("XESS=latest %command% -dx11", { XESS: null }), "%command% -dx11");
  assert.equal(setManaged("XESS=latest gamemoderun %command%", { XESS: null }), "gamemoderun %command%");
});

test("other tokens keep their order and quoting", () => {
  const s = `PROTON_LOG=1 FOO="a b" gamemoderun mangohud %command% -arg "x y"`;
  assert.equal(setManaged(s, { XESS: "latest" }),
    `PROTON_LOG=1 FOO="a b" XESS=latest gamemoderun mangohud %command% -arg "x y"`);
  assert.equal(setManaged(s, { MESA: "26.1", force_vk_vendor: "-1" }),
    `PROTON_LOG=1 FOO="a b" MESA=26.1 force_vk_vendor=-1 gamemoderun mangohud %command% -arg "x y"`);
});

test("a changed value stays where it was", () => {
  assert.equal(setManaged("A=1 XESS_FG=2 B=2 %command%", { XESS_FG: "4" }), "A=1 XESS_FG=4 B=2 %command%");
  assert.equal(setManaged("A=1 XESS_FG=2 B=2 %command%", { XESS_FG: null }), "A=1 B=2 %command%");
});

test("duplicates collapse to one", () => {
  assert.equal(setManaged("XESS=dp4a XESS=latest %command%", { XESS: "latest" }), "XESS=latest %command%");
});

test("no change returns the text exactly", () => {
  const s = `  XESS="latest"   %command%   -x `;
  assert.equal(setManaged(s, { XESS: "latest" }), s);
  assert.equal(setManaged(s, { XESS_FG: null }), s);
});

test("FSR keys", () => {
  assert.equal(setManaged("%command%", { FSR: "xess", FSR_FG: "inject" }), "FSR=xess FSR_FG=inject %command%");
  assert.equal(setManaged("FSR=xess FSR_FG=inject %command%", { FSR: null, FSR_FG: "xess" }), "FSR_FG=xess %command%");
  assert.equal(fsrSupported("20261104"), true);
  assert.equal(fsrSupported("20261102-2"), false);
  assert.equal(fsrSupported("20261031"), false);
  assert.equal(fsrSupported("v1.3.2"), false);
  assert.equal(fsrSupported(null), false);
});

test("a %command typo is shown, never changed silently", () => {
  const s = "XESS=latest %command -dx11";
  const p = parse(s);
  assert.equal(p.typo, true);
  assert.equal(p.hasCommand, false);
  assert.equal(setManaged(s, { XESS_FG: "3" }), s);
  assert.equal(fixTypo(s), "XESS=latest %command% -dx11");
  assert.equal(parse("%command%").typo, false);
});

test("odd values are quoted", () => {
  assert.equal(setManaged("%command%", { MESA: "26 1" }), "MESA='26 1' %command%");
});

test("driver prefixes", () => {
  assert.equal(driverPrefix("26.3.0-rc2"), "26.3");
  assert.equal(driverPrefix("26.1.2"), "26.1");
  assert.equal(driverPrefix("odd"), "odd");
});
