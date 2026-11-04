// Adapted from ClawDeck (MIT, Copyright (c) 2026 IHulkSmashI and ClawDeck contributors).
// Steam client calls, each behind try/catch so a Steam update that renames one turns a feature off instead of
// breaking the panel. Names checked against @decky/ui's typings: Router = CSteamUIStore (MainRunningApp),
// SteamClient.Apps.RegisterForAppDetails / SetAppLaunchOptions, GameSessions.RegisterForAppLifetimeNotifications.
import { Router } from "@decky/ui";
import { useEffect, useState } from "react";

import { remember } from "./util";

export type RunningApp = { appid: string; name: string };

/** A Steam game's appid: a non-Steam shortcut has one of 2^31 or more, and its launch options work differently. */
export const isSteamApp = (appid: string | null | undefined): boolean =>
  !!appid && /^\d+$/.test(appid) && Number(appid) > 0 && Number(appid) < 2 ** 31;

const client = () => (window as any).SteamClient as Window["SteamClient"] | undefined;

export const runningApp = (): RunningApp | null => {
  try {
    const a = Router?.MainRunningApp;
    if (!a) return null;
    // typed number in @decky/ui 4.16 and string in 4.12 (Decky 3.2's copy)
    const appid = String(a.appid);
    return { appid, name: a.display_name || `App ${appid}` };
  } catch {
    return null;
  }
};

/** The last game seen running, for the Games tab when none runs now. */
export const lastGame = (): RunningApp | null => {
  try {
    const g = JSON.parse(remember.get("lastGame") ?? "null");
    return g && typeof g.appid === "string" ? { appid: g.appid, name: String(g.name ?? g.appid) } : null;
  } catch {
    return null;
  }
};

/** The running game; follows game starts and exits (Steam's app lifetime notifications). */
export const useRunningApp = (): RunningApp | null => {
  const [app, setApp] = useState<RunningApp | null>(runningApp);
  useEffect(() => {
    const timers: ReturnType<typeof setTimeout>[] = [];
    const update = () => setApp((old) => {
      const now = runningApp();
      return old?.appid === now?.appid && old?.name === now?.name ? old : now;
    });
    let reg: { unregister(): void } | null = null;
    try {
      reg = client()?.GameSessions?.RegisterForAppLifetimeNotifications?.(() => {
        // Router catches up a moment after the notification
        update();
        timers.push(setTimeout(update, 1000));
      }) ?? null;
    } catch {
      reg = null;
    }
    update();
    return () => {
      timers.forEach(clearTimeout);
      try { reg?.unregister(); } catch { /* already gone */ }
    };
  }, []);
  useEffect(() => { if (app) remember.set("lastGame", JSON.stringify(app)); }, [app?.appid]);
  return app;
};

/** The game's launch options as Steam stores them; null when Steam does not answer within 1.5 s. */
export const getLaunchOptions = (appid: string): Promise<string | null> => new Promise((resolve) => {
  if (!isSteamApp(appid)) { resolve(null); return; }
  let done = false;
  let reg: { unregister(): void } | null = null;
  const finish = (v: string | null) => {
    if (done) return;
    done = true;
    clearTimeout(timer);
    try { reg?.unregister(); } catch { /* already gone */ }
    resolve(v);
  };
  const timer = setTimeout(() => finish(null), 1500);
  try {
    reg = client()?.Apps?.RegisterForAppDetails?.(Number(appid), (d) => {
      finish(typeof d?.strLaunchOptions === "string" ? d.strLaunchOptions : null);
    }) ?? null;
    // the first answer can come before RegisterForAppDetails returns: unregister now
    if (done) { try { reg?.unregister(); } catch { /* already gone */ } }
    if (!reg && !done) finish(null);
  } catch {
    finish(null);
  }
});

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

export type SetResult = { ok: boolean; error: string | null; text: string | null };

/**
 * Write the launch options (they apply at the game's next start), then read them back: SetAppLaunchOptions
 * answers nothing. text is what Steam has now (null when it could not be read back).
 */
export const setLaunchOptions = async (appid: string, text: string): Promise<SetResult> => {
  if (!isSteamApp(appid)) return { ok: false, error: "not a Steam game: paste the launch options by hand", text: null };
  try {
    const apps = client()?.Apps;
    if (!apps?.SetAppLaunchOptions) return { ok: false, error: "Steam has no SetAppLaunchOptions any more", text: null };
    apps.SetAppLaunchOptions(Number(appid), text);
  } catch (e) {
    return { ok: false, error: `Steam refused the launch options: ${e instanceof Error ? e.message : String(e)}`, text: null };
  }
  let back: string | null = null;
  for (const wait of [200, 600]) {
    await sleep(wait);
    back = await getLaunchOptions(appid);
    if (back === text) return { ok: true, error: null, text: back };
  }
  if (back === null) return { ok: true, error: null, text: null };   // written; Steam did not answer the re-read
  return { ok: false, error: `Steam kept different launch options: ${back || "(empty)"}`, text: back };
};
