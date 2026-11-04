import { PanelSection, PanelSectionRow, ButtonItem } from "@decky/ui";
import { definePlugin } from "@decky/api";

export default definePlugin(() => ({
  name: "OXP3Deck",
  content: (<PanelSection title="test"><PanelSectionRow><ButtonItem onClick={() => {}}>x</ButtonItem></PanelSectionRow></PanelSection>),
  icon: <span>O</span>,
}));
