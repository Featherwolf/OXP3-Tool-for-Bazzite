// Versions and updates of oxp3-tool and xess-xmx-linux, the diagnose archive, and the last tasks with their logs.
import { ButtonItem, Field, PanelSection, PanelSectionRow } from "@decky/ui";
import { FC, useState } from "react";

import { blockedBy, installOxp3, installXess, started, useAction } from "../actions";
import { get_overview, job_list, job_status, oxp3_diagnose, oxp3_update_check, xess_check_update, xess_state } from "../backend";
import { Code, explain, Note, Problem, Status } from "../components/Hint";
import { JobView, TaskSection } from "../components/JobView";
import { useCurrentJob, useResource } from "../store";
import type { Job, JobListRes, Overview, Oxp3UpdateCheck, XessStateRes, XessUpdateCheck } from "../types";
import { when } from "../util";
import { oxp3Offer, oxp3Usable } from "./Quick";

const JobRow: FC<{ j: Job }> = ({ j }) => {
  const [log, setLog] = useState<string[] | null>(null);   // null: hidden
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const toggle = async () => {
    if (log) { setLog(null); return; }
    // job_list may send short logs: the job's own answer has all of it
    setBusy(true);
    const r = await job_status(j.id);
    setBusy(false);
    setErr(r.ok ? null : r.error);
    setLog((r.ok && r.job ? r.job.log : j.log) ?? []);
  };
  return (
    <PanelSectionRow>
      <Status ok={j.state === "running" ? null : j.state === "done"} label={`${j.title}: ${j.state}`}
        detail={`${when(j.started)}${j.ended ? ` to ${when(j.ended)}` : ""}${j.error ? ` · ${j.error}` : ""}`} />
      <ButtonItem layout="below" onClick={toggle} disabled={busy} description="Shows what the task printed. Changes nothing.">
        {busy ? "Reading…" : log ? "Hide the log" : "Show the log"}
      </ButtonItem>
      {log && <Field focusable bottomSeparator="none" description={<Code>{log.length ? log.join("\n") : "(no lines)"}</Code>} />}
      <Problem what="Log" error={err} />
    </PanelSectionRow>
  );
};

export const UpdatesTab: FC = () => {
  const ov = useResource<Overview>("overview", get_overview, { fresh: true });
  const xs = useResource<XessStateRes>("xess", xess_state);
  const oc = useResource<Oxp3UpdateCheck>("manual:oxp3-check", oxp3_update_check, { skip: true });
  const xc = useResource<XessUpdateCheck>("manual:xess-check", xess_check_update, { skip: true });
  const jl = useResource<JobListRes>("jobs", job_list, { fresh: true });
  const job = useCurrentJob();
  const running = job?.state === "running";
  const oxp = useAction();
  const xes = useAction();
  const diag = useAction();
  const o = ov.data?.ok ? ov.data : null;
  const i = o?.oxp3;
  const x = xs.data?.ok ? xs.data.xess : null;

  // a usable oxp3-tool updates only when the check found something; a missing or too old one always gets the offer
  const check = oc.data?.ok ? oc.data : null;
  const offline = oxp3Offer(i);
  const offer = check?.update ? { source: check.update, version: check.update === "online" ? check.online : check.bundled, update: !!i?.installed }
    : offline && (!oxp3Usable(i) || !check) ? offline : null;
  const xLatest = xc.data?.ok ? xc.data.latest : x?.latest ?? null;
  const xUpdate = !!x?.installed && !!xLatest && xLatest !== x.version && (xc.data?.ok ? xc.data.update_available : true);

  return (
    <>
      <TaskSection />
      <PanelSection title="oxp3-tool">
        <PanelSectionRow>
          {!o && <Problem what="Overview" error={ov.data && !ov.data.ok ? ov.data.error : null} />}
          {i && (
            <Status ok={oxp3Usable(i)} label={i.installed ? `oxp3-tool ${i.version ?? "?"}` : "oxp3-tool is not installed"}
              detail={`${i.path ?? "/usr/local/bin/oxp3-tool"} · in this plugin: ${i.bundled_version ?? "none"} · OXP3 Deck needs ${i.min_version} or newer${i.installed && !i.json_ok ? " · its --json output does not work" : ""}`} />
          )}
          <ButtonItem layout="below" disabled={oc.loading || running}
            onClick={() => oxp.run("check", async () => { const r = await oc.reload(); return r && !r.ok ? r.error : null; })}
            description={explain("Compares the installed oxp3-tool with the plugin's copy and with the newest on GitHub. Changes nothing.",
              check ? `Installed ${check.installed ?? "none"} · plugin ${check.bundled ?? "none"} · GitHub ${check.online ?? "?"}${check.online_commit ? ` (${check.online_commit.slice(0, 7)})` : ""}` : undefined)}>
            {oc.loading ? "Checking…" : "Check for an update"}
          </ButtonItem>
          {check && !check.update && <Status ok label="oxp3-tool is up to date" />}
          {offer && (
            <ButtonItem layout="below" disabled={running || !!oxp.busy}
              onClick={() => oxp.run("install", async () => blockedBy() ?? installOxp3(offer.source, offer.version, offer.update))}
              description={explain(`${offer.update ? "Updates" : "Installs"} /usr/local/bin/oxp3-tool${offer.version ? ` to ${offer.version}` : ""} ${offer.source === "bundled" ? "from this plugin" : "from GitHub"}.`,
                "Asks first.")}>
              {oxp.busy === "install" ? "Working…" : `${offer.update ? "Update" : "Install"} oxp3-tool${offer.version ? ` ${offer.version}` : ""}`}
            </ButtonItem>
          )}
          <Problem what="oxp3-tool" error={oxp.error} />
        </PanelSectionRow>
        {oxp.job && <JobView job={oxp.job} lines={6} />}
      </PanelSection>
      <PanelSection title="xess-xmx-linux">
        <PanelSectionRow>
          <Status ok={x ? x.installed : null} label={x?.installed ? `xess-xmx-linux ${x.version ?? "?"}` : x ? "xess-xmx-linux is not installed" : "xess-xmx-linux: …"}
            detail={x?.package_dir ?? undefined} />
          <ButtonItem layout="below" disabled={xc.loading || running}
            onClick={() => xes.run("check", async () => { const r = await xc.reload(); return r && !r.ok ? r.error : null; })}
            description={explain("Asks GitHub for the newest release. Changes nothing.",
              xc.data?.ok ? `${xc.data.update_available ? `${xc.data.latest} is available` : "Up to date"} (checked ${when(xc.data.checked_at)}).${xc.data.note ? ` ${xc.data.note}` : ""}` : undefined)}>
            {xc.loading ? "Checking…" : "Check for an update"}
          </ButtonItem>
          {(xUpdate || (x && !x.installed)) && (
            <ButtonItem layout="below" disabled={running || !!xes.busy}
              onClick={() => xes.run("install", async () => blockedBy() ?? installXess(x, o?.user ?? null, xLatest))}
              description={explain(x?.installed ? `Updates ${x.version ?? ""} to ${xLatest}.` : "Installs the newest release.", "Then log out and back in; asks first.")}>
              {xes.busy === "install" ? "Working…" : x?.installed ? `Update to ${xLatest}` : "Install XeSS XMX"}
            </ButtonItem>
          )}
          <Problem what="xess-xmx-linux" error={xes.error} />
        </PanelSectionRow>
        {xes.job && <JobView job={xes.job} lines={6} />}
      </PanelSection>
      <PanelSection title="Report a problem">
        <PanelSectionRow>
          <ButtonItem layout="below" disabled={running || !!diag.busy || !oxp3Usable(i)}
            onClick={() => diag.run("diagnose", async () => blockedBy() ?? started(await oxp3_diagnose()))}
            description={explain(`oxp3-tool diagnose: an archive in ${o?.home ?? "your home folder"} with the status, this and the previous boot's kernel logs, InputPlumber and gamescope logs. Serial numbers and MAC addresses are replaced; look through it before posting it.`,
              "Takes up to a minute; changes nothing.")}>
            {diag.busy ? "Starting…" : "Save a diagnose archive"}
          </ButtonItem>
          <Problem what="Diagnose" error={diag.error} />
          {o && <Note label="OXP3 Deck">{`Version ${o.plugin_version} · for ${o.user} (${o.home})`}</Note>}
        </PanelSectionRow>
        {diag.job && <JobView job={diag.job} lines={6} />}
      </PanelSection>
      <PanelSection title="Last tasks">
        <PanelSectionRow>
          <Problem what="Tasks" error={jl.data && !jl.data.ok ? jl.data.error : null} />
          {jl.data?.ok && !jl.data.jobs?.length && <Note>No tasks yet.</Note>}
        </PanelSectionRow>
        {((jl.data?.ok && jl.data.jobs) || []).map((j) => <JobRow key={j.id} j={j} />)}
      </PanelSection>
    </>
  );
};
