// xess-xmx-linux: version, the driver check, the default Mesa driver, install / update / uninstall, the system
// Protons' hook, missing tools and old package folders.
import { ButtonItem, DropdownItem, PanelSection, PanelSectionRow } from "@decky/ui";
import { FC, useMemo, useState } from "react";

import { blockedBy, installXess, uninstallXess, useAction } from "../actions";
import { get_overview, xess_check_update, xess_remove_folder, xess_set_driver, xess_shim_system_protons, xess_state } from "../backend";
import { confirm } from "../components/confirm";
import { Code, explain, Note, Problem, Status } from "../components/Hint";
import { JobView, TaskSection } from "../components/JobView";
import { driverPrefix } from "../launchopts";
import { invalidate, useCurrentJob, useResource } from "../store";
import type { Overview, XessDriver, XessStateRes, XessUpdateCheck } from "../types";
import { listText, toast, when } from "../util";
import { xessLine } from "./Quick";

/** What switch-driver.sh is given: the major.minor prefix when it names one driver, else the full version. */
const prefixFor = (d: XessDriver, all: XessDriver[]) => {
  const p = driverPrefix(d.version);
  return all.filter((x) => driverPrefix(x.version) === p).length > 1 ? d.version : p;
};

const OldFolder: FC<{ path: string; disabled: boolean }> = ({ path, disabled }) => {
  const act = useAction();
  return (
    <>
      <ButtonItem layout="below" disabled={disabled || !!act.busy}
        onClick={() => act.run("remove", async () => {
          const ok = await confirm({ title: "Remove this folder?", ok: "Remove", destructive: true,
            text: <>Deletes <Code>{path}</Code>, an earlier xess-xmx-linux package nothing uses any more (the plugin checks that first).</> });
          if (!ok) return null;
          const r = await xess_remove_folder(path);
          if (r.ok) { toast("Old folder removed"); invalidate("xess"); }
          return r.ok ? null : r.error;
        })}
        description={explain(<Code>{path}</Code>, "Only after a re-login: until then Steam may still use it.")}>
        {act.busy ? "Removing…" : "Remove old folder"}
      </ButtonItem>
      <Problem what="Remove" error={act.error} />
    </>
  );
};

export const XessTab: FC = () => {
  const ov = useResource<Overview>("overview", get_overview);
  const xs = useResource<XessStateRes>("xess", xess_state, { fresh: true });
  const chk = useResource<XessUpdateCheck>("manual:xess-check", xess_check_update, { skip: true });
  const job = useCurrentJob();
  const running = job?.state === "running";
  const inst = useAction();
  const drv = useAction();
  const shim = useAction();
  const [drvRev, setDrvRev] = useState(0);   // remounts the dropdown when the pick did not happen
  const [shimNotes, setShimNotes] = useState<string[]>([]);
  const x = xs.data?.ok ? xs.data.xess : null;
  const drivers = x?.drivers ?? [];
  const current = drivers.find((d) => d.default) ?? null;
  const driverOpts = useMemo(() => drivers.map((d) => ({ data: d.version, label: `Mesa ${d.version}` })), [drivers.map((d) => d.version).join(",")]);
  const latest = chk.data?.ok ? chk.data.latest : x?.latest ?? null;
  const update = !!x?.installed && !!latest && latest !== x.version && (chk.data?.ok ? chk.data.update_available : true);
  const user = ov.data?.ok ? ov.data.user : null;
  // the state read for this tab is newer than the overview's summary
  const line = xessLine(x ? { installed: x.installed, version: x.version, status: x.status.state, line: x.status.line,
    live: x.live, relogin_needed: x.relogin_needed } : ov.data?.ok ? ov.data.xess : null);
  const unshimmed = (x?.system_protons ?? []).filter((p) => !p.shimmed);
  const busy = running || !!inst.busy;

  return (
    <>
      <TaskSection />
      <PanelSection title="xess-xmx-linux">
        <PanelSectionRow>
          {!xs.data && <Status ok={null} label="Reading…" />}
          <Problem what="XeSS state" error={xs.data && !xs.data.ok ? xs.data.error : null} />
          <Status ok={line.ok} label={line.label} detail={line.detail} />
          {x && !x.installed && x.package_dir && (
            <Note label="Not set up">{`${x.package_dir} is the package OXP3 Deck installed last, but its install.sh files are gone (uninstalled). Install sets it up again.`}</Note>
          )}
          {x?.installed && (
            <Note label="Package">
              <Code>{x.package_dir ?? "?"}</Code>
              {x.managed ? " (installed by OXP3 Deck)" : " (installed by hand; Update moves it to a folder OXP3 Deck manages)"}
            </Note>
          )}
          {x?.installed && (
            <Status ok={x.status.state === "ok" ? true : x.status.state === "failed" ? false : null}
              label={`Driver check: ${x.status.state}${x.status.driver ? ` · ${x.status.driver}` : ""}`}
              detail={<>{x.status.line && <Code>{x.status.line}</Code>}{(x.status.notes ?? []).map((n, i) => <div key={i}>{n}</div>)}</>} />
          )}
          {x?.installed && (
            <Note label="Services">
              {`Driver check at log-in: ${x.units.check ?? "not set up"} · prefix watcher: ${x.units.watcher ?? "not set up"}`}
            </Note>
          )}
          {x?.flatpak_steam && (
            <Status ok={false} label="Steam is a Flatpak" detail="xess-xmx-linux works with Steam installed from the system's packages; the Flatpak's Vulkan driver and Protons are separate." />
          )}
          {!!x?.missing_tools?.length && (
            <Status ok={false} label={`Missing: ${listText(x.missing_tools)}`}
              detail="install.sh needs these programs; install them with the system's package manager first." />
          )}
        </PanelSectionRow>
        <PanelSectionRow>
          {(!x?.installed || update) && (
            <ButtonItem layout="below" disabled={busy || !xs.data}
              onClick={() => inst.run("install", async () => blockedBy() ?? installXess(x, user, latest))}
              description={explain(x?.installed ? `Updates ${x.version ?? ""} to ${latest} in a new folder; the old one is uninstalled first.`
                : "Downloads the newest release and runs its install.sh as your user.", "Then log out and back in; asks first.")}>
              {inst.busy === "install" ? "Working…" : x?.installed ? `Update to ${latest}` : "Install XeSS XMX"}
            </ButtonItem>
          )}
          <ButtonItem layout="below" disabled={busy || chk.loading}
            onClick={() => inst.run("check", async () => { const r = await chk.reload(); return r && !r.ok ? r.error : null; })}
            description={explain("Asks GitHub for the newest xess-xmx-linux release. Changes nothing.",
              chk.data?.ok ? `${chk.data.update_available ? `${chk.data.latest} is available` : `${chk.data.installed ?? x?.version ?? "?"} is the newest`} (checked ${when(chk.data.checked_at)}).${chk.data.note ? ` ${chk.data.note}` : ""}` : undefined)}>
            {chk.loading ? "Checking…" : "Check for an update"}
          </ButtonItem>
          {x?.installed && (
            <ButtonItem layout="below" disabled={busy}
              onClick={() => inst.run("uninstall", async () => blockedBy() ?? uninstallXess(x))}
              description={explain("Runs the package's uninstall.sh as your user; the folder stays.", "Log out and back in afterwards; asks first.")}>
              {inst.busy === "uninstall" ? "Working…" : "Uninstall"}
            </ButtonItem>
          )}
          <Problem what="xess-xmx-linux" error={inst.error} />
        </PanelSectionRow>
        {inst.job && <JobView job={inst.job} lines={6} />}
      </PanelSection>
      {x?.installed && drivers.length > 0 && (
        <PanelSection title="Default driver">
          <PanelSectionRow>
            <DropdownItem key={`drv-${current?.version ?? ""}-${drivers.length}-${drvRev}`} label="Mesa driver for all games" menuLabel="Default Mesa driver"
              layout="below" childrenContainerWidth="max" bottomSeparator="none" disabled={running || !!drv.busy}
              rgOptions={driverOpts} selectedOption={current?.version ?? null}
              onChange={(o) => drv.run("driver", async () => {
                const d = drivers.find((v) => v.version === o.data);
                if (!d || d.default) return null;
                const ok = await confirm({ title: `Make Mesa ${d.version} the default?`, ok: "Switch",
                  text: `switch-driver.sh tests the driver first, then every game without MESA=… in its launch options uses it from its next start. No re-login needed; OXP3 Deck sets it again after an update.` });
                if (!ok) { setDrvRev((n) => n + 1); return null; }
                const r = await xess_set_driver(prefixFor(d, drivers));
                if (r.ok) toast(`Default driver: Mesa ${d.version}. Applies at each game's next start.`);
                else setDrvRev((n) => n + 1);
                invalidate("xess");
                return r.ok ? null : r.error;
              })}
              description={explain(`The package's patched driver games use unless their launch options say MESA=… (tools/switch-driver.sh). ${x.preferred_driver ? `Kept across updates: ${x.preferred_driver}.` : ""}`,
                "Applies at each game's next start; no re-login.")} />
            {drv.busy && <Status ok={null} label="Testing and switching the driver…" />}
            <Problem what="Default driver" error={drv.error} />
          </PanelSectionRow>
        </PanelSection>
      )}
      {x?.installed && (
        <PanelSection title="Protons">
          <PanelSectionRow>
            {(x.system_protons ?? []).map((p) => (
              <Status key={p.path} ok={p.shimmed} label={p.name}
                detail={p.shimmed ? "System Proton with the XMX shim." : "System Proton without the shim: XeSS in games on it stays on DP4a."} />
            ))}
            {unshimmed.length > 0 && (
              <ButtonItem layout="below" disabled={running || !!shim.busy}
                onClick={() => shim.run("shim", async () => {
                  const ok = await confirm({ title: "Add the XMX shim to the system Protons?", ok: "Add",
                    text: <>Copies the package's igdext64.dll into {listText(unshimmed.map((p) => p.name))} (keeping
                      the original as igdext64.dll.stock), as install.sh asks for these read-only folders. A package update of these Protons
                      brings the stock file back; tap this again then.</> });
                  if (!ok) return null;
                  const r = await xess_shim_system_protons();
                  invalidate("xess");
                  setShimNotes(r.notes ?? []);
                  if (r.ok && r.done?.length) toast(`Shim added to ${r.done.length} system Proton${r.done.length === 1 ? "" : "s"}`);
                  return r.ok ? (r.failed?.length ? `not done: ${listText(r.failed)}` : null) : r.error;
                })}
                description={explain("Gives games on CachyOS's system Protons (proton-cachyos-*) XeSS on the XMX cores.", "Applies at each game's next start; asks first.")}>
                {shim.busy ? "Working…" : `Add the shim (${unshimmed.length})`}
              </ButtonItem>
            )}
            {x.system_protons_need_shim && (
              <Status ok={false} label="A system Proton lost the shim"
                detail="A package update of it put the stock igdext64.dll back; add the shim again." />
            )}
            <Problem what="System Protons" error={shim.error} />
            {shimNotes.map((n, i) => <Note key={i}>{n}</Note>)}
            {(x.user_protons ?? []).length > 0 && (
              <Note label="Your Protons">
                {(x.user_protons ?? []).map((p) => `${p.name}: ${p.shimmed ? "shim" : "no shim"}, ${p.hook ? "hook" : "no hook"}`).join(" · ")}
              </Note>
            )}
          </PanelSectionRow>
        </PanelSection>
      )}
      {!!x?.old_folders?.length && (
        <PanelSection title="Old package folders">
          <PanelSectionRow>
            {x.relogin_needed && <Note>Log out and back in first: Steam still runs with the old folder's driver.</Note>}
          </PanelSectionRow>
          {x.old_folders.map((p) => <PanelSectionRow key={p}><OldFolder path={p} disabled={running || x.relogin_needed} /></PanelSectionRow>)}
        </PanelSection>
      )}
    </>
  );
};
