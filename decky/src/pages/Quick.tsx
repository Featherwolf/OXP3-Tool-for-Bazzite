// Adapted from ClawDeck (MIT, Copyright (c) 2026 IHulkSmashI and ClawDeck contributors).
// Based on ClawDeck's QuickPanel (src/pages.tsx): the Quick Access panel.
// Short: the state of the device, what waits for the user, and the running game's XeSS options. Everything else is
// on the full page ("More").
import { ButtonItem, Field, PanelSection, PanelSectionRow } from "@decky/ui";
import { FC, useEffect } from "react";

import { applyFixes, blockedBy, installOxp3, installXess, useAction } from "../actions";
import { get_overview, oxp3_status, xess_check_update, xess_state } from "../backend";
import { GameControls } from "../components/GameControls";
import { explain, Note, Problem, Status } from "../components/Hint";
import { JobView } from "../components/JobView";
import { RebootRow } from "../components/Reboot";
import { openPage } from "../nav";
import { useRunningApp } from "../steam";
import { syncJob, useCurrentJob, useResource } from "../store";
import type { Overview, Oxp3Install, Oxp3Status, StatusRes, XessStateRes, XessSummary, XessUpdateCheck } from "../types";
import { atLeast, cmpVersion, listText, platformName } from "../util";
import { checkText, offerLabel, repairText, xessOffer } from "../xessoffer";

/** oxp3-tool is there and new enough for --json; otherwise nothing but the install button works. */
export const oxp3Usable = (i: Oxp3Install | null | undefined) =>
  !!i?.installed && i.json_ok && atLeast(i.version, i.min_version);

/** What the oxp3-tool section offers: install, update to the plugin's copy, or nothing. */
export const oxp3Offer = (i: Oxp3Install | null | undefined): { source: "bundled" | "online"; version: string | null; update: boolean } | null => {
  if (!i) return null;
  const source = i.bundled_version ? "bundled" : "online";
  if (!i.installed) return { source, version: i.bundled_version, update: false };
  if (!oxp3Usable(i)) return { source, version: i.bundled_version, update: true };
  if (i.bundled_version && i.version && cmpVersion(i.bundled_version, i.version) > 0) {
    return { source: "bundled", version: i.bundled_version, update: true };
  }
  return null;
};

export const xessLine = (x: XessSummary | null | undefined): { ok: boolean | null; label: string; detail: string } => {
  if (!x) return { ok: null, label: "XeSS XMX: …", detail: "" };
  if (!x.installed) return { ok: false, label: "XeSS XMX: not installed", detail: "XeSS in games runs on DP4a, not on the XMX cores." };
  const v = x.version ?? "";
  if (x.relogin_needed) return { ok: null, label: `XeSS XMX ${v}: log out and in`, detail: "Installed, but Steam still runs with the old driver setting: log out and back in (or reboot)." };
  if (x.status === "failed") return { ok: false, label: `XeSS XMX ${v}: driver failed`, detail: x.line ?? "The session check fell back to the system driver: games use DP4a." };
  if (x.status === "unknown") return { ok: null, label: `XeSS XMX ${v}: not checked yet`, detail: "The driver check runs at the next log-in." };
  return { ok: x.live, label: `XeSS XMX ${v}: ${x.live ? "on" : "driver ok, not live"}`,
    detail: x.live ? (x.line ?? "The patched driver is the session's Vulkan driver.") : "The session does not use the patched driver yet: log out and back in." };
};

const StatusSection: FC<{ o: Overview | null; err: string | null; st: Oxp3Status | null }> = ({ o, err, st }) => {
  const os = st ? `${st.os.name ?? platformName(st.os.platform)}${st.os.kernel ? `, kernel ${st.os.kernel}` : ""}` : platformName(o?.platform);
  return (
    <PanelSection title="Status">
      <PanelSectionRow>
        {o && (
          <Status ok={o.device.supported} label={o.device.supported ? (o.device.product ?? "ONEXPLAYER 3") : `${o.device.product ?? "Unknown device"}: not a ONEXPLAYER 3`}
            detail={`${os}${o.platform ? "" : " · oxp3-tool supports Bazzite and CachyOS"}`} />
        )}
        {!o && !err && <Status ok={null} label="Reading the state…" />}
        <Problem what="Status" error={err} />
      </PanelSectionRow>
      <RebootRow o={o} st={st} />
      <JobView quick />
    </PanelSection>
  );
};

const Oxp3Section: FC<{ o: Overview; st: StatusRes | undefined; loading: boolean }> = ({ o, st, loading }) => {
  const act = useAction();
  const inst = useAction();     // its own, so its error shows under it also while oxp3-tool is missing (FE-2)
  const job = useCurrentJob();
  const running = job?.state === "running";
  const i = o.oxp3;
  const offer = oxp3Offer(i);
  const usable = oxp3Usable(i);
  const status = st?.ok ? st.status : null;
  const recommended = (status?.fixes ?? []).filter((f) => f.state === "recommended");
  const attention = (status?.fixes ?? []).filter((f) => f.state === "damaged" || f.state === "incomplete");
  const applied = (status?.fixes ?? []).filter((f) => f.state === "applied").length;
  return (
    <PanelSection title="oxp3-tool">
      <PanelSectionRow>
        <Status ok={usable ? true : false}
          label={!i.installed ? "oxp3-tool is not installed" : usable ? `oxp3-tool ${i.version}` : `oxp3-tool ${i.version ?? "?"}: too old`}
          detail={!i.installed ? "The controller, sleep and display fixes for the OXP3 come from it."
            : !usable ? `OXP3 Deck needs ${i.min_version} or newer.`
            : status ? `${applied} fix${applied === 1 ? "" : "es"} applied${status.can_change ? "" : " · this system is not supported, so nothing can be changed"}` : loading ? "Reading its status…" : undefined} />
        {offer && (
          <ButtonItem layout="below" disabled={!!inst.busy || !!act.busy || running}
            onClick={() => inst.run("oxp3-tool", async () => blockedBy() ?? installOxp3(offer.source, offer.version, offer.update))}
            description={explain(`${offer.update ? "Updates" : "Installs"} /usr/local/bin/oxp3-tool${offer.version ? ` to ${offer.version}` : ""} ${offer.source === "bundled" ? "from the copy in this plugin" : "from GitHub"}.`,
              "Takes a few seconds; asks first.")}>
            {inst.busy ? "Working…" : `${offer.update ? "Update" : "Install"} oxp3-tool`}
          </ButtonItem>
        )}
        <Problem what={offer?.update ? "Update oxp3-tool" : "Install oxp3-tool"} error={inst.busy ? null : inst.error} />
        {usable && st && !st.ok && <Problem what="oxp3-tool status" error={st.error} />}
      </PanelSectionRow>
      {usable && status && (
        <PanelSectionRow>
          {status.warnings.map((w, n) => <Status key={n} ok={false} label="Warning" detail={w} />)}
          {recommended.map((f) => (
            <Field key={f.id} focusable bottomSeparator="none" label={f.title}
              description={explain(f.summary, `Takes effect ${f.takes_effect}.`)} />
          ))}
          {recommended.length > 0 && status.can_change && (
            <ButtonItem layout="below" disabled={!!act.busy || !!inst.busy || running}
              onClick={() => act.run("apply", async () => blockedBy() ?? applyFixes(recommended, {}, "Apply the recommended fixes"))}
              description={explain(`Applies ${recommended.length === 1 ? "this fix" : `these ${recommended.length} fixes`}. You see oxp3-tool's plan first.`,
                recommended.some((f) => /reboot/i.test(f.takes_effect)) ? "Some take effect after a reboot." : undefined)}>
              {act.busy === "apply" ? "Reading the plan…" : `Apply recommended (${recommended.length})`}
            </ButtonItem>
          )}
          {recommended.length === 0 && attention.length === 0 && <Status ok label="All recommended fixes are applied" />}
          {attention.length > 0 && (
            <ButtonItem layout="below" onClick={() => openPage("fixes")}
              description={`${listText(attention.map((f) => f.title))}: ${attention.length === 1 ? "needs" : "need"} a look.`}>
              Fixes needing a look ({attention.length})
            </ButtonItem>
          )}
          <Problem what="Apply" error={act.busy ? null : act.error} />
        </PanelSectionRow>
      )}
    </PanelSection>
  );
};

const XessSection: FC<{ o: Overview }> = ({ o }) => {
  const act = useAction();
  const job = useCurrentJob();
  const xs = useResource<XessStateRes>("xess", xess_state);
  const chk = useResource<XessUpdateCheck>("manual:xess-check", xess_check_update, { skip: true });
  const line = xessLine(o.xess);
  const x = xs.data?.xess ?? null;
  // the button and its confirmation both go by the overview's summary (FE-4); only a newer tag is an update (FE-1)
  const offer = xessOffer(o.xess.installed, o.xess.version, x, chk.data);
  const busy = !!act.busy || job?.state === "running";
  return (
    <PanelSection title="XeSS XMX">
      <PanelSectionRow>
        <Status ok={line.ok} label={line.label} detail={line.detail} />
        {offer.kind ? (
          <ButtonItem layout="below" disabled={busy}
            onClick={() => act.run("xess", async () => blockedBy() ?? installXess(offer, x, o.user))}
            description={explain(offer.kind === "update" ? `Updates xess-xmx-linux ${o.xess.version ?? ""} to ${offer.latest}.`
              : offer.kind === "repair" ? repairText(offer)
              : "Installs xess-xmx-linux (XeSS super resolution and frame generation on the XMX cores) from GitHub.",
              offer.kind === "repair" ? "Asks first." : "Then log out and back in; asks first.")}>
            {act.busy === "xess" ? "Working…" : offerLabel(offer)}
          </ButtonItem>
        ) : (
          <ButtonItem layout="below" disabled={busy || chk.loading}
            onClick={() => act.run("check", async () => { const r = await chk.reload(); return r && !r.ok ? r.error : null; })}
            description={explain("Asks GitHub for the newest xess-xmx-linux release. Changes nothing.",
              chk.data?.ok ? `${checkText(chk.data, o.xess.installed, o.xess.version)}.` : undefined)}>
            {chk.loading ? "Checking…" : "Check for an update"}
          </ButtonItem>
        )}
        <Problem what="XeSS" error={act.error} />
      </PanelSectionRow>
    </PanelSection>
  );
};

export const QuickPanel: FC = () => {
  const ov = useResource<Overview>("overview", get_overview, { fresh: true });
  const job = useCurrentJob();
  const o = ov.data?.ok ? ov.data : null;
  useEffect(() => { if (ov.data?.ok) syncJob(ov.data.job); }, [ov.data]);
  // oxp3-tool status waits for oxp3-tool's lock, which a running job holds: read it once the job has ended
  const st = useResource<StatusRes>("status", oxp3_status, { skip: !oxp3Usable(o?.oxp3) || job?.state === "running" });
  const app = useRunningApp();
  const status = st.data?.ok ? st.data.status : null;
  return (
    <>
      <StatusSection o={o} err={ov.data && !ov.data.ok ? ov.data.error : null} st={status} />
      {o && <Oxp3Section o={o} st={st.data} loading={st.loading} />}
      {o && <XessSection o={o} />}
      {app && (
        <PanelSection title="This game">
          <PanelSectionRow><Note label={app.name}>XeSS options for this game, written into its Steam launch options.</Note></PanelSectionRow>
          <GameControls app={app} quick running />
        </PanelSection>
      )}
      <PanelSection>
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={() => openPage(app ? "games" : "fixes")}
            description="Every fix, the button layout, XeSS details, games, the kernel and the logs.">More</ButtonItem>
        </PanelSectionRow>
      </PanelSection>
    </>
  );
};
