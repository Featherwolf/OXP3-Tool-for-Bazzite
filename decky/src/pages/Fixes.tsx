// Every oxp3-tool fix with its state, notes and when it takes effect; Apply / Revert each after a confirmation that
// shows oxp3-tool's dry-run plan. All of it comes from `oxp3-tool status --json` and `list --json`.
import { ButtonItem, PanelSection, PanelSectionRow, SliderField, ToggleField } from "@decky/ui";
import { FC, useState } from "react";

import { applyFixes, blockedBy, revertFixes, useAction } from "../actions";
import { get_overview, oxp3_catalog, oxp3_status } from "../backend";
import { explain, Note, Problem, Status } from "../components/Hint";
import { JobView, TaskSection } from "../components/JobView";
import { RebootRow } from "../components/Reboot";
import { openPage } from "../nav";
import { useCurrentJob, useResource } from "../store";
import type { ApplyOpts, CatalogRes, FixState, Overview, Oxp3Catalog, Oxp3CatalogFix, Oxp3Fix, StatusRes } from "../types";
import { oxp3Usable } from "./Quick";

const STATE_TEXT: Record<FixState, string> = {
  applied: "applied",
  damaged: "applied, but changed since (apply it again)",
  recommended: "recommended for this unit, not applied",
  optional: "optional, not applied",
  experimental: "experimental, not applied",
  "n/a": "not needed here",
  reverting: "reverted; a reboot finishes it",
  incomplete: "an earlier run did not finish (apply or revert it)",
  unknown: "unknown (oxp3-tool's state file could not be read)",
};

const PENDING_TEXT = { reboot: "Waits for a reboot.", "inputplumber-restart": "The controller map loads at the next reboot." };

/** The oxp3-tool data every oxp3-tool tab needs: overview, status (not while a job holds the lock) and catalog. */
export const useOxp3 = () => {
  const ov = useResource<Overview>("overview", get_overview);
  const job = useCurrentJob();
  const usable = oxp3Usable(ov.data?.oxp3);
  const st = useResource<StatusRes>("status", oxp3_status, { skip: !usable || job?.state === "running" });
  const cat = useResource<CatalogRes>("catalog", oxp3_catalog, { skip: !usable });
  return { ov, st, cat, usable, running: job?.state === "running",
           status: st.data?.ok ? st.data.status : null, catalog: cat.data?.ok ? cat.data.catalog : null };
};

/** When oxp3-tool is missing or too old, the tabs only point to the install button. */
export const NeedsOxp3: FC<{ ov: Overview | undefined; err?: string | null }> = ({ ov, err }) => (
  <PanelSection title="oxp3-tool">
    <PanelSectionRow>
      {!ov ? <Status ok={null} label="Reading…" />
        : !ov.ok ? <Problem what="Overview" error={ov.error} />
        : <Status ok={false} label={ov.oxp3.installed ? `oxp3-tool ${ov.oxp3.version ?? "?"} is too old` : "oxp3-tool is not installed"}
            detail={`Install or update it (${ov.oxp3.min_version} or newer) in the Quick Access panel or under Updates & logs.`} />}
      <Problem what="oxp3-tool" error={err} />
      {ov?.ok && <ButtonItem layout="below" onClick={() => openPage("updates")}>Updates & logs</ButtonItem>}
    </PanelSectionRow>
  </PanelSection>
);

const FixRow: FC<{ f: Oxp3Fix; c: Oxp3CatalogFix | undefined; cat: Oxp3Catalog | null; canChange: boolean; running: boolean }> =
  ({ f, c, cat, canChange, running }) => {
    const act = useAction();
    const [minRefresh, setMinRefresh] = useState<number | null>(null);
    const [anySsd, setAnySsd] = useState(false);
    const appliedLike = f.state === "applied" || f.state === "damaged";
    const canApply = canChange && f.state !== "n/a" && f.state !== "unknown" && f.state !== "reverting";
    const canRevert = canChange && (appliedLike || f.state === "incomplete" || (f.state === "reverting" && f.notes.some((n) => /again/.test(n))));
    const opts: ApplyOpts = {};
    if (f.id === "oled-profile" && minRefresh !== null) opts.min_refresh = minRefresh;
    if (f.id === "nvme-resume" && anySsd) opts.any_ssd = true;
    const disabled = !!act.busy || running;
    const details = c?.details ? c.details.replace(/\n/g, " ") : f.summary;
    return (
      <>
        <PanelSectionRow>
          <Status ok={f.state === "applied" ? true : f.state === "damaged" || f.state === "incomplete" ? false : null}
            label={f.title}
            detail={`${STATE_TEXT[f.state] ?? f.state}${f.applied_at ? ` (${f.applied_at})` : ""}. ${f.pending ? PENDING_TEXT[f.pending] + " " : ""}${f.summary}.`} />
          {!!f.notes.length && <Note>{f.notes.map((n, i) => <div key={i}>{n}</div>)}</Note>}
          {f.id === "oled-profile" && cat && canApply && (
            <SliderField label="Lowest refresh rate" value={minRefresh ?? Number(f.options?.min_refresh ?? cat.min_refresh.default ?? cat.min_refresh.max)}
              min={cat.min_refresh.min} max={cat.min_refresh.max} step={1} showValue valueSuffix=" Hz" disabled={disabled}
              onChange={setMinRefresh}
              description={explain(`The lowest refresh rate the display profile offers Game Mode (default ${cat.min_refresh.default ?? cat.min_refresh.max} Hz; below that is untested).`,
                "Used when you tap Apply below; takes effect the next time Game Mode starts.")} />
          )}
          {f.id === "nvme-resume" && canApply && !appliedLike && f.state !== "recommended" && (
            <ToggleField label="Apply without signs of the problem" checked={anySsd} disabled={disabled} onChange={setAnySsd}
              description={explain("oxp3-tool applies this only with the Predator GM7 SSD or a logged resume failure; this applies it anyway (--any-ssd).",
                "Costs a little battery while asleep; takes effect after a reboot.")} />
          )}
          {f.id === "buttons" && (
            <ButtonItem layout="below" onClick={() => openPage("buttons")}
              description="Choose what each key does; Apply there applies this fix with your layout.">Button layout</ButtonItem>
          )}
          {canApply && (
            <ButtonItem layout="below" disabled={disabled}
              onClick={() => act.run("apply", async () => blockedBy() ?? applyFixes([f], opts, `${appliedLike ? "Re-apply" : "Apply"}: ${f.title}`))}
              description={explain(details, `Takes effect ${f.takes_effect}. You see oxp3-tool's plan first.`)}>
              {act.busy === "apply" ? "Reading the plan…" : appliedLike ? "Apply again" : "Apply"}
            </ButtonItem>
          )}
          {canRevert && (
            <ButtonItem layout="below" disabled={disabled}
              onClick={() => act.run("revert", async () => blockedBy() ?? revertFixes([f]))}
              description={explain("Removes what this fix installed and puts back what it replaced.", `Undone ${f.takes_effect}. Asks first.`)}>
              {act.busy === "revert" ? "Working…" : "Revert"}
            </ButtonItem>
          )}
          <Problem what={f.title} error={act.error} />
        </PanelSectionRow>
        {act.job && <JobView job={act.job} lines={6} />}
      </>
    );
  };

export const FixesTab: FC = () => {
  const { ov, st, usable, running, status, catalog } = useOxp3();
  const act = useAction();
  if (!usable) return <NeedsOxp3 ov={ov.data} />;
  const byId = new Map((catalog?.fixes ?? []).map((c) => [c.id, c]));
  const group = (g: string) => (status?.fixes ?? []).filter((f) => f.group === g);
  return (
    <>
      <TaskSection />
      <PanelSection title="oxp3-tool status">
        <PanelSectionRow>
          {!status && st.loading && <Status ok={null} label="Reading oxp3-tool status…" detail="It checks the controller, the display and the boot entries: a few seconds." />}
          {!status && !st.loading && running && <Status ok={null} label="The status is read when the running task has finished." />}
          <Problem what="oxp3-tool status" error={st.data && !st.data.ok ? st.data.error : null} />
          {status && !status.can_change && (
            <Status ok={false} label="Nothing can be changed here" detail="oxp3-tool changes only a ONEXPLAYER 3 running Bazzite or CachyOS." />
          )}
          {status?.warnings.map((w, i) => <Status key={i} ok={false} label="Warning" detail={w} />)}
          {status?.notes.map((n, i) => <Note key={i}>{n}</Note>)}
          <ButtonItem layout="below" disabled={!!act.busy || running || st.loading}
            onClick={() => act.run("refresh", async () => { const r = await st.reload(); return r && !r.ok ? r.error : null; })}
            description="Runs oxp3-tool status again (a few seconds). Changes nothing.">
            {st.loading ? "Reading…" : "Read the status again"}
          </ButtonItem>
          <Problem what="Refresh" error={act.error} />
        </PanelSectionRow>
        <RebootRow o={ov.data?.ok ? ov.data : null} st={status} />
      </PanelSection>
      {status && (
        <>
          <PanelSection title="Controller">
            {group("controller").map((f) => <FixRow key={f.id} f={f} c={byId.get(f.id)} cat={catalog} canChange={status.can_change} running={running} />)}
          </PanelSection>
          <PanelSection title="Display and sleep">
            {group("display").map((f) => <FixRow key={f.id} f={f} c={byId.get(f.id)} cat={catalog} canChange={status.can_change} running={running} />)}
            {status.refresh && (
              <PanelSectionRow>
                <Note label="Display refresh">{status.refresh.summary.map((l, i) => <div key={i}>{l}</div>)}</Note>
                {status.refresh.advice.map((a, i) => <Note key={i} label={i === 0 ? "Advice" : undefined}>{a}</Note>)}
              </PanelSectionRow>
            )}
          </PanelSection>
        </>
      )}
    </>
  );
};
