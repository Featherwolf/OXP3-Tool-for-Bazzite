// The flows behind the buttons that change the system: each asks first (with oxp3-tool's dry-run plan where there
// is one), then starts the backend job. They answer an error text for the red row under the button, or null (done,
// started, or cancelled).
import { useState } from "react";

import { oxp3_apply, oxp3_install, oxp3_plan, oxp3_revert, reboot, xess_install, xess_uninstall } from "./backend";
import { confirm } from "./components/confirm";
import { currentJob, noteJob } from "./store";
import type { ApplyOpts, Job, JobStart, XessState } from "./types";
import { toast } from "./util";

/** One control's busy state ("which action runs"), the last error, and the job its last action started. */
export const useAction = () => {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const run = async (name: string, fn: () => Promise<string | null | void>) => {
    if (busy) return;
    setBusy(name);
    setError(null);
    const before = currentJob()?.id;
    try {
      setError((await fn()) || null);
    } catch (e) {
      setError(`${name}: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(null);
      const now = currentJob();
      if (now && now.id !== before) setJob(now);
    }
  };
  return { busy, error, job, run, setError };
};

/** Why a new task cannot start now (one task at a time), or null. */
export const blockedBy = (): string | null => {
  const j = currentJob();
  return j?.state === "running" ? `Wait until "${j.title}" has finished.` : null;
};

/** The job a JobStart answer started (now followed by the panel), or its error. */
export const started = (r: JobStart): string | null => {
  if (r.ok && r.job) { noteJob(r.job); return null; }
  return r.error || "the task did not start";
};

type FixLike = { id: string; title: string; takes_effect: string; level?: string; pending?: string | null };

/** Apply fixes once the user has seen what oxp3-tool would change (apply --dry-run). */
export const applyFixes = async (fixes: FixLike[], opts: ApplyOpts = {}, title = "Apply fixes"): Promise<string | null> => {
  const ids = fixes.map((f) => f.id);
  const plan = await oxp3_plan(ids, opts);
  if (!plan.ok) return plan.error || "oxp3-tool's dry run failed";
  if (!plan.plan?.length) { toast("Nothing to change"); return null; }
  const experimental = fixes.filter((f) => f.level === "experimental").map((f) => f.title);
  const ok = await confirm({
    title, ok: "Apply", lines: plan.plan, linesTitle: "oxp3-tool will:",
    text: (
      <>
        {fixes.map((f) => <div key={f.id}>{f.title}: takes effect {f.takes_effect}.</div>)}
        {ids.includes("buttons") && !opts.no_restart && (
          <div style={{ marginTop: 8 }}>The controller stops for a few seconds while InputPlumber restarts.</div>
        )}
        {experimental.length > 0 && (
          <div style={{ marginTop: 8 }}>Experimental: {experimental.join(", ")}. It may not help on every unit; Revert undoes it.</div>
        )}
      </>
    ),
  });
  if (!ok) return null;
  return started(await oxp3_apply(ids, opts));
};

/** Revert fixes: oxp3-tool removes what it installed and puts back the files it replaced. */
export const revertFixes = async (fixes: FixLike[]): Promise<string | null> => {
  const ok = await confirm({
    title: fixes.length === 1 ? `Revert ${fixes[0].title}?` : `Revert ${fixes.length} fixes?`, ok: "Revert", destructive: true,
    text: (
      <>
        <div>oxp3-tool removes the files it installed for this, puts back the ones it replaced and disables its services.</div>
        {fixes.map((f) => <div key={f.id} style={{ marginTop: 6 }}>{f.title}: undone {f.takes_effect}.</div>)}
      </>
    ),
  });
  if (!ok) return null;
  return started(await oxp3_revert(fixes.map((f) => f.id)));
};

export const rebootNow = async (why: string): Promise<string | null> => {
  const ok = await confirm({ title: "Reboot now?", text: why, ok: "Reboot" });
  if (!ok) return null;
  const r = await reboot();
  return r.ok ? null : r.error || "the reboot did not start";
};

export const installOxp3 = async (source: "bundled" | "online", version: string | null, update: boolean): Promise<string | null> => {
  const ok = await confirm({
    title: update ? "Update oxp3-tool?" : "Install oxp3-tool?", ok: update ? "Update" : "Install",
    text: (
      <>
        <div>Installs oxp3-tool {version ?? ""} {source === "bundled" ? "from this plugin" : "from GitHub (checked against its sha256)"} as
          /usr/local/bin/oxp3-tool.</div>
        <div style={{ marginTop: 8 }}>{update
          ? "Your fixes stay applied. Apply them again afterwards when the Fixes tab says they were installed by an older oxp3-tool, so their helpers are updated too."
          : "Nothing else changes until you apply a fix."}</div>
      </>
    ),
  });
  if (!ok) return null;
  return started(await oxp3_install(source));
};

export const installXess = async (x: XessState | null, user: string | null, latest: string | null): Promise<string | null> => {
  const update = !!x?.installed;
  const ok = await confirm({
    title: update ? "Update xess-xmx-linux?" : "Install xess-xmx-linux?", ok: update ? "Update" : "Install",
    text: (
      <>
        <div>Downloads xess-xmx-linux {latest ?? "(the newest release)"} from GitHub into a new folder and runs its install.sh
          as {user ?? "your user"} (it also fetches Intel's XeSS SDK and compiler: a few hundred MB).</div>
        {update && (
          <div style={{ marginTop: 8 }}>First the installed {x?.version ?? ""} is uninstalled with its own uninstall.sh
            {x && !x.managed && x.package_dir ? `; its folder ${x.package_dir} stays, for you to remove after the re-login (XeSS tab)` : ""}.
            Your default Mesa driver is set again afterwards.</div>
        )}
        <div style={{ marginTop: 8 }}>Close running games first. Afterwards log out and back in (or reboot) so Steam starts with the new
          driver. Each game's first start then compiles XeSS's kernels for a minute or two, once.</div>
      </>
    ),
  });
  if (!ok) return null;
  return started(await xess_install());
};

export const uninstallXess = async (x: XessState | null): Promise<string | null> => {
  const ok = await confirm({
    title: "Uninstall xess-xmx-linux?", ok: "Uninstall", destructive: true,
    text: (
      <>
        <div>Runs the package's uninstall.sh as your user: it removes the session driver setting, the Proton hooks and the shims in the
          game prefixes, and puts back what it replaced.</div>
        <div style={{ marginTop: 8 }}>The folder {x?.package_dir ?? ""} stays (remove it later). Log out and back in afterwards. Launch
          options with XESS, MESA or FSR then do nothing.</div>
      </>
    ),
  });
  if (!ok) return null;
  return started(await xess_uninstall());
};
