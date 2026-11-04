// What the xess-xmx-linux install button offers (install, update, repair) and what its confirmation says. Pure, with
// type imports only, so tests/js can run this file under node as it is.
import type { XessState, XessUpdateCheck } from "./types";

/**
 * A release tag's sort key, as xess.tag_key: YYYYMMDD[-N] as [1, date, N or 1], vX.Y.Z (the old tags) as
 * [0, X, Y, Z], below every date tag; null for any other form.
 */
export const tagKey = (tag: string | null | undefined): number[] | null => {
  if (!tag) return null;
  const d = /^([0-9]{8})(?:-([0-9]+))?$/.exec(tag);
  if (d) return [1, Number(d[1]), Number(d[2] ?? 1)];
  const v = /^v([0-9]+(?:\.[0-9]+)*)$/.exec(tag);
  return v ? [0, ...v[1].split(".").map(Number)] : null;
};

/**
 * Release `a` is newer than `b` (xess.newer: keys compared like Python tuples, so v1.3 < v1.3.0). With a tag of
 * another form, or no installed tag, they only differ, as xess.check_update treats them. No `a`: false.
 */
export const tagNewer = (a: string | null | undefined, b: string | null | undefined): boolean => {
  if (!a) return false;
  const ka = tagKey(a), kb = tagKey(b);
  if (!ka || !kb) return a !== b;
  for (let i = 0; i < Math.min(ka.length, kb.length); i++) if (ka[i] !== kb[i]) return ka[i] > kb[i];
  return ka.length > kb.length;
};

export type XessOffer = {
  kind: "install" | "update" | "repair" | null;   // null: nothing to do (the page offers the update check)
  installed: boolean;
  version: string | null;    // the installed release
  latest: string | null;     // the release an install or update gets, when known
  reason: string | null;     // repair: why install.sh should run again
};

/**
 * What the install button offers. installed / version: what the page shows (the quick panel: the overview's
 * summary; the full page: xess_state). check: an update check tapped in this session (newer than the stored one).
 * An update needs a newer tag, not just another one (FE-1: the stored tag can be older than a package installed by
 * hand since). A repair (xess_state.repair) is install.sh again in the installed folder, for the same release.
 */
export const xessOffer = (installed: boolean, version: string | null, x: XessState | null | undefined,
  check?: XessUpdateCheck | null): XessOffer => {
  const chk = check?.ok ? check : null;
  const latest = (chk ? chk.latest : x?.latest) ?? null;
  const base = { installed, version, latest, reason: null };
  if (!installed) return { ...base, kind: "install" };
  if (tagNewer(latest, version) && (chk ? chk.update_available : x?.update_available !== false)) return { ...base, kind: "update" };
  if (x?.repair) return { ...base, kind: "repair", latest: version, reason: x.repair };
  return { ...base, kind: null };
};

/** The button's label. */
export const offerLabel = (o: XessOffer): string =>
  o.kind === "update" ? `Update to ${o.latest}` : o.kind === "repair" ? `Repair ${o.version ?? "XeSS XMX"}` : "Install XeSS XMX";

const sentence = (s: string) => {
  const t = s.trim();
  return t ? `${t[0].toUpperCase()}${t.slice(1)}${/[.!?]$/.test(t) ? "" : "."}` : "";
};

/** The repair button's description: why, and what it does. */
export const repairText = (o: XessOffer): string =>
  `${o.reason ? `${sentence(o.reason)} ` : ""}Runs install.sh of ${o.version ?? "the installed release"} again in its folder; nothing is uninstalled.`;

const and = (xs: string[]) => (xs.length < 2 ? xs.join("") : `${xs.slice(0, -1).join(", ")} and ${xs[xs.length - 1]}`);

/** missing_downloads, for the user: what each piece is and what is missing without it (install.sh's messages). */
const PIECES: Record<string, string> = {
  igc: "igc/, the compiler: XeSS's kernels are not built, so a game whose kernels are not compiled yet uses DP4a.",
  xess: "xess/, Intel's XeSS SDK: XESS=latest uses the games' own libraries.",
  ffx: "ffx/, AMD's FidelityFX SDK: FSR=xess and FSR_FG=xess work only in games with FSR 3.1.4 and newer.",
};
export const pieceText = (p: string): string => PIECES[p] ?? `${p}/`;

/** The confirmation's title and paragraphs. */
export const installTitle = (o: XessOffer): string =>
  o.kind === "update" ? "Update xess-xmx-linux?" : o.kind === "repair" ? "Repair xess-xmx-linux?" : "Install xess-xmx-linux?";

export const installLines = (o: XessOffer, x: XessState | null | undefined, user: string | null): string[] => {
  const who = user ?? "your user";
  const lines: string[] = [];
  if (o.kind === "repair") {
    lines.push(`${o.reason ? `${sentence(o.reason)} ` : ""}Runs install.sh of ${o.version ?? "the installed release"} again in ${
      x?.package_dir ?? "its folder"} as ${who}: it downloads only what is missing and sets up the driver setting, the shims and its services again. Nothing is uninstalled; if GitHub has a newer release by now, it updates to that one instead.`);
  } else {
    lines.push(`Downloads xess-xmx-linux ${o.latest ?? "(the newest release)"} from GitHub into a new folder and runs its install.sh as ${
      who} (it also fetches Intel's XeSS SDK and compiler: a few hundred MB).`);
  }
  // XESS-3: only a driver chosen in OXP3 Deck (xess_driver) is set again; lib/active belongs to the old folder
  const driver = x?.preferred_driver ? `Your default Mesa driver (Mesa ${x.preferred_driver}) is set again afterwards.`
    : o.kind === "update" ? "The new release starts with its own default Mesa driver (one switched by hand with switch-driver.sh is not carried over); pick another on the XeSS tab afterwards if needed."
    : "";
  if (o.kind === "update") {
    const folder = x && !x.managed && x.package_dir ? `; its folder ${x.package_dir} stays, for you to remove after the re-login (XeSS tab)` : "";
    lines.push(`First the installed ${o.version ?? "package"} is uninstalled with its own uninstall.sh${folder}. ${driver}`.trim());
  } else if (driver) {
    lines.push(driver);
  }
  // XESS-5: the install puts the shim into the system Protons as root (xess._install_into, _shim_system)
  const sys = (x?.system_protons ?? []).map((p) => p.name);
  if (sys.length) {
    lines.push(`Also puts the XMX shim into the system Proton${sys.length === 1 ? "" : "s"} ${and(sys)} as root (keeping igdext64.dll.stock); Uninstall puts the stock file back.`);
  }
  lines.push(o.kind === "repair"
    ? "Close running games first. The task says at the end whether you need to log out and back in."
    : "Close running games first. Afterwards log out and back in (or reboot) so Steam starts with the new driver. Each game's first start then compiles XeSS's kernels for a minute or two, once.");
  return lines;
};

/**
 * The answer of a tapped update check, for the button's description. FE-5: with nothing installed it names the newest
 * release instead of saying "up to date".
 */
export const checkText = (c: XessUpdateCheck, installed: boolean, version: string | null): string => {
  const have = c.installed ?? version;
  if (!installed) return `${c.latest ?? "?"} is the newest release`;
  if (c.update_available) return `${c.latest} is available`;
  if (have && c.latest && tagNewer(have, c.latest)) return `Up to date: the installed ${have} is newer than GitHub's newest release (${c.latest})`;
  return `Up to date: ${have ?? c.latest ?? "?"} is the newest release`;
};

/** xess.py's NEXT_INSTALL: a log-out, which is not enough after a failed driver check (XESS-4). */
const RELOGIN = /^Log out of Game Mode and back in \(or reboot\)/;
export const REBOOT_AFTER_FAILED_CHECK = "Reboot (not just log out): the new driver failed its check, and the session " +
  "check that falls back to the package's other driver or to the system's runs again only at boot. After a log-out " +
  "alone Game Mode may not start.";

/**
 * The "Next" line under a finished job: the result's `next`, except after an xess install whose new driver failed
 * its check while `next` still says to log out (the check unit stays active since boot, so a log-out does not run
 * it again) and the result does not say otherwise (reboot_needed: false).
 */
export const jobNext = (kind: string, result: unknown): string | null => {
  const r = result && typeof result === "object" ? result as Record<string, any> : {};
  const next = typeof r.next === "string" ? r.next : null;
  if (kind === "xess-install" && r.status?.state === "failed" && r.reboot_needed !== false && next && RELOGIN.test(next)) {
    return REBOOT_AFTER_FAILED_CHECK;
  }
  return next;
};
