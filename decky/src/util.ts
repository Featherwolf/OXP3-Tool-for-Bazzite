// Small helpers shared by the pages: versions, times, toasts and per-device remembered choices.
import { toaster } from "@decky/api";

export const toast = (body: string, title = "OXP3 Deck") => {
  try { toaster.toast({ title, body }); } catch { /* the toaster is gone while the plugin unloads */ }
};

const parts = (v: string) => v.replace(/^v/, "").split(/[.+-]/).map((x) => (/^\d+$/.test(x) ? Number(x) : -1));

/** Compare dotted versions ("0.4.0" < "0.10.1"); a missing number counts as 0. */
export const cmpVersion = (a: string, b: string): number => {
  const pa = parts(a), pb = parts(b);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const d = (pa[i] ?? 0) - (pb[i] ?? 0);
    if (d) return d < 0 ? -1 : 1;
  }
  return 0;
};

export const atLeast = (v: string | null | undefined, min: string) => !!v && cmpVersion(v, min) >= 0;

/** A Unix time (seconds) as "18:34" today, else "4 Nov 18:34". */
export const when = (t: number | null | undefined): string => {
  if (!t) return "";
  const d = new Date(t * 1000);
  const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return d.toDateString() === new Date().toDateString() ? time
    : `${d.toLocaleDateString([], { day: "numeric", month: "short" })} ${time}`;
};

export const platformName = (p: string | null | undefined) =>
  p === "cachyos" ? "CachyOS" : p === "bazzite" ? "Bazzite" : "an unsupported OS";

/** Remembered on this device only (Steam's browser storage); a private or broken storage just forgets. */
export const remember = {
  get: (key: string): string | null => { try { return localStorage.getItem(`oxp3deck.${key}`); } catch { return null; } },
  set: (key: string, value: string) => { try { localStorage.setItem(`oxp3deck.${key}`, value); } catch { /* forgets */ } },
};

/** Text for a list in a description: "a, b and c". */
export const listText = (xs: string[]) => (xs.length < 2 ? xs.join("") : `${xs.slice(0, -1).join(", ")} and ${xs[xs.length - 1]}`);
