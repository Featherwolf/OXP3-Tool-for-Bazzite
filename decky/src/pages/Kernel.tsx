// CachyOS with Limine only: the installed kernels, what boots next, and oxp3-tool's kernel try (boot another kernel
// once, with a way back that needs no keyboard) and kernel keep. All of it from `oxp3-tool kernel --json`.
import { ButtonItem, PanelSection, PanelSectionRow } from "@decky/ui";
import { FC } from "react";

import { blockedBy, rebootNow, started, useAction } from "../actions";
import { oxp3_kernel, oxp3_kernel_keep, oxp3_kernel_try } from "../backend";
import { confirm } from "../components/confirm";
import { explain, Note, Problem, Status } from "../components/Hint";
import { JobView, TaskSection } from "../components/JobView";
import { useCurrentJob, useResource } from "../store";
import type { KernelRes, Oxp3KernelEntry } from "../types";
import { NeedsOxp3, useOxp3 } from "./Fixes";

const WHY: Record<string, string> = {
  "one-shot": "booted once, as asked (kernel try)",
  remembered: "it booted last and remember_last_entry is on",
  default: "Limine's default entry",
};

const tryKernel = async (k: Oxp3KernelEntry, now: string): Promise<string | null> => {
  const ok = await confirm({
    title: `Boot ${k.package} once?`, ok: "Boot it once",
    text: (
      <>
        <div>oxp3-tool makes {now}, the kernel running now, the one every power-on boots, and asks Limine to boot {k.package} ({k.release})
          once at the next reboot.</div>
        <div style={{ marginTop: 8 }}>If {k.package} does not come up, hold the power button to switch off, then on: {now} starts again.
          If it works, tap Keep on it to keep booting it.</div>
      </>
    ),
  });
  if (!ok) return null;
  return started(await oxp3_kernel_try(k.package));
};

const keepKernel = async (now: string): Promise<string | null> => {
  const ok = await confirm({
    title: `Keep booting ${now}?`, ok: "Keep",
    text: "oxp3-tool puts limine.conf's remember_last_entry and default_entry back as they were and cancels a pending one-time boot, so Limine keeps booting the kernel running now.",
  });
  if (!ok) return null;
  return started(await oxp3_kernel_keep());
};

export const KernelTab: FC = () => {
  const { ov, usable } = useOxp3();
  const job = useCurrentJob();
  const running = job?.state === "running";
  const kr = useResource<KernelRes>("kernel", oxp3_kernel, { skip: !usable || running });
  const act = useAction();
  if (!usable) return <NeedsOxp3 ov={ov.data} />;
  const k = kr.data?.ok ? kr.data.kernel : null;
  const now = k?.kernels.find((x) => x.running)?.package ?? "the running kernel";
  const oneshot = k?.next_boot.why === "one-shot";
  return (
    <>
      <TaskSection />
      <PanelSection title="Kernels">
        <PanelSectionRow>
          {!k && kr.loading && <Status ok={null} label="Reading Limine's configuration…" />}
          {!k && !kr.loading && running && <Status ok={null} label="Read when the running task has finished." />}
          <Problem what="oxp3-tool kernel" error={kr.data && !kr.data.ok ? kr.data.error : null} />
        </PanelSectionRow>
        {k?.kernels.map((x) => (
          <PanelSectionRow key={x.package}>
            <Status ok={x.running ? true : x.usable ? null : false} label={`${x.package} ${x.release}`}
              detail={[x.running && "running now", x.default && "Limine's default", !x.usable && "no usable Limine entry (or its files changed)"]
                .filter(Boolean).join(" · ") || "installed"} />
            {!x.running && x.usable && (
              <ButtonItem layout="below" disabled={!!act.busy || running}
                onClick={() => act.run(`try-${x.package}`, async () => blockedBy() ?? tryKernel(x, now))}
                description={explain(`Boots ${x.package} at the next reboot only; every later power-on boots ${now} until you keep it.`,
                  "Takes effect at the next reboot; asks first.")}>
                {act.busy === `try-${x.package}` ? "Working…" : "Boot it once"}
              </ButtonItem>
            )}
          </PanelSectionRow>
        ))}
      </PanelSection>
      {k && (
        <PanelSection title="Next boot">
          <PanelSectionRow>
            <Status ok={null} label={`Next boot: ${k.next_boot.package ?? "unknown"}`}
              detail={`${WHY[k.next_boot.why] ?? k.next_boot.why}${k.next_boot.then ? `; then ${k.next_boot.then}` : ""} · ${k.conf}`} />
            {(k.notes ?? []).map((n, i) => <Note key={i}>{n}</Note>)}
            {oneshot && (
              <ButtonItem layout="below" disabled={!!act.busy || running}
                onClick={() => act.run("reboot", () => rebootNow(`Boots ${k.next_boot.package} once. Save your game first.`))}
                description={explain(`Reboots into ${k.next_boot.package} now.`, "Asks first.")}>
                Reboot now
              </ButtonItem>
            )}
            {(k.trying || oneshot) && (
              <ButtonItem layout="below" disabled={!!act.busy || running}
                onClick={() => act.run("keep", async () => blockedBy() ?? keepKernel(now))}
                description={explain(`Keeps booting ${now}, the kernel running now${oneshot ? ", and cancels the pending one-time boot" : ""}.`,
                  "Takes effect at the next reboot; asks first.")}>
                {act.busy === "keep" ? "Working…" : `Keep ${now}`}
              </ButtonItem>
            )}
            <Problem what="Kernel" error={act.error} />
          </PanelSectionRow>
          {act.job && <JobView job={act.job} lines={6} />}
        </PanelSection>
      )}
    </>
  );
};
