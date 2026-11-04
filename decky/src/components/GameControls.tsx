// Adapted from ClawDeck (MIT, Copyright (c) 2026 IHulkSmashI and ClawDeck contributors).
// Based on ClawDeck's per-game tab (src/tabs/PerGame.tsx).
// One game's xess-xmx-linux options, written straight into its Steam launch options (before %command%), plus what
// the backend found about the game (its XeSS DLLs, its Proton, the last run's logs).
import { ButtonItem, DropdownItem, PanelSectionRow, ToggleField } from "@decky/ui";
import { FC, ReactNode, useEffect, useMemo, useRef, useState } from "react";

import { game_info, xess_state } from "../backend";
import { driverPrefix, fixTypo, fsrSupported, managed, ManagedKey, parse, setManaged } from "../launchopts";
import { getLaunchOptions, isSteamApp, RunningApp, setLaunchOptions } from "../steam";
import { useResource } from "../store";
import type { GameInfo, GameInfoRes, XessDriver, XessStateRes } from "../types";
import { remember, when } from "../util";
import { Code, explain, Note, Problem, Status } from "./Hint";

type Opt = { data: string; label: string };

/** The FSR=/FSR_FG= pair as one choice (docs/fsr-frame-generation.md in xess-xmx-linux 20261104). */
const FSR_MODES: { data: string; label: string; set: { FSR: string | null; FSR_FG: string | null }; does: string }[] = [
  { data: "", label: "Game's own FSR", set: { FSR: null, FSR_FG: null }, does: "The game's FSR stays as it is." },
  { data: "all", label: "FSR on XeSS", set: { FSR: "xess", FSR_FG: null },
    does: "FSR=xess: the game's FSR 3.1+ upscaling runs on XeSS super resolution and its FSR frame generation on XeSS frame generation (with the frame generation choice above: multi-frame). Only for games with FSR 3.1+ and no XeSS; choose FSR in the game." },
  { data: "fg", label: "FSR frame gen on XeSS", set: { FSR: null, FSR_FG: "xess" },
    does: "FSR_FG=xess: only the game's FSR 3.1+ frame generation runs on XeSS frame generation; the game's FSR upscaling stays." },
  { data: "inject", label: "Add XeSS frame gen", set: { FSR: "xess", FSR_FG: "inject" },
    does: "FSR=xess FSR_FG=inject: XeSS frame generation for a game with FSR 3.1+ upscaling but no FSR frame generation (Forza Horizon 6). Choose FSR upscaling in the game; the HUD is interpolated too." },
];

const fsrMode = (fsr?: string, fg?: string): string => {
  if (!fsr && !fg) return "";
  if (fsr === "xess" && !fg) return "all";
  if (!fsr && fg === "xess") return "fg";
  if (fsr === "xess" && fg === "inject") return "inject";
  return "custom";
};

/** One option per installed driver prefix (26.1, 26.3): MESA= takes a prefix, which survives an update. */
const driverOptions = (drivers: XessDriver[]): Opt[] => {
  const seen = new Map<string, string[]>();
  for (const d of drivers) {
    const p = driverPrefix(d.version);
    seen.set(p, [...(seen.get(p) ?? []), d.version]);
  }
  return [...seen.entries()].map(([p, versions]) => ({ data: p, label: `Mesa ${versions.join(" / ")}` }));
};

const lastRunLine = (lr: NonNullable<GameInfoRes["game"]>["last_run"]): { ok: boolean | null; label: string; detail: string } => {
  if (!lr) return { ok: null, label: "Last run: no logs", detail: "Nothing from xess-xmx-linux in this game's prefix yet: start it once (with Logs on for the details)." };
  if (lr.fallback) return { ok: false, label: "Last run: XeSS fell back to DP4a", detail: lr.fallback };
  if (lr.compile_failed) return { ok: false, label: "Last run: a kernel did not compile", detail: "The patched driver could not build one of XeSS's kernels; ~/.cache/xess-xmx/compile.log has the line." };
  if (lr.canary) return { ok: true, label: "Last run: XeSS on the XMX cores", detail: "The patched driver saw XeSS's self-test in this game." };
  return { ok: null, label: "Last run: no sign of XMX", detail: lr.trace ? "The shim was loaded (trace log), but the patched driver saw no XeSS self-test: the game may not have used XeSS." : "No trace log: turn Logs on and start the game to see whether the shim loads." };
};

const toolName = (g: GameInfo): string =>
  g.compat_tool ?? (g.default_compat_tool ? `${g.default_compat_tool} (Steam's default)` : "Steam's default");

/** The shim (igdext64.dll: XeSS on the XMX cores) and the hook (user_settings.py: the XESS / FSR shorthands). */
const protonLine = (g: GameInfo): string => {
  if (g.compat_tool_shimmed === false) {
    return `Its igdext64.dll is not xess-xmx-linux's shim, so XeSS stays on DP4a. ${g.compat_tool_system
      ? "Add the shim to the system Protons on the XeSS tab, or pick" : "Run the XeSS install again, or pick"} another Proton in the game's Properties → Compatibility.`;
  }
  if (g.compat_tool_shimmed === true) {
    return g.compat_tool_hook === false
      ? "Has the shim (XeSS on the XMX cores) but not the hook: XESS=latest, XESS=dp4a, XESS_LOG and FSR do nothing on this Proton."
      : g.compat_tool_hook ? "Has the shim (XeSS on the XMX cores) and the hook (XESS, XESS_LOG and FSR work)." : "Has the shim (XeSS on the XMX cores).";
  }
  return g.compat_tool_dir ? "This Proton has no driver store of its own (no igdext64.dll); see the advice below."
    : "Its folder was not found, so whether it carries the shim is unknown.";
};

export const GameControls: FC<{ app: RunningApp; quick?: boolean; running?: boolean }> = ({ app, quick, running }) => {
  const steam = isSteamApp(app.appid);
  const xs = useResource<XessStateRes>("xess", xess_state);
  // the backend knows Steam games only (appmanifest, compatdata); a shortcut gets no game check
  const gi = useResource<GameInfoRes>(steam ? `game:${app.appid}` : null, () => game_info(app.appid), { fresh: true });
  const [opts, setOpts] = useState<string | null | undefined>(undefined);   // undefined: reading; null: unreadable
  const [draft, setDraft] = useState("");          // what to paste, when the plugin cannot write it itself
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [rev, setRev] = useState(0);              // remounts the dropdowns when the value is not what was picked
  const [experimental, setExperimental] = useState(() => remember.get("fgExperimental") === "1");

  const seq = useRef(0);
  const read = async () => {
    const mine = ++seq.current;   // a read for a game the panel has left since is dropped
    setOpts(undefined);
    const o = steam ? await getLaunchOptions(app.appid) : null;
    if (mine !== seq.current) return;
    setOpts(o);
    setRev((n) => n + 1);
  };
  useEffect(() => { setErr(null); setSaved(false); setDraft(""); read(); }, [app.appid]);

  const writable = steam && typeof opts === "string";
  const text = writable ? (opts as string) : draft;
  const p = useMemo(() => parse(text), [text]);
  const cur = managed(p);
  const blocked = p.typo && !p.hasCommand;
  const x = xs.data?.xess ?? null;
  const drivers = x?.drivers ?? [];
  const defaultDriver = drivers.find((d) => d.default)?.version ?? x?.preferred_driver ?? null;
  const showFsr = fsrSupported(x?.version) || !!x?.features?.fsr || !!cur.FSR || !!cur.FSR_FG;
  const disabled = busy || opts === undefined || blocked;
  const nextStart = running ? `Applies when you start ${app.name} again.` : `Applies at the next start of ${app.name}.`;

  const write = async (next: string) => {
    if (next === text) return;
    setErr(null);
    if (!writable) { setDraft(next); setSaved(false); return; }
    setBusy(true);
    const r = await setLaunchOptions(app.appid, next);
    setBusy(false);
    const now = r.text ?? (r.ok ? next : text);
    setOpts(now);
    if (now !== next) setRev((n) => n + 1);
    setErr(r.ok ? null : r.error);
    setSaved(r.ok);
  };
  const change = (c: Partial<Record<ManagedKey, string | null>>) => write(setManaged(text, c));

  const xessOpts = useMemo<Opt[]>(() => {
    const o = [{ data: "", label: "Game's own" }, { data: "latest", label: "Latest (package)" }, { data: "dp4a", label: "DP4a, no XMX" }];
    return cur.XESS && !o.some((v) => v.data === cur.XESS) ? [...o, { data: cur.XESS, label: `${cur.XESS} (unknown)` }] : o;
  }, [cur.XESS]);
  const fgOpts = useMemo<Opt[]>(() => {
    const o: Opt[] = [{ data: "", label: "Game default" }, { data: "2", label: "2x" }, { data: "3", label: "3x" }, { data: "4", label: "4x" }];
    const n = Number(cur.XESS_FG);
    for (const k of [5, 6, 7, 8]) if (experimental || n === k) o.push({ data: String(k), label: `${k}x (experimental)` });
    return cur.XESS_FG && !o.some((v) => v.data === cur.XESS_FG) ? [...o, { data: cur.XESS_FG, label: `${cur.XESS_FG} (unknown)` }] : o;
  }, [cur.XESS_FG, experimental]);
  const mesaOpts = useMemo<Opt[]>(() => {
    const o = [{ data: "", label: defaultDriver ? `Default (${defaultDriver})` : "Default" }, ...driverOptions(drivers)];
    if (!cur.MESA || o.some((v) => v.data === cur.MESA)) return o;
    // MESA takes any prefix of a version (26.3.0-rc2 too); the ICD loader falls back to the default for others
    const known = drivers.some((d) => d.version.startsWith(cur.MESA as string));
    return [...o, { data: cur.MESA, label: known ? `Mesa ${cur.MESA}` : `${cur.MESA} (not installed)` }];
  }, [cur.MESA, drivers.map((d) => d.version).join(","), defaultDriver]);
  const mode = fsrMode(cur.FSR, cur.FSR_FG);
  const fsrOpts = useMemo<Opt[]>(() => [...FSR_MODES.map((m) => ({ data: m.data, label: m.label })),
    ...(mode === "custom" ? [{ data: "custom", label: `FSR=${cur.FSR ?? ""} FSR_FG=${cur.FSR_FG ?? ""}` }] : [])], [mode, cur.FSR, cur.FSR_FG]);

  const key = `${app.appid}-${rev}-${opts === undefined ? "reading" : "read"}-${drivers.length}`;
  const below = { layout: "below" as const, childrenContainerWidth: "max" as const, bottomSeparator: "none" as const };
  const desc = (does: ReactNode, note?: ReactNode) => explain(does, nextStart, quick ? undefined : note);
  const fgN = Number(cur.XESS_FG);
  const game = gi.data?.game ?? null;
  const lr = game ? lastRunLine(game.last_run) : null;

  return (
    <>
      {xs.data?.ok && !x?.installed && (
        <PanelSectionRow><Note>xess-xmx-linux is not installed: these options do nothing until it is (XeSS above, or the XeSS tab).</Note></PanelSectionRow>
      )}
      {blocked && (
        <PanelSectionRow>
          <Status ok={false} red label="Launch options: %command is missing its closing %"
            detail="Steam then passes everything as game arguments, so no variable reaches the game. Fix it to use the options below." />
          <ButtonItem layout="below" disabled={busy || opts === undefined} onClick={() => write(fixTypo(text))}
            description={explain("Changes %command to %command% and leaves the rest as it is.", nextStart)}>
            Fix: %command%
          </ButtonItem>
        </PanelSectionRow>
      )}
      <PanelSectionRow>
        <DropdownItem key={`xess-${key}`} {...below} label="XeSS libraries" menuLabel="XeSS libraries" disabled={disabled}
          rgOptions={xessOpts} selectedOption={cur.XESS ?? ""} onChange={(o) => change({ XESS: o.data || null })}
          description={desc(cur.XESS === "latest" ? "XESS=latest: Intel's newest XeSS libraries from the package replace the game's older ones while it runs; its own files are back when it exits. For XeSS older than 2.0.2.68."
            : cur.XESS === "dp4a" ? "XESS=dp4a: XeSS without Intel's extension (super resolution on DP4a, frame generation 2x only). For comparing, or ruling the XMX path out."
            : "The game's own XeSS libraries, on the XMX cores through the patched driver.",
          "Needs the xess-xmx-linux hook in the game's Proton.")} />
        <DropdownItem key={`fg-${key}-${experimental}`} {...below} label="Frame generation" menuLabel="XeSS frame generation"
          disabled={disabled} rgOptions={fgOpts} selectedOption={cur.XESS_FG ?? ""} onChange={(o) => change({ XESS_FG: o.data || null })}
          description={desc(cur.XESS_FG ? `XESS_FG=${cur.XESS_FG}: XeSS frame generation makes ${cur.XESS_FG}x, whatever the game's menu says. Turn frame generation on in the game and keep a frame limit on.${fgN > 4 ? " Experimental: beyond what Intel supports; 7x and 8x add latency." : ""}`
            : "The game's own frame generation choice.",
          "Needs XeSS frame generation 1.2 or newer in the game (else also Latest above).")} />
        <ToggleField label="5x to 8x (experimental)" checked={experimental} bottomSeparator="none"
          onChange={(v) => { setExperimental(v); remember.set("fgExperimental", v ? "1" : "0"); }}
          description={explain("Offers 5x to 8x in the list above: the shim raises the number inside Intel's library, which Intel does not support. 7x and 8x are not recommended (latency).", "Only changes the list; nothing is written.")} />
        <DropdownItem key={`mesa-${key}`} {...below} label="Mesa driver" menuLabel="Mesa driver" disabled={disabled}
          rgOptions={mesaOpts} selectedOption={cur.MESA ?? ""} onChange={(o) => change({ MESA: o.data || null })}
          description={desc(cur.MESA ? `MESA=${cur.MESA}: this game uses the package's Mesa ${cur.MESA} driver.` : "The package's default driver (the XeSS tab sets it for all games).",
            "For a game that misbehaves on one driver only.")} />
        <ToggleField label="Hide the Intel GPU" checked={cur.force_vk_vendor === "-1"} disabled={disabled} bottomSeparator="none"
          onChange={(v) => change({ force_vk_vendor: v ? "-1" : null })}
          description={desc("force_vk_vendor=-1: Mesa's own profile for this game again (the Intel vendor id is hidden). XeSS then runs on DP4a.",
            "Only for a game that needs Mesa's workaround.")} />
        <ToggleField label="Logs" checked={!!cur.XESS_LOG && cur.XESS_LOG !== "0"} disabled={disabled} bottomSeparator="none"
          onChange={(v) => change({ XESS_LOG: v ? "1" : null })}
          description={desc("XESS_LOG=1: the shim's, XeLL's and XeSS frame generation's logs in the game's prefix (C:\\igdext_trace.log and others), for a bug report.",
            "Leave it off otherwise.")} />
        {showFsr && (
          <DropdownItem key={`fsr-${key}`} {...below} label="FSR" menuLabel="FSR on XeSS" disabled={disabled}
            rgOptions={fsrOpts} selectedOption={mode}
            onChange={(o) => { const m = FSR_MODES.find((f) => f.data === o.data); if (m) change(m.set); }}
            description={desc(FSR_MODES.find((f) => f.data === mode)?.does ?? "Set by hand; pick one of the others to replace it.",
              "Needs xess-xmx-linux 20261104 or newer and its hook in the game's Proton.")} />
        )}
        {busy && <Status ok={null} label="Writing the launch options…" />}
        {saved && !err && <Status ok label="Saved" detail={nextStart} />}
        <Problem what="Launch options" error={err} />
        {writable ? (
          <Note label="Launch options"><Code>{text || "(none)"}</Code></Note>
        ) : opts === undefined ? (
          <Note label="Launch options">Reading them from Steam…</Note>
        ) : (
          <>
            <Note label={steam ? "Steam did not return this game's launch options" : "Non-Steam game"}>
              {steam ? "So OXP3 Deck does not write them (that would replace yours). " : "OXP3 Deck does not change a shortcut's launch options. "}
              Paste this into the game's Properties → Launch options{text ? ":" : " once you picked something above."}
              {text && <><br /><Code>{text}</Code></>}
            </Note>
            {steam && <ButtonItem layout="below" onClick={read} description="Asks Steam for this game's launch options again.">Read again</ButtonItem>}
          </>
        )}
      </PanelSectionRow>
      <PanelSectionRow>
        <Problem what="Game check" error={gi.data && !gi.data.ok ? gi.data.error : null} />
        {game && <Status ok={game.compat_tool_shimmed} label={`Proton: ${toolName(game)}`} detail={protonLine(game)} />}
        {(game?.advice ?? []).map((a, i) => <Note key={i} label={i === 0 ? "Advice" : undefined}>{a}</Note>)}
        {lr && <Status ok={lr.ok} label={lr.label} detail={quick ? undefined : lr.detail} />}
        {!quick && game?.last_run && (
          <Note label="Last run details">
            {`Trace log: ${game.last_run.trace ? "yes (the shim loaded)" : "no"} · patched driver self-test: ${game.last_run.canary ? "seen" : "not seen"} · kernel compile: ${game.last_run.compile_failed ? "failed" : "ok"}`}
            {game.last_run.xell_unsupported === true ? " · XeLL: this game's version is not supported" : game.last_run.xell_unsupported === false ? " · XeLL: supported" : ""}
            {game.last_run.logged_at ? ` · logged ${when(game.last_run.logged_at)}` : ""}
          </Note>
        )}
        {!quick && game && (
          <Note label="XeSS files in the game">
            {game.dlls?.length ? game.dlls.map((d) => `${d.name} ${d.version ?? "(version unknown)"}${d.link ? " (a link left by XESS=latest)" : ""}`).join(" · ") : "None found (the game may not use XeSS, or keeps it elsewhere)."}
          </Note>
        )}
        {!quick && game?.prefix && <Note label="Prefix"><Code>{game.prefix}</Code></Note>}
      </PanelSectionRow>
    </>
  );
};
