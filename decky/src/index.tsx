// Adapted from ClawDeck (MIT, Copyright (c) 2026 IHulkSmashI and ClawDeck contributors).
import { definePlugin, routerHook } from "@decky/api";
import { DialogButton, Focusable, staticClasses } from "@decky/ui";
import { MdSettings, MdVideogameAsset } from "react-icons/md";

import { openPage, ROUTE } from "./nav";
import { FullPage } from "./pages/Full";
import { QuickPanel } from "./pages/Quick";
import { stopPolling } from "./store";

export default definePlugin(() => {
  // exact: false, so /oxp3deck/<tab> reaches the page and SidebarNavigation picks the tab
  routerHook.addRoute(ROUTE, FullPage, { exact: false });
  return {
    name: "OXP3 Deck",
    // Decky's own title bar, with a button to the full page (as "More" at the bottom does)
    titleView: (
      <Focusable style={{ display: "flex", alignItems: "center", justifyContent: "space-between", width: "100%" }}>
        <div className={staticClasses.Title}>OXP3 Deck</div>
        <DialogButton aria-label="All OXP3 Deck settings" onClick={() => openPage("fixes")}
          style={{ width: "auto", minWidth: 0, height: 28, padding: "0 10px", display: "flex", alignItems: "center" }}>
          <MdSettings />
        </DialogButton>
      </Focusable>
    ),
    content: <QuickPanel />,
    icon: <MdVideogameAsset />,
    onDismount() {
      stopPolling();
      try { routerHook.removeRoute(ROUTE); } catch { /* the loader is going away */ }
    },
  };
});
