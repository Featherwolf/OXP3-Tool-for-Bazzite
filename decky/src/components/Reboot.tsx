// "Do not reboot" while oxp3-tool's boot-entry rebuild is unfinished, else "Reboot to finish" when a change waits
// for one (fix.pending from oxp3-tool status).
import { ButtonItem, PanelSectionRow } from "@decky/ui";
import { FC } from "react";

import { rebootNow, useAction } from "../actions";
import { useCurrentJob } from "../store";
import type { Overview, Oxp3Status } from "../types";
import { listText } from "../util";
import { explain, Problem, Status } from "./Hint";

export const RebootRow: FC<{ o: Overview | null | undefined; st: Oxp3Status | null | undefined }> = ({ o, st }) => {
  const act = useAction();
  const job = useCurrentJob();
  const dnr = o?.do_not_reboot ?? st?.rebuild_unfinished ?? null;
  const waits = (st?.fixes ?? []).filter((f) => f.pending).map((f) =>
    f.pending === "inputplumber-restart" ? `${f.title} (the controller map loads at the next reboot)` : f.title);
  const pending = !!(st?.reboot_pending ?? o?.reboot_pending) || waits.length > 0;
  if (dnr) {
    return (
      <PanelSectionRow>
        <Status ok={false} red label="Do not reboot"
          detail={`oxp3-tool's last boot-entry rebuild did not finish (or still runs). Reboot only after "sudo ${dnr}" has run without errors in a terminal (Desktop Mode).`} />
      </PanelSectionRow>
    );
  }
  if (!pending) return null;
  const what = listText(waits) || "the last change";
  return (
    <PanelSectionRow>
      <ButtonItem layout="below" disabled={!!act.busy || job?.state === "running"}
        onClick={() => act.run("Reboot", () => rebootNow(`This finishes ${what}. Save your game first.`))}
        description={explain(`Waiting for a reboot: ${what}.`, "Reboots right away after you confirm.")}>
        {act.busy ? "Rebooting…" : "Reboot to finish"}
      </ButtonItem>
      <Problem what="Reboot" error={act.error} />
    </PanelSectionRow>
  );
};
