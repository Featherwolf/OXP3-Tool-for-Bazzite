// Steam launch options: read and change the variables OXP3 Deck manages before %command%, and keep every other
// token as the user wrote it. Pure functions with no imports, so tests/js can run this file under node as it is.

export type ManagedKey = "XESS" | "XESS_FG" | "MESA" | "force_vk_vendor" | "XESS_LOG" | "FSR" | "FSR_FG";

/** The variables the plugin manages (xess-xmx-linux docs/launch-options.md). FSR and FSR_FG need 20261104+. */
export const MANAGED_KEYS: readonly ManagedKey[] = ["XESS", "XESS_FG", "MESA", "force_vk_vendor", "XESS_LOG", "FSR", "FSR_FG"];
export const FSR_KEYS: readonly ManagedKey[] = ["FSR", "FSR_FG"];

/** The values xess-xmx-linux reads (MESA takes a driver version or a prefix of one, e.g. 26.1). */
export const KNOWN_VALUES: Partial<Record<ManagedKey, readonly string[]>> = {
  XESS: ["latest", "dp4a"],
  XESS_FG: ["2", "3", "4", "5", "6", "7", "8"],
  force_vk_vendor: ["-1"],
  XESS_LOG: ["1"],
  FSR: ["xess"],
  FSR_FG: ["xess", "inject"],
};

export type Token = { raw: string; key: string | null; value: string | null; end: number };

export type Parsed = {
  vars: [string, string][];   // KEY=VALUE tokens before %command% (values without their quotes)
  others: string[];           // the other tokens before %command% (wrappers such as gamemoderun), as written
  after: string;              // the text after %command% (the game's arguments), as written
  hasCommand: boolean;
  typo: boolean;              // "%command" without the closing %: Steam then passes everything as game arguments
  tokens: Token[];            // every token before %command%, in order
  command: string;            // the token holding %command%, as written ("" without one)
};

const COMMAND = "%command%";
const TYPO = /%command(?!%)/;
const ASSIGN = /^([A-Za-z_][A-Za-z0-9_]*)=([\s\S]*)$/;

/** Split like a shell does (quotes and backslashes keep spaces inside a token), keeping each token's raw text. */
const tokenize = (s: string): Token[] => {
  const out: Token[] = [];
  let i = 0;
  while (i < s.length) {
    while (i < s.length && /\s/.test(s[i])) i++;
    if (i >= s.length) break;
    const start = i;
    while (i < s.length && !/\s/.test(s[i])) {
      const c = s[i];
      if (c === "'") {
        const close = s.indexOf("'", i + 1);
        i = close < 0 ? s.length : close + 1;
      } else if (c === "\"") {
        i++;
        while (i < s.length && s[i] !== "\"") i += s[i] === "\\" ? 2 : 1;
        i = Math.min(i + 1, s.length);
      } else if (c === "\\") {
        i = Math.min(i + 2, s.length);
      } else {
        i++;
      }
    }
    const raw = s.slice(start, i);
    const m = ASSIGN.exec(raw);
    out.push({ raw, key: m ? m[1] : null, value: m ? unquote(m[2]) : null, end: i });
  }
  return out;
};

/** A shell word without its quoting: 'a b' and "a b" and a\ b are all `a b`. */
export const unquote = (w: string): string => {
  let r = "";
  for (let i = 0; i < w.length; i++) {
    const c = w[i];
    if (c === "'") {
      const close = w.indexOf("'", i + 1);
      const end = close < 0 ? w.length : close;
      r += w.slice(i + 1, end);
      i = end;
    } else if (c === "\"") {
      i++;
      while (i < w.length && w[i] !== "\"") {
        if (w[i] === "\\" && i + 1 < w.length && "\"\\$`".includes(w[i + 1])) i++;
        r += w[i];
        i++;
      }
    } else if (c === "\\" && i + 1 < w.length) {
      r += w[++i];
    } else {
      r += c;
    }
  }
  return r;
};

/** A value as a shell word: plain when it is safe, else in single quotes. */
const quote = (v: string): string => (/^[A-Za-z0-9._:,+/@%-]+$/.test(v) ? v : `'${v.replace(/'/g, "'\\''")}'`);

export const parse = (str: string): Parsed => {
  const s = str ?? "";
  const all = tokenize(s);
  const at = all.findIndex((t) => t.raw.includes(COMMAND));
  const typo = TYPO.test(s);
  if (at < 0) {
    return { vars: [], others: [], after: s.trim(), hasCommand: false, typo, tokens: [], command: "" };
  }
  const tokens = all.slice(0, at);
  return {
    vars: tokens.filter((t) => t.key !== null).map((t) => [t.key as string, t.value as string]),
    others: tokens.filter((t) => t.key === null).map((t) => t.raw),
    after: s.slice(all[at].end).trim(),
    hasCommand: true,
    typo,
    tokens,
    command: all[at].raw,
  };
};

/** The managed variables that are set (the last one wins, as in a shell). */
export const managed = (p: Parsed): Partial<Record<ManagedKey, string>> => {
  const r: Partial<Record<ManagedKey, string>> = {};
  for (const [k, v] of p.vars) if ((MANAGED_KEYS as readonly string[]).includes(k)) r[k as ManagedKey] = v;
  return r;
};

/**
 * Set (a string) or remove (null or "") managed variables; every other token keeps its place and quoting. Text
 * without %command% is the game's arguments, so the result is "VARS %command% ARGS". Nothing left but %command%
 * gives "". A "%command" typo is left alone (the string comes back unchanged): fixTypo() is the user's choice.
 */
export const setManaged = (str: string, changes: Partial<Record<ManagedKey, string | null>>): string => {
  const s = str ?? "";
  const p = parse(s);
  if (p.typo && !p.hasCommand) return s;
  let tokens = p.tokens.slice();
  const command = p.hasCommand ? p.command : COMMAND;
  const after = p.after;
  let changed = false;
  for (const [key, value] of Object.entries(changes) as [ManagedKey, string | null | undefined][]) {
    if (value === undefined) continue;
    if (value === null || value === "") {
      const kept = tokens.filter((t) => t.key !== key);
      if (kept.length !== tokens.length) changed = true;
      tokens = kept;
      continue;
    }
    const raw = `${key}=${quote(value)}`;
    const next: Token[] = [];
    let placed = false;
    for (const t of tokens) {
      if (t.key !== key) { next.push(t); continue; }
      if (placed) { changed = true; continue; }   // a second copy would win in the shell: drop it
      placed = true;
      if (t.value === value) { next.push(t); continue; }   // same value: keep the user's spelling of it
      changed = true;
      next.push({ raw, key, value, end: -1 });
    }
    if (!placed) {
      // after the leading assignments, so a wrapper such as gamemoderun does not take it as an argument
      const at = next.findIndex((t) => t.key === null);
      next.splice(at < 0 ? next.length : at, 0, { raw, key, value, end: -1 });
      changed = true;
    }
    tokens = next;
  }
  if (!changed) return s;
  const head = tokens.map((t) => t.raw);
  if (!head.length && command === COMMAND && !after) return "";
  return [...head, command, ...(after ? [after] : [])].join(" ");
};

/** "%command" (no closing %) to "%command%"; offered behind a button, never applied on its own. */
export const fixTypo = (str: string): string => (str ?? "").replace(/%command(?!%)/g, COMMAND);

/** FSR / FSR_FG arrived in xess-xmx-linux 20261104 (folder tags are dates, sometimes with a -N suffix). */
export const fsrSupported = (version: string | null | undefined): boolean => {
  const m = /^(\d{8})/.exec(version ?? "");
  return !!m && Number(m[1]) >= 20261104;
};

/** "26.3.0-rc2" -> "26.3": what MESA= takes, and what stays valid when an update brings a newer build. */
export const driverPrefix = (version: string): string => {
  const m = /^(\d+)\.(\d+)/.exec(version);
  return m ? `${m[1]}.${m[2]}` : version;
};
