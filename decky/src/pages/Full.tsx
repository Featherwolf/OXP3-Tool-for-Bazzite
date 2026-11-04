// Adapted from ClawDeck (MIT, Copyright (c) 2026 IHulkSmashI and ClawDeck contributors).
// Based on ClawDeck's FullPage (src/pages.tsx): the full page.
// One tab per area, navigated with the D-pad on the left like Steam's own settings.
import { SidebarNavigation } from "@decky/ui";
import { FC, useEffect } from "react";
import { MdBuild, MdGamepad, MdHistory, MdMemory, MdSportsEsports, MdAutoAwesome } from "react-icons/md";

import { get_overview, oxp3_status } from "../backend";
import { ROUTE } from "../nav";
import { syncJob, useCurrentJob, useResource } from "../store";
import type { Overview, StatusRes } from "../types";
import { ButtonsTab } from "./Buttons";
import { FixesTab } from "./Fixes";
import { GamesTab } from "./Games";
import { KernelTab } from "./Kernel";
import { oxp3Usable } from "./Quick";
import { UpdatesTab } from "./Updates";
import { XessTab } from "./XeSS";

export const FullPage: FC = () => {
  const ov = useResource<Overview>("overview", get_overview, { fresh: true });
  const job = useCurrentJob();
  useEffect(() => { if (ov.data?.ok) syncJob(ov.data.job); }, [ov.data]);
  const st = useResource<StatusRes>("status", oxp3_status, { skip: !oxp3Usable(ov.data?.oxp3) || job?.state === "running" });
  const s = st.data?.ok ? st.data.status : null;
  // oxp3-tool kernel works only on CachyOS booting with Limine (kernel_cmd.py)
  const limine = s?.os.platform === "cachyos" && s.boot === "Limine";
  return (
    <SidebarNavigation title="OXP3 Deck" showTitle pages={[
      { title: "Fixes", icon: <MdBuild />, route: `${ROUTE}/fixes`, content: <FixesTab /> },
      { title: "Buttons", icon: <MdGamepad />, route: `${ROUTE}/buttons`, content: <ButtonsTab /> },
      { title: "XeSS", icon: <MdAutoAwesome />, route: `${ROUTE}/xess`, content: <XessTab /> },
      { title: "Games", icon: <MdSportsEsports />, route: `${ROUTE}/games`, content: <GamesTab /> },
      { title: "Kernel", icon: <MdMemory />, route: `${ROUTE}/kernel`, content: <KernelTab />, visible: limine },
      { title: "Updates & logs", icon: <MdHistory />, route: `${ROUTE}/updates`, content: <UpdatesTab /> },
    ]} />
  );
};
