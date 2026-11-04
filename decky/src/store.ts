// Answers shared by the quick panel and the full page. One request per key at a time, every component using a key
// re-renders when it lands, and a job that ends reloads everything (it may have changed any of it). Nothing here
// polls oxp3-tool: the backend runs one oxp3-tool at a time, so only job_status is polled, and only while a job runs
// and someone is looking at it.
import { useEffect, useReducer, useRef } from "react";

import { job_status } from "./backend";
import type { Job } from "./types";

type Entry = { data: unknown; inflight: Promise<unknown> | null; subs: Set<() => void>;
               loader: (() => Promise<unknown>) | null; stale: boolean };
const entries = new Map<string, Entry>();

const entry = (key: string): Entry => {
  let e = entries.get(key);
  if (!e) { e = { data: undefined, inflight: null, subs: new Set(), loader: null, stale: false }; entries.set(key, e); }
  return e;
};
const notify = (subs: Set<() => void>) => subs.forEach((f) => { try { f(); } catch { /* a dead subscriber */ } });

export const load = <T>(key: string, loader: () => Promise<T>): Promise<T> => {
  const e = entry(key);
  e.loader = loader;
  if (e.inflight) return e.inflight as Promise<T>;
  const p: Promise<T> = loader()
    .then((d) => { e.data = d; e.stale = false; return d; })
    .catch(() => e.data as T)
    .finally(() => { if (e.inflight === p) e.inflight = null; notify(e.subs); });
  e.inflight = p;
  notify(e.subs);
  return p;
};

export const peek = <T>(key: string): T | undefined => entries.get(key)?.data as T | undefined;

/**
 * The cached answer for key, loaded on first use. fresh: reload on every mount (the cached one shows meanwhile).
 * skip: do not load now (e.g. oxp3-tool status while a job holds oxp3-tool's lock); it loads once skip clears.
 */
export const useResource = <T>(key: string | null, loader: () => Promise<T>, opts: { fresh?: boolean; skip?: boolean } = {}) => {
  const [, bump] = useReducer((n: number) => n + 1, 0);
  const latest = useRef(loader);
  latest.current = loader;
  const call = () => latest.current();
  useEffect(() => {
    if (!key) return undefined;
    const e = entry(key);
    e.subs.add(bump);
    if (!opts.skip && !e.inflight && (opts.fresh || e.data === undefined || e.stale)) load(key, call);
    return () => { e.subs.delete(bump); };
  }, [key, !!opts.skip]);
  const e = key ? entries.get(key) : undefined;
  return {
    data: e?.data as T | undefined,
    loading: !!e?.inflight,
    reload: (): Promise<T | undefined> => (key ? load(key, call) : Promise.resolve(undefined)),
  };
};

/**
 * Mark answers out of date: the ones on screen reload now, the others on their next use. Keys starting with
 * "manual:" (online update checks) are left alone: they run only when the user taps.
 */
export const invalidate = (prefix = "") => {
  entries.forEach((e, key) => {
    if (!key.startsWith(prefix) || (key.startsWith("manual:") && !prefix.startsWith("manual:"))) return;
    if (e.subs.size && e.loader) load(key, e.loader as () => Promise<unknown>);
    else e.stale = true;
  });
};

// ---- the job ------------------------------------------------------------------------------------------------------

let current: Job | null = null;
const jobSubs = new Set<() => void>();
const endSubs = new Set<(j: Job) => void>();
let pollers = 0;
let timer: ReturnType<typeof setTimeout> | null = null;
let failures = 0;
let stopped = false;

/** The newest job this frontend knows about (running or the last one that ended). */
export const currentJob = () => current;
export const jobRunning = () => current?.state === "running";

/** Take a job from any answer (overview, JobStart, job_status); the end of a running one reloads the caches. */
export const noteJob = (j: Job | null | undefined) => {
  if (!j || !j.id) return;
  const prev = current;
  if (prev && prev.id !== j.id && j.started < prev.started) return;   // an older job than the one we follow
  current = j;
  notify(jobSubs);
  if (prev && prev.id === j.id && prev.state === "running" && j.state !== "running") {
    invalidate();
    endSubs.forEach((f) => { try { f(j); } catch { /* a dead subscriber */ } });
  }
  schedule();
};

/** The overview says no job runs: if we still think ours does, ask how it ended (it ended while nobody looked). */
export const syncJob = (running: Job | null | undefined) => {
  if (running) { noteJob(running); return; }
  const mine = current;
  if (mine?.state === "running") job_status(mine.id).then((r) => { if (r.ok && r.job) noteJob(r.job); });
};

const tick = async () => {
  timer = null;
  const j = current;
  if (!j || j.state !== "running" || pollers <= 0) return;
  const r = await job_status(j.id);
  if (r.ok && r.job) { failures = 0; noteJob(r.job); } else { failures++; }
  schedule();
};

const schedule = () => {
  if (stopped || timer || pollers <= 0 || current?.state !== "running") return;
  timer = setTimeout(tick, failures ? 5000 : 1500);   // a backend that does not answer: ask less often
};

/** Re-render on every change of the current job. */
export const useCurrentJob = (): Job | null => {
  const [, bump] = useReducer((n: number) => n + 1, 0);
  useEffect(() => { jobSubs.add(bump); return () => { jobSubs.delete(bump); }; }, []);
  return current;
};

/** Poll job_status every 1.5 s while active (the panel is visible) and a job runs. */
export const useJobPolling = (active: boolean) => {
  useEffect(() => {
    if (!active) return undefined;
    pollers++;
    schedule();
    return () => { pollers--; };
  }, [active]);
};

/** fn(job) whenever a job this frontend followed ends. */
export const useJobEnded = (fn: (j: Job) => void) => {
  const latest = useRef(fn);
  latest.current = fn;
  useEffect(() => {
    const f = (j: Job) => latest.current(j);
    endSubs.add(f);
    return () => { endSubs.delete(f); };
  }, []);
};

// jobs shown next to the control that started them: the tab's own task section then leaves them out
const claimed = new Map<string, number>();
export const isClaimed = (id: string | null | undefined) => !!id && (claimed.get(id) ?? 0) > 0;
export const useClaim = (id: string | null | undefined) => {
  useEffect(() => {
    if (!id) return undefined;
    claimed.set(id, (claimed.get(id) ?? 0) + 1);
    notify(jobSubs);
    return () => {
      const n = (claimed.get(id) ?? 1) - 1;
      if (n > 0) claimed.set(id, n); else claimed.delete(id);
      notify(jobSubs);
    };
  }, [id]);
};

/** For onDismount: stop the poll timer. */
export const stopPolling = () => { stopped = true; if (timer) clearTimeout(timer); timer = null; };
