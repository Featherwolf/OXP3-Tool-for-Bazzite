// The pages must not crash on a catalog from an older oxp3-tool (OXP-1: commit 7fbcb93's 0.4.0 has no button_keys
// and no min_refresh.default). The pages need Steam's React, so this checks their source for the unguarded reads.
// Run from decky/: node --experimental-strip-types --test tests/js/*.test.mjs
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const page = (name) => readFileSync(new URL(`../../src/pages/${name}`, import.meta.url), "utf8");

test("Buttons reads button_keys only with a fallback", () => {
  const src = page("Buttons.tsx");
  assert.match(src, /Object\.keys\(catalog\.button_keys \?\? \{\}\)/);
  assert.doesNotMatch(src, /Object\.keys\(catalog\??\.button_keys\)/, "Object.keys(undefined) throws");
  assert.doesNotMatch(src, /catalog\?\.button_keys\[/, "catalog?.button_keys[k] throws when button_keys is missing");
});

test("Fixes never shows min_refresh.default without a fallback", () => {
  const src = page("Fixes.tsx");
  const reads = [...src.matchAll(/min_refresh\.default(\s*\?\?)?/g)];
  assert.ok(reads.length > 0);
  for (const m of reads) assert.ok(m[1], `min_refresh.default without ?? (NaN on the slider): ${m[0]}`);
});
