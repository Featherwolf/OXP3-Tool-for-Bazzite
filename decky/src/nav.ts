// Adapted from ClawDeck (MIT, Copyright (c) 2026 IHulkSmashI and ClawDeck contributors).
import { Navigation } from "@decky/ui";

export const ROUTE = "/oxp3deck";
export type Tab = "fixes" | "buttons" | "xess" | "games" | "kernel" | "updates";

/** Opens the full page on one tab and closes the Quick Access menu. */
export const openPage = (tab: Tab = "fixes") => {
  try {
    Navigation.Navigate(`${ROUTE}/${tab}`);
    Navigation.CloseSideMenus();
  } catch { /* Steam's router changed: nothing to open */ }
};
