// A background task (oxp3-tool apply, xess install, ...): its title, state and newest log lines, followed while it runs.
import { useQuickAccessVisible as apiQuickAccessVisible } from "@decky/api";
import { Field, PanelSection, PanelSectionRow, useQuickAccessVisible as uiQuickAccessVisible } from "@decky/ui";
import { FC, useEffect, useRef } from "react";

import { isClaimed, noteJob, useClaim, useCurrentJob, useJobEnded, useJobPolling } from "../store";
import type { Job } from "../types";
import { when } from "../util";
import { jobNext } from "../xessoffer";
import { Code, Note, Problem, Status } from "./Hint";

// @decky/api has it only with loader API 2 (Decky 3.3); older loaders get @decky/ui's copy. Chosen once, so the
// same hook runs on every render.
const useQuickAccessVisible: () => boolean =
  typeof apiQuickAccessVisible === "function" ? apiQuickAccessVisible
    : typeof uiQuickAccessVisible === "function" ? uiQuickAccessVisible : () => true;

const STATE = { running: "running…", done: "done", failed: "failed" };

/** A job worth showing without being asked: running, or ended in the last 15 minutes. */
export const recent = (j: Job | null | undefined): boolean =>
  !!j && (j.state === "running" || !j.ended || Date.now() / 1000 - j.ended < 15 * 60);

const texts = (v: unknown): string[] => (Array.isArray(v) ? v.filter((m): m is string => typeof m === "string") : []);

/**
 * What the result says beyond the log: oxp3-tool's outcome (a failed post step is only a warning there), the xess
 * jobs' warnings and notes, diagnose's findings (DESIGN.md "Job results").
 */
const resultLines = (job: Job): string[] => {
  const r = job.result;
  if (!r || typeof r !== "object") return [];
  const o = r.outcome && typeof r.outcome === "object" ? r.outcome : {};
  return [...texts(o.errors), ...texts(o.messages), ...texts(r.warnings), ...texts(r.findings), ...texts(r.notes)];
};

/**
 * Shows `job` (or, without one, the newest job the plugin knows about). Polls job_status every 1.5 s while the job
 * runs and, in the quick panel, while the panel is visible; onDone(job) runs once, when this job ends. The last 6
 * log lines in the quick panel, all of them on the full page (lines overrides both).
 */
export const JobView: FC<{ job?: Job | null; quick?: boolean; lines?: number; onDone?: (j: Job) => void; hideDone?: boolean }> =
  ({ job, quick, lines: maxLines, onDone, hideDone }) => {
    const visible = useQuickAccessVisible();
    const cur = useCurrentJob();
    useEffect(() => { if (job) noteJob(job); }, [job?.id]);
    useClaim(job?.id);
    // the newest copy of `job` the store has had (it moves on to the next job once this one has ended)
    const seen = useRef<Job | null>(null);
    if (!job || cur?.id === job.id) seen.current = cur;
    else if (seen.current?.id !== job.id) seen.current = job;
    const shown = seen.current;
    useJobPolling(!!shown && shown.state === "running" && (!quick || visible));
    const called = useRef<Set<string>>(new Set());
    useJobEnded((j) => {
      if (!onDone || called.current.has(j.id) || (shown && j.id !== shown.id)) return;
      called.current.add(j.id);
      onDone(j);
    });
    if (!shown || (hideDone && shown.state !== "running") || (!job && !recent(shown))) return null;
    const lines = shown.log ?? [];
    const keep = maxLines ?? (quick ? 6 : 0);
    const tail = keep ? lines.slice(-keep) : lines;
    const extra = shown.state === "running" ? [] : resultLines(shown);
    const reboot = shown.state === "done" && (shown.result?.outcome?.reboot_needed || shown.result?.reboot_needed === true);
    const next = shown.state === "done" ? jobNext(shown.kind, shown.result) : null;
    const path = shown.state === "done" && typeof shown.result?.path === "string" ? shown.result.path as string : null;
    const time = shown.state === "running" ? `since ${when(shown.started)}` : when(shown.ended ?? shown.started);
    return (
      <PanelSectionRow>
        <Status ok={shown.state === "running" ? null : shown.state === "done"}
          label={`${shown.title}: ${STATE[shown.state] ?? shown.state}`}
          detail={`${time}${shown.state === "running" ? " · keep the device on; the task goes on if you close this" : ""}`} />
        {!!tail.length && (
          <Field focusable bottomSeparator="none" description={
            <Code>{(lines.length > tail.length ? "…\n" : "") + tail.join("\n")}</Code>} />
        )}
        {!!extra.length && !quick && <Field focusable bottomSeparator="none" description={<Code>{extra.join("\n")}</Code>} />}
        {reboot && <Status ok={null} label="Reboot to finish" detail="The change takes effect after a reboot." />}
        {next && <Status ok={null} label="Next" detail={next} />}
        {path && shown.kind === "oxp3-diagnose" && <Note label="Saved">{path}</Note>}
        {path && shown.kind === "oxp3-install" && <Note label="Installed">{`oxp3-tool ${shown.result?.version ?? ""} as ${path}`}</Note>}
        {shown.state === "failed" && <Problem what={shown.title} error={shown.error ?? "failed (see the log above)"} />}
      </PanelSectionRow>
    );
  };

/** The running (or just finished) task at the top of a full-page tab, so its progress shows wherever it started. */
export const TaskSection: FC<{ onDone?: (j: Job) => void }> = ({ onDone }) => {
  const cur = useCurrentJob();
  if (!recent(cur) || isClaimed(cur?.id)) return null;
  return <PanelSection title="Task"><JobView onDone={onDone} /></PanelSection>;
};
